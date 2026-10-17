"""MCP server exposing six Anki tools, behind single-user OAuth. Run: python server.py

Env:
  PUBLIC_URL           public https URL of this server (OAuth issuer), e.g. https://anki.example.com
  OWNER_SECRET_HASH    from `python auth.py hash`
  ANKI_COLLECTION      collection path for the owner (default <DATA_DIR>/owner/collection.anki2)
  DATA_DIR             root for per-user data and the auth database (default ./data)
  ANKIWEB_USER / ANKIWEB_PASS   AnkiWeb login (omit to run without sync)
  HOST / PORT          bind address (default 127.0.0.1:8000, put a reverse proxy in front)
  REDIRECT_ALLOWLIST   comma-separated OAuth redirect rules (default: Claude + loopback)
  TRUST_PROXY=1        take the client IP from the proxy's X-Forwarded-For (behind a reverse proxy)
  ALLOW_INSECURE_NO_AUTH=1   dev only: no auth, and only when bound to loopback

Without PUBLIC_URL and OWNER_SECRET_HASH the server refuses to start.
"""

from __future__ import annotations

import functools
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Mapping
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp

from auth import (
    OWNER_SUBJECT,
    REGISTRATION_WINDOW,
    REGISTRATIONS_PER_IP,
    Authenticator,
    LoginLimiter,
    OwnerAuthenticator,
    OwnerProvider,
    RegistrationLimit,
    is_loopback,
    derive_server_key,
    default_client_ip,
    forwarded_client_ip,
    make_login_handler,
    redirect_allowlist_from_env,
)
from core import Bridge, BridgeError, SyncConfig

MCP_PATH = "/mcp"
_SUBJECT_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def surfaced(fn):
    """mcp 2.x hides the text of ordinary exceptions from the model ("Error executing tool x").
    Only ToolError text is shown, and BridgeError messages are written for Claude, so pass them on."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BridgeError as e:
            raise ToolError(str(e)) from e

    return wrapper


Tag = Annotated[str, StringConstraints(pattern=r"^\S+$")]


class NewNote(BaseModel):
    """One note for add_notes. The deck, note type and field names must already exist."""

    model_config = ConfigDict(extra="forbid")
    deck: str = Field(description="Existing deck, e.g. 'Physics::Optics' (see list_decks)")
    note_type: str = Field(description="Existing note type (see list_note_types)")
    fields: dict[str, str] = Field(description="Field name to text; names exactly as list_note_types gives them")
    tags: list[Tag] = Field(default_factory=list, description="Words without spaces")


class BridgeServer(MCPServer):
    """MCPServer that wraps some of the SDK's own HTTP routes (by path), e.g. to rate-limit /register.
    The SDK adds custom routes after its own, so a custom route cannot replace one.

    Overriding streamable_http_app covers mcp.run() and tests that build the app directly."""

    def __init__(self, *args: Any, route_guards: Mapping[str, Callable[[ASGIApp], ASGIApp]] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._route_guards = dict(route_guards or {})

    def streamable_http_app(self, **kwargs: Any) -> Starlette:
        app = super().streamable_http_app(**kwargs)
        for route in app.routes:
            if isinstance(route, Route) and route.path in self._route_guards:
                route.app = self._route_guards[route.path](route.app)
        return app


class ConfigError(Exception):
    """Startup configuration is unsafe or incomplete."""


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: str = "./data"
    collection: str | None = None  # overrides the owner's collection path
    sync: SyncConfig | None = None
    public_url: str | None = None  # None only in insecure dev mode
    owner_secret_hash: str | None = None
    redirect_allowlist: list[str] = field(default_factory=list)
    trust_proxy: bool = False

    @property
    def auth_enabled(self) -> bool:
        return self.owner_secret_hash is not None


def load_config(env: Mapping[str, str]) -> Config:
    host = env.get("HOST", "127.0.0.1")
    public_url = env.get("PUBLIC_URL") or None
    secret_hash = env.get("OWNER_SECRET_HASH") or None
    try:
        port = int(env.get("PORT", "8000"))
    except ValueError:
        raise ConfigError(f"PORT must be a number, not {env.get('PORT')!r}") from None
    cfg = Config(
        host=host,
        port=port,
        data_dir=env.get("DATA_DIR", "./data"),
        collection=env.get("ANKI_COLLECTION") or None,
        sync=SyncConfig.from_env(env),
        redirect_allowlist=redirect_allowlist_from_env(env),
        trust_proxy=env.get("TRUST_PROXY") == "1",
    )
    if public_url and secret_hash:
        u = urlparse(public_url)
        if not u.hostname or u.scheme not in ("http", "https") or u.path not in ("", "/") or u.query or u.fragment:
            raise ConfigError("PUBLIC_URL must be a bare origin like https://anki.example.com")
        if u.scheme != "https" and not is_loopback(u.hostname):
            raise ConfigError("PUBLIC_URL must be https unless it is a loopback address")
        if not secret_hash.startswith("scrypt$"):
            raise ConfigError("OWNER_SECRET_HASH is not a hash from `python auth.py hash`")
        cfg.public_url, cfg.owner_secret_hash = public_url.rstrip("/"), secret_hash
        return cfg
    if env.get("ALLOW_INSECURE_NO_AUTH") == "1":
        if not is_loopback(host):
            raise ConfigError(f"ALLOW_INSECURE_NO_AUTH is only allowed on a loopback HOST, not {host!r}")
        return cfg
    raise ConfigError(
        "Auth is not configured. Set PUBLIC_URL and OWNER_SECRET_HASH (python auth.py hash), "
        "or for local development only, ALLOW_INSECURE_NO_AUTH=1 on a loopback HOST."
    )


class Bridges:
    """One Bridge per subject. Today only the owner exists; this is the multi-user seam."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._bridges: dict[str, Bridge] = {}
        # Tools run in worker threads. Two Bridges for one subject would be two locks on one
        # collection, and the second open fails with "Anki already open".
        self._lock = threading.Lock()

    def collection_path(self, subject: str) -> str:
        owner = subject == OWNER_SUBJECT
        return (self._cfg.collection if owner else None) or os.path.join(
            self._cfg.data_dir, subject, "collection.anki2"
        )

    def for_subject(self, subject: str) -> Bridge:
        if not _SUBJECT_OK.match(subject):
            raise BridgeError("Invalid user.")
        with self._lock:
            if subject not in self._bridges:
                path = self.collection_path(subject)
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                # AnkiWeb credentials are the owner's; other users get none until per-user storage exists.
                sync = self._cfg.sync if subject == OWNER_SUBJECT else None
                self._bridges[subject] = Bridge(path, sync)
            return self._bridges[subject]


def transport_security(cfg: Config) -> TransportSecuritySettings:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if cfg.public_url:
        u = urlparse(cfg.public_url)
        hosts += [u.netloc]
        origins += [cfg.public_url, "https://claude.ai", "https://claude.com"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def build_server(cfg: Config, authenticator: Authenticator | None = None) -> tuple[MCPServer, OwnerProvider | None]:
    """authenticator defaults to the owner's secret; tests inject others to exercise the subject seam."""
    bridges = Bridges(cfg)
    client_ip = forwarded_client_ip if cfg.trust_proxy else default_client_ip

    def bridge() -> Bridge:
        """The caller's Bridge. Under auth the subject comes from the verified token; no token, no Bridge."""
        if not cfg.auth_enabled:
            return bridges.for_subject(OWNER_SUBJECT)
        token = get_access_token()
        if token is None or not token.subject:
            raise BridgeError("Not authenticated.")
        return bridges.for_subject(token.subject)

    provider: OwnerProvider | None = None
    kwargs: dict[str, Any] = {}
    route_guards: dict[str, Callable[[ASGIApp], ASGIApp]] = {}
    if cfg.auth_enabled:
        assert cfg.public_url and cfg.owner_secret_hash
        resource = cfg.public_url + MCP_PATH
        provider = OwnerProvider(
            os.path.join(cfg.data_dir, "auth.sqlite3"),
            cfg.redirect_allowlist,
            server_key=derive_server_key(cfg.owner_secret_hash),
            public_url=cfg.public_url,
            resource_url=resource,
        )
        kwargs = dict(
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=cfg.public_url,
                resource_server_url=resource,
                validate_token_resource=True,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
            ),
        )
        registrations = LoginLimiter(REGISTRATIONS_PER_IP, REGISTRATION_WINDOW)
        route_guards["/register"] = lambda app: RegistrationLimit(app, registrations, client_ip)

    mcp = BridgeServer(
        "anki",
        instructions=(
            "Tools for the user's Anki collection. Propose cards in chat first and "
            "call add_notes or update_note only after the user explicitly approves "
            "them in this conversation. Check list_note_types for field names and "
            "run find_notes for near-duplicates before proposing."
        ),
        route_guards=route_guards,
        **kwargs,
    )

    if provider is not None:
        handler = make_login_handler(
            provider,
            authenticator or OwnerAuthenticator(cfg.owner_secret_hash),
            LoginLimiter(),
            client_ip,
        )
        mcp.custom_route("/login", methods=["GET", "POST"])(handler)

    def guard(approved: bool) -> None:
        if not approved:
            raise BridgeError(
                "Not approved. Show the proposed cards to the user and call again "
                "with approved_by_user=true only after they say yes."
            )

    # Lets Claude's connector settings allow reads without asking and ask before writes.
    reads = ToolAnnotations(read_only_hint=True)
    adds = ToolAnnotations(read_only_hint=False, destructive_hint=False)
    edits = ToolAnnotations(read_only_hint=False, destructive_hint=True)  # overwrites field text

    @mcp.tool(annotations=reads)
    @surfaced
    def list_decks() -> list[dict[str, Any]]:
        """List decks with the number of notes directly in each."""
        return bridge().list_decks()

    @mcp.tool(annotations=reads)
    @surfaced
    def list_note_types(with_examples: bool = False) -> list[dict[str, Any]]:
        """List note types with field names and templates. with_examples adds one existing note per type."""
        return bridge().list_note_types(with_examples)

    @mcp.tool(annotations=reads)
    @surfaced
    def find_notes(query: str, limit: int = 20) -> dict[str, Any]:
        """Search with Anki search syntax (e.g. 'deck:Physics tag:to-review', 'prop:lapses>3', or plain words). Use before proposing cards to catch near-duplicates. Returns {total, notes}: the first `limit` (max 100) of `total` matches."""
        return bridge().find_notes(query, limit)

    @mcp.tool(annotations=adds)
    @surfaced
    def add_notes(notes: list[NewNote], approved_by_user: bool = False) -> dict[str, Any]:
        """Add notes in one all-or-nothing batch (max 50). Existing decks and note types only. Syncs with AnkiWeb before and after. Requires approved_by_user=true, set only after the user approved these exact cards."""
        guard(approved_by_user)
        return bridge().add_notes([n.model_dump() for n in notes])

    @mcp.tool(annotations=edits)
    @surfaced
    def update_note(
        note_id: int,
        fields: dict[str, str] | None = None,
        add_tags: list[Tag] | None = None,
        remove_tags: list[Tag] | None = None,
        approved_by_user: bool = False,
    ) -> dict[str, Any]:
        """Change fields (partial) and tags of one note. Cannot change deck or note type, and cannot delete: tag a note 'to-review' instead. Requires approved_by_user=true."""
        guard(approved_by_user)
        return bridge().update_note(note_id, fields, add_tags, remove_tags)

    @mcp.tool(annotations=adds)
    @surfaced
    def sync() -> dict[str, str]:
        """Normal sync with AnkiWeb. Fails if a full sync is required."""
        return bridge().sync()

    return mcp, provider


def startup_warnings(cfg: Config) -> list[str]:
    """Safe to run with, but worth seeing in the log."""
    out = []
    if not cfg.auth_enabled:
        out.append("running WITHOUT authentication (ALLOW_INSECURE_NO_AUTH=1, loopback only).")
    owner_collection = Bridges(cfg).collection_path(OWNER_SUBJECT)
    if not os.path.exists(owner_collection):
        out.append(f"no collection at {owner_collection}; tools fail until `python bootstrap.py download`.")
    return out


def main() -> None:
    os.umask(0o077)  # the collection, auth database and their journals: owner only
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        sys.exit(f"Refusing to start: {e}")
    for warning in startup_warnings(cfg):
        print(f"WARNING: {warning}", file=sys.stderr)
    mcp, _ = build_server(cfg)
    mcp.run(
        transport="streamable-http",
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=MCP_PATH,
        transport_security=transport_security(cfg),
    )


if __name__ == "__main__":
    main()
