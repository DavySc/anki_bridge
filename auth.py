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
import threading
from typing import Any, Protocol
from urllib.parse import urlparse

from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

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


class OwnerProvider:
    """OAuthAuthorizationServerProvider backed by SQLite. Built up task by task."""

    def __init__(self, path: str, redirect_allowlist: list[str] | None = None):
        self.path = path
        self.redirect_allowlist = list(
            DEFAULT_REDIRECT_ALLOWLIST if redirect_allowlist is None else redirect_allowlist
        )
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL)"
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
