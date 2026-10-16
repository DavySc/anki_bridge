# anki-claude-bridge

Headless MCP server that lets Claude add and edit notes in my Anki collection,
syncing through AnkiWeb. Spec and boundaries: [GOALS.md](GOALS.md).

## Dev environment

    nix flake lock        # first time, then commit flake.lock
    nix develop           # python 3.13, jj, git, and a .venv from requirements.txt

(`direnv allow` also works via `.envrc`.) In a jj colocated repo, if nix
complains that flake.nix is not tracked, use `nix develop path:.`.

## Run locally

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    python -m pytest -q            # throwaway collection, in-process OAuth flow

    export ANKIWEB_USER=... ANKIWEB_PASS=...
    python bootstrap.py download   # one time, by hand: ./data/owner/collection.anki2

Development without auth (loopback only, prints a warning):

    ALLOW_INSECURE_NO_AUTH=1 python server.py      # http://127.0.0.1:8000/mcp

With auth, as it runs in production:

    python auth.py hash                            # prompts, prints OWNER_SECRET_HASH='...'
    export OWNER_SECRET_HASH='scrypt$...'
    export PUBLIC_URL=http://127.0.0.1:8000        # https://anki.panzeilkamp.com in production
    python server.py

The server refuses to start without `PUBLIC_URL` and `OWNER_SECRET_HASH`, and
refuses `ALLOW_INSECURE_NO_AUTH` on anything but a loopback `HOST`.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `PUBLIC_URL` | Public origin, used as the OAuth issuer. https unless loopback | required |
| `OWNER_SECRET_HASH` | Login secret hash from `python auth.py hash`. Changing it makes registered clients register again | required |
| `ALLOW_INSECURE_NO_AUTH` | `1` = no auth, dev only, loopback `HOST` only | unset |
| `HOST` / `PORT` | Bind address | `127.0.0.1` / `8000` |
| `DATA_DIR` | Per-user data and `auth.sqlite3` | `./data` |
| `ANKI_COLLECTION` | Owner's collection path | `<DATA_DIR>/owner/collection.anki2` |
| `ANKIWEB_USER` / `ANKIWEB_PASS` | AnkiWeb login; omit to run without sync | unset |
| `REDIRECT_ALLOWLIST` | Comma-separated OAuth redirect rules (`scheme://host`; no port = any port, for loopback) | claude.ai, claude.com, loopback |
| `TRUST_PROXY` | `1` = client IP from the last `X-Forwarded-For` entry (set behind Caddy) | unset |

## How auth works

The server is its own single-user OAuth 2.1 server. Claude registers itself
(dynamic client registration), sends you to `/login`, you enter the owner secret
once, and Claude receives a 1 hour access token plus a 30 day refresh token.
Tokens, codes and login links are stored only as hashes; client secrets are
derived from the owner secret hash and never stored. Failed logins are limited
to 5 per 15 minutes per IP (25 overall). Details: [SPEC-auth.md](SPEC-auth.md).

Multi-user is not built, but every token carries a `subject`, login goes through
an `Authenticator`, and tools pick their collection via `Bridges.for_subject`
(`data/<subject>/`). See "Multi-user readiness" in [GOALS.md](GOALS.md).
Deliberately not built: user table, signup, per-user AnkiWeb credentials, quotas.

## Status

- Done: six tools, validation, OAuth (clients, login, tokens, rotation, revoke),
  startup guard, tests (core logic and the HTTP auth flow, all offline).
- Untested: real AnkiWeb sync (module `sync-verification`).
- Untested: the real Claude connector against this server (needs HTTPS, so it
  waits for module `deploy`). Open questions to settle then: does Claude use
  dynamic client registration, and which exact callback URLs does it use
  (adjust `REDIRECT_ALLOWLIST`)? If it needs a fixed client, add one
  pre-registered client from env vars.
- Not built: systemd unit, Caddy block (module `deploy`).

Note: this uses mcp 2.x (`MCPServer`), not the 1.x `FastMCP`. It hides the text
of ordinary exceptions from the model, so `server.py` wraps tools in `surfaced`.
