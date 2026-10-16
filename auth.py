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
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

# The one place the owner's subject is defined. Do not repeat the literal.
OWNER_SUBJECT = "owner"

# Rule = scheme://host. A rule without a port allows any port, which is only
# sensible for loopback; a non-loopback rule must match the URI's host exactly.
DEFAULT_REDIRECT_ALLOWLIST = [
    "https://claude.ai",
    "https://claude.com",
    "http://127.0.0.1",
    "http://localhost",
    "http://[::1]",
]


def redirect_allowlist_from_env() -> list[str]:
    raw = os.environ.get("REDIRECT_ALLOWLIST")
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


def _loopback(host: str) -> bool:
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
        if r.port is not None:
            if u.port == r.port:
                return True
        elif _loopback(u.hostname) or u.port is None:
            return True
    return False


PENDING_TTL = 600  # seconds a login page stays valid
CODE_TTL = 300  # seconds an authorization code stays valid


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
        public_url: str = "http://127.0.0.1:8000",
        now: Callable[[], float] = time.time,
    ):
        self.path = path
        self.public_url = public_url.rstrip("/")
        self._now = now
        self.redirect_allowlist = list(
            DEFAULT_REDIRECT_ALLOWLIST if redirect_allowlist is None else redirect_allowlist
        )
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
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
            " resource TEXT)"
        )
        self._db.commit()

    def _exec(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self._exec("SELECT info FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        for uri in uris:
            if not _allowed(uri, self.redirect_allowlist):
                raise RegistrationError("invalid_redirect_uri", f"redirect_uri not allowed: {uri}")
        # The SDK compares the presented client_secret to the stored one, so it is stored
        # as given. It is a DCR client credential, not the owner secret or a token.
        self._exec(
            "INSERT INTO clients (client_id, info) VALUES (?, ?)",
            (client_info.client_id, client_info.model_dump_json()),
        )

    # ---- authorize -> login -> code -----------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to our login page."""
        req_id = secrets.token_urlsafe(32)
        self._exec("DELETE FROM pending WHERE expires_at < ?", (self._now(),))
        self._exec(
            "INSERT INTO pending (req_hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
            (_sha256(req_id), client.client_id, params.model_dump_json(), self._now() + PENDING_TTL),
        )
        return construct_redirect_uri(f"{self.public_url}/login", req=req_id)

    def pending_exists(self, req_id: str) -> bool:
        rows = self._exec("SELECT expires_at FROM pending WHERE req_hash = ?", (_sha256(req_id),))
        return bool(rows) and rows[0][0] >= self._now()

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
            "INSERT INTO codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _sha256(code),
                client_id,
                subject,
                json.dumps(params.scopes or []),
                self._now() + CODE_TTL,
                params.code_challenge,
                str(params.redirect_uri),
                int(params.redirect_uri_provided_explicitly),
                params.resource,
            ),
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        rows = self._exec(
            "SELECT client_id, subject, scopes, expires_at, code_challenge, redirect_uri, explicit, resource"
            " FROM codes WHERE code_hash = ?",
            (_sha256(authorization_code),),
        )
        if not rows:
            return None
        client_id, subject, scopes, expires_at, challenge, redirect_uri, explicit, resource = rows[0]
        if client_id != client.client_id or expires_at < self._now():
            return None
        return AuthorizationCode(
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

    def _delete_code(self, code: str) -> None:
        """Codes are single use: the token exchange (task 3) calls this."""
        self._exec("DELETE FROM codes WHERE code_hash = ?", (_sha256(code),))


# ---- login page ----------------------------------------------------------------


class LoginLimiter:
    """Failed-login limit per key within a window. In memory, single process.

    Keys are chosen by the caller from things the attacker cannot vary freely
    (client IP, and a global bucket), never from the claimed username alone.
    """

    def __init__(self, max_failures: int = 5, window: float = 900, now: Callable[[], float] = time.time):
        self.max, self.window, self._now = max_failures, window, now
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        q = self._fails[key]
        while q and q[0] <= self._now() - self.window:
            q.popleft()
        return q

    def blocked(self, key: str, limit: int | None = None) -> bool:
        return len(self._recent(key)) >= (limit or self.max)

    def fail(self, key: str) -> None:
        self._recent(key).append(self._now())

    def clear(self, key: str) -> None:
        self._fails.pop(key, None)


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


def _default_client_ip(request: Request) -> str:
    # Behind Caddy this is the proxy unless uvicorn trusts X-Forwarded-For (deploy decides).
    return request.client.host if request.client else "unknown"


def make_login_handler(
    provider: OwnerProvider,
    authenticator: Authenticator,
    limiter: LoginLimiter,
    client_ip: Callable[[Request], str] = _default_client_ip,
):
    """Starlette handler for GET/POST /login. Register it unauthenticated (custom_route)."""

    async def login(request: Request) -> Response:
        if request.method == "POST":
            form = await request.form()
            req_id = str(form.get("req", ""))
        else:
            form = None
            req_id = request.query_params.get("req", "")
        if not req_id or not provider.pending_exists(req_id):
            return _page("<p class=e>This login link is invalid or has expired. Start again from Claude.</p>", 400)

        if form is None:
            return _page(
                f'<form method="post"><input type="hidden" name="req" value="{html.escape(req_id)}">'
                '<input type="password" name="secret" placeholder="Owner secret" autofocus required>'
                "<button>Authorize Claude</button></form>"
            )

        ip_key = f"ip:{client_ip(request)}"
        if limiter.blocked(ip_key) or limiter.blocked("global", GLOBAL_FAILURE_LIMIT):
            return _page("<p class=e>Too many failed attempts. Try again later.</p>", 429)

        credentials = {k: str(v) for k, v in form.items() if k in ("username", "secret")}
        subject = authenticator.authenticate(credentials)
        if subject is None:
            limiter.fail(ip_key)
            limiter.fail("global")
            return _page(
                f'<p class=e>Wrong secret.</p><form method="post"><input type="hidden" name="req" '
                f'value="{html.escape(req_id)}"><input type="password" name="secret" '
                'placeholder="Owner secret" autofocus required><button>Authorize Claude</button></form>',
                401,
            )
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
