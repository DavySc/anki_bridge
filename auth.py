"""Single-user OAuth provider for the MCP server. No Anki code here.

The SDK serves the OAuth endpoints; this module supplies storage and the
decision of who may log in. Multi-user is not built, but the seams are:
every grant carries a `subject`, and login goes through an `Authenticator`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import sqlite3
import getpass
import html
import json
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from contextlib import contextmanager
from typing import Iterator

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("anki_bridge.auth")

# The one place the owner's subject is defined. Do not repeat the literal.
OWNER_SUBJECT = "owner"

# Rule = scheme://host[:port][/path]. A rule without a port allows any port, which is only
# sensible for loopback; a non-loopback rule must match the URI's host exactly. A rule with a
# path allows only that path: any page on claude.ai that leaked its URL would leak the code.
DEFAULT_REDIRECT_ALLOWLIST = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://127.0.0.1",
    "http://localhost",
    "http://[::1]",
]


def derive_server_key(owner_secret_hash: str) -> bytes:
    """Key for derived client secrets. Changing the owner secret invalidates registered
    clients, which simply register again."""
    return hmac.new(b"anki-claude-bridge/server-key/v1", owner_secret_hash.encode(), hashlib.sha256).digest()


def redirect_allowlist_from_env(env: Mapping[str, str] = os.environ) -> list[str]:
    raw = env.get("REDIRECT_ALLOWLIST")
    if not raw:
        return list(DEFAULT_REDIRECT_ALLOWLIST)
    return [r.strip() for r in raw.split(",") if r.strip()]


# ---- owner secret ------------------------------------------------------------

_SCRYPT = {"n": 2**15, "r": 8, "p": 1}


def _scrypt(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * n * r * 2, dklen=32)


def hash_secret(secret: str) -> str:
    """Format: scrypt$n$r$p$salt_b64$hash_b64"""
    salt = os.urandom(16)
    digest = _scrypt(secret, salt, **_SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = _scrypt(secret, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


class Authenticator(Protocol):
    """Decides who a login belongs to. Returns the subject, or None."""

    def authenticate(self, credentials: dict[str, str]) -> str | None: ...


class OwnerAuthenticator:
    """Today's only login: one secret, one subject. Username is ignored."""

    def __init__(self, secret_hash: str):
        self._hash = secret_hash

    def authenticate(self, credentials: dict[str, str]) -> str | None:
        secret = credentials.get("secret")
        if secret and verify_secret(secret, self._hash):
            return OWNER_SUBJECT
        return None


# ---- provider ----------------------------------------------------------------


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allowed(uri: str, rules: list[str]) -> bool:
    u = urlparse(uri)
    if u.username or u.password or u.fragment or not u.hostname:
        return False
    for rule in rules:
        r = urlparse(rule)
        if u.scheme != r.scheme or u.hostname != r.hostname:
            continue
        if r.path not in ("", "/") and u.path != r.path:
            continue
        if r.port is not None:
            if u.port == r.port:
                return True
        elif is_loopback(u.hostname) or u.port is None:
            return True
    return False


PENDING_TTL = 600  # seconds a login page stays valid
CODE_TTL = 300  # seconds an authorization code stays valid
ACCESS_TTL = 3600  # 1 hour
REFRESH_TTL = 30 * 24 * 3600  # 30 days

# Registration and /authorize are open to anyone, so what they can store is bounded.
MAX_CLIENTS = 100  # idle clients are evicted oldest first; clients holding tokens never are
MAX_CLIENT_METADATA = 8 * 1024  # bytes stored per client; a normal registration is under 1 KB
MAX_REDIRECT_URIS = 10
MAX_PENDING_PER_CLIENT = 10  # parked logins per client; the oldest are dropped
REGISTRATIONS_PER_IP = 10  # per REGISTRATION_WINDOW; Claude registers once per connector
REGISTRATION_WINDOW = 3600


# A grant is one successful login. Its id ties together the code and every token
# issued from it, so reuse detection and revoke act on exactly that login.
class OwnerAuthorizationCode(AuthorizationCode):
    grant_id: str


class OwnerRefreshToken(RefreshToken):
    grant_id: str
    rotated: bool = False


class OwnerAccessToken(AccessToken):
    grant_id: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerProvider:
    """OAuthAuthorizationServerProvider backed by SQLite. Built up task by task.

    Secrets that grant access (login request ids, codes) are stored only as hashes.
    """

    def __init__(
        self,
        path: str,
        redirect_allowlist: list[str] | None = None,
        *,
        server_key: bytes,
        public_url: str = "http://127.0.0.1:8000",
        resource_url: str | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.path = path
        self._server_key = server_key
        self.public_url = public_url.rstrip("/")
        # When set, tokens are only issued for this resource (the MCP endpoint).
        self.resource_url = resource_url.rstrip("/") if resource_url else None
        self._now = now
        self.redirect_allowlist = list(
            DEFAULT_REDIRECT_ALLOWLIST if redirect_allowlist is None else redirect_allowlist
        )
        # Clients and token hashes are for the server's eyes only (SQLite gives its journal the
        # database's mode).
        os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending (req_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
            " params TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,"
            " subject TEXT NOT NULL, scopes TEXT NOT NULL, expires_at REAL NOT NULL,"
            " code_challenge TEXT NOT NULL, redirect_uri TEXT NOT NULL, explicit INTEGER NOT NULL,"
            " resource TEXT, grant_id TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0)"
        )
        for table in ("access_tokens", "refresh_tokens"):
            self._db.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL,"
                " client_id TEXT NOT NULL, subject TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT,"
                " expires_at REAL NOT NULL"
                + (", rotated INTEGER NOT NULL DEFAULT 0)" if table == "refresh_tokens" else ")")
            )
        self._db.commit()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    def _client_secret(self, client_id: str) -> str:
        return hmac.new(self._server_key, b"client-secret|" + client_id.encode(), hashlib.sha256).hexdigest()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self._exec("SELECT info FROM clients WHERE client_id = ?", (client_id,))
        if not rows:
            return None
        info = OAuthClientInformationFull.model_validate_json(rows[0][0])
        if info.token_endpoint_auth_method != "none":
            info.client_secret = self._client_secret(client_id)
        return info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        if len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri", f"at most {MAX_REDIRECT_URIS} redirect_uris")
        for uri in uris:
            if not _allowed(uri, self.redirect_allowlist):
                raise RegistrationError("invalid_redirect_uri", f"redirect_uri not allowed: {uri}")
        stored = client_info.model_copy(update={"client_secret": None}).model_dump_json()
        if len(stored.encode()) > MAX_CLIENT_METADATA:
            raise RegistrationError("invalid_client_metadata", f"client metadata over {MAX_CLIENT_METADATA} bytes")
        # The SDK compares the presented client_secret with what get_client returns, so a
        # hash will not do. Instead the secret is derived from a server key and never stored:
        # replace the random one the SDK minted (it echoes this same object to the client).
        if client_info.token_endpoint_auth_method != "none" and client_info.client_secret is not None:
            client_info.client_secret = self._client_secret(client_info.client_id)
        with self._tx() as db:
            self._make_room_for_a_client(db)
            db.execute("INSERT INTO clients (client_id, info) VALUES (?, ?)", (client_info.client_id, stored))

    def _make_room_for_a_client(self, db: sqlite3.Connection) -> None:
        """Evict the oldest clients that hold no live token or unused code, so one more fits."""
        excess = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] - MAX_CLIENTS + 1
        if excess <= 0:
            return
        now = self._now()
        idle = db.execute(
            "SELECT client_id FROM clients WHERE client_id NOT IN ("
            " SELECT client_id FROM access_tokens WHERE expires_at >= ?"
            " UNION SELECT client_id FROM refresh_tokens WHERE expires_at >= ?"
            " UNION SELECT client_id FROM codes WHERE expires_at >= ? AND used = 0)"
            " ORDER BY rowid LIMIT ?",
            (now, now, now, excess),
        ).fetchall()
        if len(idle) < excess:
            raise RegistrationError("invalid_client_metadata", "too many registered clients")
        for (client_id,) in idle:
            db.execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
            db.execute("DELETE FROM pending WHERE client_id = ?", (client_id,))

    # ---- authorize -> login -> code -----------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to our login page."""
        if self.resource_url and params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError("invalid_target", "unknown resource")
        req_id = secrets.token_urlsafe(32)
        with self._tx() as db:
            db.execute("DELETE FROM pending WHERE expires_at < ?", (self._now(),))
            db.execute(
                "INSERT INTO pending (req_hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                (_sha256(req_id), client.client_id, params.model_dump_json(), self._now() + PENDING_TTL),
            )
            db.execute(
                "DELETE FROM pending WHERE client_id = ? AND rowid NOT IN"
                " (SELECT rowid FROM pending WHERE client_id = ? ORDER BY rowid DESC LIMIT ?)",
                (client.client_id, client.client_id, MAX_PENDING_PER_CLIENT),
            )
        return construct_redirect_uri(f"{self.public_url}/login", req=req_id)

    def pending_login(self, req_id: str) -> tuple[str, str] | None:
        """(client name, redirect host) of a live login request, for the login page to show.
        Anyone can register a client and send the owner a real login link, so the page says who
        asks. None if the request is unknown or expired."""
        rows = self._exec(
            "SELECT p.params, p.expires_at, c.info FROM pending p JOIN clients c USING (client_id)"
            " WHERE p.req_hash = ?",
            (_sha256(req_id),),
        )
        if not rows or rows[0][1] < self._now():
            return None
        params, _, info = rows[0]
        name = json.loads(info).get("client_name") or "A client without a name"
        redirect = urlparse(str(AuthorizationParams.model_validate_json(params).redirect_uri))
        return name, redirect.netloc

    def complete_login(self, req_id: str, subject: str) -> str | None:
        """Consume the pending request and return the client's redirect URL with a fresh code.

        None if the request is unknown, expired or already used. The only place a
        redirect is built, and it only ever targets the redirect_uri the SDK validated.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT client_id, params, expires_at FROM pending WHERE req_hash = ?", (_sha256(req_id),)
            ).fetchone()
            if row is None:
                return None
            self._db.execute("DELETE FROM pending WHERE req_hash = ?", (_sha256(req_id),))
            self._db.commit()
        client_id, raw, expires_at = row
        if expires_at < self._now():
            return None
        params = AuthorizationParams.model_validate_json(raw)
        code = secrets.token_urlsafe(32)
        self._exec(
            "INSERT INTO codes (code_hash, client_id, subject, scopes, expires_at, code_challenge,"
            " redirect_uri, explicit, resource, grant_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _sha256(code),
                client_id,
                subject,
                json.dumps(params.scopes or []),
                self._now() + CODE_TTL,
                params.code_challenge,
                str(params.redirect_uri),
                int(params.redirect_uri_provided_explicitly),
                params.resource or self.resource_url,
                secrets.token_urlsafe(16),
            ),
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> OwnerAuthorizationCode | None:
        rows = self._exec(
            "SELECT client_id, subject, scopes, expires_at, code_challenge, redirect_uri, explicit, resource,"
            " grant_id FROM codes WHERE code_hash = ?",
            (_sha256(authorization_code),),
        )
        if not rows:
            return None
        client_id, subject, scopes, expires_at, challenge, redirect_uri, explicit, resource, grant_id = rows[0]
        if client_id != client.client_id or expires_at < self._now():
            return None
        return OwnerAuthorizationCode(
            grant_id=grant_id,
            code=authorization_code,
            scopes=json.loads(scopes),
            expires_at=expires_at,
            client_id=client_id,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=bool(explicit),
            resource=resource,
            subject=subject,
        )

    # ---- tokens ---------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _revoke_grant(self, db: sqlite3.Connection, grant_id: str) -> None:
        for table in ("access_tokens", "refresh_tokens", "codes"):
            db.execute(f"DELETE FROM {table} WHERE grant_id = ?", (grant_id,))

    def _issue(
        self, db: sqlite3.Connection, grant_id: str, client_id: str, subject: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        now = self._now()
        for table in ("access_tokens", "refresh_tokens"):
            db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for table, token, ttl in (("access_tokens", access, ACCESS_TTL), ("refresh_tokens", refresh, REFRESH_TTL)):
            db.execute(
                f"INSERT INTO {table} (token_hash, grant_id, client_id, subject, scopes, resource, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_sha256(token), grant_id, client_id, subject, json.dumps(scopes), resource, now + ttl),
            )
        return OAuthToken(
            access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Single use: a second exchange of the same code revokes everything issued from it."""
        assert isinstance(authorization_code, OwnerAuthorizationCode)
        with self._tx() as db:
            used = db.execute(
                "UPDATE codes SET used = 1 WHERE code_hash = ? AND client_id = ? AND used = 0",
                (_sha256(authorization_code.code), client.client_id),
            ).rowcount
            if used == 1:
                return self._issue(
                    db,
                    authorization_code.grant_id,
                    client.client_id,
                    authorization_code.subject or OWNER_SUBJECT,
                    authorization_code.scopes,
                    authorization_code.resource,
                )
            self._revoke_grant(db, authorization_code.grant_id)
        # Raised after the transaction commits, so the revocation sticks.
        raise TokenError("invalid_grant", "authorization code already used")

    async def load_access_token(self, token: str) -> OwnerAccessToken | None:
        rows = self._exec(
            "SELECT grant_id, client_id, subject, scopes, resource, expires_at FROM access_tokens"
            " WHERE token_hash = ?",
            (_sha256(token),),
        )
        if not rows:
            return None
        grant_id, client_id, subject, scopes, resource, expires_at = rows[0]
        if expires_at < self._now():
            return None
        return OwnerAccessToken(
            token=token,
            grant_id=grant_id,
            client_id=client_id,
            subject=subject,
            scopes=json.loads(scopes),
            resource=resource,
            expires_at=int(expires_at),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> OwnerRefreshToken | None:
        rows = self._exec(
            "SELECT grant_id, client_id, subject, scopes, resource, expires_at, rotated FROM refresh_tokens"
            " WHERE token_hash = ?",
            (_sha256(refresh_token),),
        )
        if not rows:
            return None
        grant_id, client_id, subject, scopes, resource, expires_at, rotated = rows[0]
        if client_id != client.client_id or expires_at < self._now():
            return None
        return OwnerRefreshToken(
            token=refresh_token,
            grant_id=grant_id,
            client_id=client_id,
            subject=subject,
            scopes=json.loads(scopes),
            resource=resource,
            expires_at=int(expires_at),
            rotated=bool(rotated),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: the old refresh token dies. Presenting it again revokes the whole grant."""
        assert isinstance(refresh_token, OwnerRefreshToken)
        with self._tx() as db:
            fresh = db.execute(
                "UPDATE refresh_tokens SET rotated = 1 WHERE token_hash = ? AND client_id = ? AND rotated = 0",
                (_sha256(refresh_token.token), client.client_id),
            ).rowcount
            if fresh == 1:
                db.execute("DELETE FROM access_tokens WHERE grant_id = ?", (refresh_token.grant_id,))
                return self._issue(
                    db,
                    refresh_token.grant_id,
                    client.client_id,
                    refresh_token.subject or OWNER_SUBJECT,
                    scopes,
                    refresh_token.resource,
                )
            self._revoke_grant(db, refresh_token.grant_id)
        raise TokenError("invalid_grant", "refresh token already used; grant revoked")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoking either token ends the whole grant (login) it belongs to."""
        grant_id = getattr(token, "grant_id", None)
        if grant_id:
            with self._tx() as db:
                self._revoke_grant(db, grant_id)


# ---- login page ----------------------------------------------------------------


def rate_limit_key(ip: str) -> str:
    """The rate-limit bucket for a client address. An IPv6 /64 is one subscriber, who can use
    every address in it; IPv4 (also IPv4-mapped IPv6) counts per address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"ip:{ip}"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return f"ip:{addr.ipv4_mapped}"
        return f"ip:{ipaddress.IPv6Network((addr, 64), strict=False)}"
    return f"ip:{addr}"


class LoginLimiter:
    """Event limit per key within a window: failed logins, and client registrations. In memory,
    single process.

    Keys are chosen by the caller from things the attacker cannot vary freely
    (client IP, and a global bucket), never from the claimed username alone.
    """

    def __init__(self, max_failures: int = 5, window: float = 900, now: Callable[[], float] = time.time):
        self.max, self.window, self._now = max_failures, window, now
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        q = self._events[key]
        while q and q[0] <= self._now() - self.window:
            q.popleft()
        return q

    def blocked(self, key: str, limit: int | None = None) -> bool:
        return len(self._recent(key)) >= (limit or self.max)

    def hit(self, key: str) -> None:
        self._recent(key).append(self._now())
        if len(self._events) > 1024:  # one key per client IP; anyone can add IPs
            cutoff = self._now() - self.window
            for k in [k for k, q in self._events.items() if not q or q[-1] <= cutoff]:
                del self._events[k]

    def clear(self, key: str) -> None:
        self._events.pop(key, None)


class RegistrationLimit:
    """ASGI wrapper for the SDK's /register endpoint: each client IP may register a few clients
    per window. The provider bounds what is stored; this bounds how fast anyone can churn it."""

    def __init__(self, app: ASGIApp, limiter: LoginLimiter, client_ip: Callable[[Request], str]):
        self.app, self.limiter, self.client_ip = app, limiter, client_ip

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            ip = self.client_ip(Request(scope))
            key = rate_limit_key(ip)
            if self.limiter.blocked(key):
                logger.warning("client registration refused: rate limit (client %r)", ip)
                response = JSONResponse(
                    {"error": "too_many_requests", "error_description": "too many client registrations; try later"},
                    status_code=429,
                    headers={"Retry-After": str(int(self.limiter.window))},
                )
                await response(scope, receive, send)
                return
            self.limiter.hit(key)  # every registration counts
        await self.app(scope, receive, send)


GLOBAL_FAILURE_LIMIT = 25  # across all IPs, so rotating IPs does not help
_NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Anki bridge login</title>
<style>body{{font-family:system-ui;max-width:22rem;margin:4rem auto;padding:0 1rem}}
input,button{{width:100%;padding:.6rem;margin:.4rem 0;font-size:1rem}}.e{{color:#b00}}</style></head>
<body><h1>Anki bridge</h1>{body}</body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status, headers=_NO_STORE)


def _login_form(req_id: str, who: tuple[str, str], error: str = "") -> str:
    name, host = who
    return (
        (f"<p class=e>{error}</p>" if error else "")
        + f"<p><b>{html.escape(name[:100])}</b> asks for access to your Anki collection. After you"
        f" log in, your browser goes to <b>{html.escape(host)}</b>. If you did not just connect"
        " Claude to this server, close this page.</p>"
        f'<form method="post"><input type="hidden" name="req" value="{html.escape(req_id)}">'
        '<input type="password" name="secret" placeholder="Owner secret" autofocus required>'
        "<button>Authorize</button></form>"
    )


def forwarded_client_ip(request: Request) -> str:
    """Client IP behind one trusted reverse proxy (Caddy): the last X-Forwarded-For entry,
    which the proxy appended itself. Earlier entries are client-supplied and ignored."""
    xff = request.headers.get("x-forwarded-for", "")
    last = xff.rsplit(",", 1)[-1].strip()
    return last or default_client_ip(request)


def default_client_ip(request: Request) -> str:
    # Behind Caddy this is the proxy unless uvicorn trusts X-Forwarded-For (deploy decides).
    return request.client.host if request.client else "unknown"


def make_login_handler(
    provider: OwnerProvider,
    authenticator: Authenticator,
    limiter: LoginLimiter,
    client_ip: Callable[[Request], str] = default_client_ip,
):
    """Starlette handler for GET/POST /login. Register it unauthenticated (custom_route)."""

    async def login(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            req_id = str(form.get("req", ""))
        else:
            form = None
            req_id = request.query_params.get("req", "")
        who = provider.pending_login(req_id) if req_id else None
        if who is None:
            return _page("<p class=e>This login link is invalid or has expired. Start again from Claude.</p>", 400)

        if form is None:
            return _page(_login_form(req_id, who))

        ip = client_ip(request)
        ip_key = rate_limit_key(ip)
        if limiter.blocked(ip_key) or limiter.blocked("global", GLOBAL_FAILURE_LIMIT):
            logger.warning("login refused: too many failed attempts (client %r)", ip)
            return _page("<p class=e>Too many failed attempts. Try again later.</p>", 429)

        credentials = {k: str(v) for k, v in form.items() if k in ("username", "secret")}
        subject = authenticator.authenticate(credentials)
        if subject is None:
            limiter.hit(ip_key)
            limiter.hit("global")
            logger.warning("login failed: wrong secret (client %r)", ip)
            return _page(_login_form(req_id, who, "Wrong secret."), 401)
        logger.info("login succeeded for %s (client %r)", subject, ip)
        limiter.clear(ip_key)
        url = provider.complete_login(req_id, subject)
        if url is None:
            return _page("<p class=e>This login link is invalid or has expired. Start again from Claude.</p>", 400)
        return RedirectResponse(url, status_code=302, headers=_NO_STORE)

    return login


def main(argv: list[str], prompt: Callable[[str], str] = getpass.getpass) -> str:
    if argv != ["hash"]:
        sys.exit("usage: python auth.py hash")
    first = prompt("Owner secret: ")
    if len(first) < 12:
        sys.exit("Use at least 12 characters.")
    if prompt("Again: ") != first:
        sys.exit("Secrets do not match.")
    return f"OWNER_SECRET_HASH='{hash_secret(first)}'"


if __name__ == "__main__":
    print(main(sys.argv[1:]))
