# anki-claude-bridge

Headless MCP server that lets Claude add and edit notes in my Anki collection,
syncing through AnkiWeb. Spec and boundaries: [GOALS.md](GOALS.md). Other people can use the
same server with their own collection and AnkiWeb account ([SPEC-multi-user.md](SPEC-multi-user.md)).

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
| `ANKIWEB_USER` / `ANKIWEB_PASS` | Owner's AnkiWeb login, in the clear. Optional: without it, enter it on the login page, where it is stored encrypted (see "AnkiWeb logins"). With neither, sync is off | unset |
| `REDIRECT_ALLOWLIST` | Comma-separated OAuth redirect rules (`scheme://host[/path]`; no port = any port, for loopback; a path allows only that path) | `/api/mcp/auth_callback` on claude.ai and claude.com, Perplexity's `/rest/connections/oauth_callback`, loopback |
| `TRUST_PROXY` | `1` = client IP from the last `X-Forwarded-For` entry (set behind the reverse proxy) | unset |
| `USERS_FILE` | TOML file with other people (see "Other people"); mode 600. Unset = owner only | unset |

## How auth works

The server is its own OAuth 2.1 server. Claude registers itself
(dynamic client registration), sends you to `/login`, you enter the owner secret
once, and Claude receives a 1 hour access token plus a 30 day refresh token.
Tokens, codes and login links are stored only as hashes; client secrets are
derived from the owner secret hash and never stored. Failed logins are limited
to 5 per 15 minutes per IP (25 overall). Details: [SPEC-auth.md](SPEC-auth.md).

## AnkiWeb logins

The login page has a collapsed "AnkiWeb login" section. Fill it in once: the server logs in to
AnkiWeb, keeps only AnkiWeb's sync key (never the password, like Anki desktop), and downloads your
collection if the server has none yet. The sync key is encrypted (AES-GCM, `vault.py`) under a
random key that opens only with your login secret or one of your live tokens, which only your
Claude connector holds. The server's files and backups alone cannot decrypt it, and neither can
the admin. Later logins unlock it with your secret; you type the AnkiWeb login again only after
changing your secret or your AnkiWeb password.

A connection made before a login was saved cannot write ("log in again"; in Claude, disconnect
and reconnect the connector). The owner's `ANKIWEB_*` env vars, if set, win over a saved login.

## Other people

Without `USERS_FILE` the server is the owner's alone, exactly as before. With it, each person in
the file logs in with their username and their own secret; the owner leaves the username empty
and uses the owner secret as always. Every token carries its person's `subject`, and tools use
that person's collection (`data/<name>/collection.anki2`) and saved AnkiWeb login.

    python auth.py hash alice           # they type their secret; prints a [users.alice] entry
    # paste it into users.toml (template: deploy/users.example.toml), chmod 600, restart

Someone who is not at your terminal can make the hash on their own computer (Python 3 only),
so their secret never leaves it, and send you what it prints as their `secret_hash`:

    python3 -c "import base64,getpass,hashlib,os;s=getpass.getpass('Secret: ');salt=os.urandom(16);d=hashlib.scrypt(s.encode(),salt=salt,n=2**15,r=8,p=1,maxmem=2**26,dklen=32);b=lambda x:base64.b64encode(x).decode();print(f'scrypt\$32768\$8\$1\${b(salt)}\${b(d)}')"

Clients: Claude (custom connector) and Perplexity (custom remote connector: Streamable HTTP,
OAuth 2.0, no client ID needed). Both register themselves and use the same login page.

They then log in as `alice` and enter their AnkiWeb login; that first login downloads their
collection. The file holds no AnkiWeb logins. The server refuses to start if the file is readable
by others, a name is invalid or `owner`, or it has AnkiWeb keys; two people cannot save the same
AnkiWeb account. Removing someone and restarting cuts their tokens off. The file is read at
startup: restart after changing it. Not built: signup, roles, quotas.

## Status

- Done: six tools, validation, OAuth (clients, login, tokens, rotation, revoke),
  startup guard, tests (core logic and the HTTP auth flow, all offline).
- Done: sync checked offline against Anki's bundled sync server (see "Sync tests").
- Done: running on the VPS at `https://anki.panzeilkamp.com/mcp` (systemd + nginx + Let's
  Encrypt, see [DEPLOY.md](DEPLOY.md)). The Claude connector (web and phone) registers, logs in
  and edits the real collection; real AnkiWeb sync works.
- Done (offline, not yet deployed): other people via `USERS_FILE`, and AnkiWeb logins saved
  encrypted from the login page ([SPEC-multi-user.md](SPEC-multi-user.md)). The owner's env login
  and tokens keep working, checked against a login made by the previously deployed code.
- Still to watch: AnkiWeb-only behaviour the local server cannot show (login rate limits,
  connections that hang). Look at `journalctl -u anki-bridge` for login errors in daily use.

## Sync tests

    python -m pytest -q test_sync.py     # offline, 30-45 s (each login costs the local server ~0.4 s)

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

- The collection must exist: run `bootstrap.py download` first, or save the AnkiWeb login on the
  login page (it downloads into an empty place, never over a collection, never uploads). The
  server never creates an empty one (its reads would report "no duplicates"); tools say so instead.
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
- Deployment (systemd unit, nginx, certificate, checks): `deploy/` and [DEPLOY.md](DEPLOY.md).

Note: this uses mcp 2.x (`MCPServer`), not the 1.x `FastMCP`. It hides the text
of ordinary exceptions from the model, so `server.py` wraps tools in `surfaced`.
