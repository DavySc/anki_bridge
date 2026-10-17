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
| `REDIRECT_ALLOWLIST` | Comma-separated OAuth redirect rules (`scheme://host[/path]`; no port = any port, for loopback; a path allows only that path) | `/api/mcp/auth_callback` on claude.ai and claude.com, loopback |
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
- Done: sync checked offline against Anki's bundled sync server (see "Sync tests").
- Untested: real AnkiWeb sync. AnkiWeb-only behaviour (login rate limits, dropped
  connections that never answer) cannot be reproduced locally. Follow "First real use".
- Untested: the real Claude connector against this server (needs HTTPS, so it
  waits for module `deploy`). Open questions to settle then: does Claude use
  dynamic client registration, and is its callback still
  `/api/mcp/auth_callback` (otherwise add it to `REDIRECT_ALLOWLIST`)? If it needs
  a fixed client, add one pre-registered client from env vars.

## Sync tests

    python -m pytest -q test_sync.py     # offline, about 25 s

They start `python -m anki.syncserver` (shipped in the anki wheel) on localhost and use the
real client against it: round trip between two devices, merge and conflict, wrong password,
unreachable or stalled server, a failed sync before or after a write, full-sync refusal, and
`bootstrap.py` download and upload. They do not cover real AnkiWeb: its rate limits, its
timeouts, its choice of `FULL_UPLOAD`/`FULL_DOWNLOAD` (the local server only says `FULL_SYNC`).

## First real use (do this once, watching)

1. In Anki desktop, make a backup (File, Export, with scheduling) and sync it, so AnkiWeb
   holds what you want to keep.
2. Set `ANKIWEB_USER` / `ANKIWEB_PASS`, then `python bootstrap.py download`.
3. Call `sync` from Claude. Expect `synced` and no errors.
4. Approve a single `add_notes` with one note. Check it on your phone after its sync.
5. Watch for login errors for a day. AnkiWeb may rate-limit logins; the server logs in once
   per process and renews only when AnkiWeb rejects the key.
6. If anything asks for a full sync, the server refuses and says which direction Anki
   suggests. Decide by hand with `bootstrap.py`; the server never does it.

## Safeguards worth knowing

- The collection must exist: run `bootstrap.py download` first. The server never
  creates an empty one (its reads would report "no duplicates"); tools say so instead.
- A call that waits more than 3 s for another one (usually an AnkiWeb sync; a stalled
  AnkiWeb request takes ~31 s to fail) answers "busy, try again" (`LOCK_WAIT` in `core.py`).
- The AnkiWeb login is made once per server process and renewed when AnkiWeb rejects it.
- Card fields accept text formatting, `http(s)`/`mailto` links and local images only:
  no scripts, event handlers, remote images or CSS that loads anything. Cards run
  as HTML with JavaScript on every device.
- Notes are checked for shape before any sync; nothing is coerced or dropped.
  `update_note` refuses an edit that empties the first field, duplicates another
  note or removes every cloze, and a batch of `add_notes` is stored in one operation.
- `find_notes` returns `{total, notes}`, so a duplicate check can tell it saw only part.
- Anonymous OAuth endpoints are bounded: 10 client registrations per IP (IPv6: per /64)
  per hour, at most 100 clients (idle ones evicted, never ones with tokens), 8 KB of
  metadata and 10 redirect URIs per client, 10 parked logins per client. The login
  page names the client and where it redirects; failed logins are logged with the client IP.
- Not built: systemd unit, Caddy block (module `deploy`).

Note: this uses mcp 2.x (`MCPServer`), not the 1.x `FastMCP`. It hides the text
of ordinary exceptions from the model, so `server.py` wraps tools in `surfaced`.
