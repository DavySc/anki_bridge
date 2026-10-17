# Spec: deploy (module `deploy`)

Status: DRAFT, awaiting review. Part of [CAPABILITY-MAP.md](CAPABILITY-MAP.md).
Depends on: `auth` (built), `sync-verification` (built). Required by: nothing.

## Objective

Run the bridge on the Hetzner VPS (`pzk-server`, Ubuntu 26.04, 49.12.74.253) at
`https://anki.panzeilkamp.com/mcp`, so Claude (web, desktop, phone) can add it as a
custom connector, and nobody else can use it. The code is done; this module is
configuration, a runbook, and the first supervised real-world checks.

Success looks like: I follow `DEPLOY.md` once on the VPS, the service survives a
reboot, Claude's connector completes the login, a single approved `add_notes` shows up
on my phone after AnkiWeb sync, and the server still refuses everything without a token.

Non-goals: CI/CD, containers, automated backups of the collection (AnkiWeb is the
backup, decided in the capability map), monitoring beyond systemd and journald,
multi-user, a second host, a self-hosted sync server.

## What is already known

- Caddy already runs on this VPS and serves another app (`panzeilkamp.com`). Add one
  site block; do not replace its config.
- DNS: `anki` A record to 49.12.74.253, now **DNS only** in Cloudflare (confirmed), so Caddy
  gets its own certificate and the client IP header is real.
- You ran `sudo apt install nix` as root. The post-install step printed
  `Could not execute systemctl`, so the nix daemon may not be running. Flakes also need
  enabling in `/etc/nix/nix.conf`. Both are checked in the runbook before relying on nix.
- Runtime: the app needs Python 3.13 and `anki==26.9.3` plus `mcp` 2.3 from PyPI
  (`requirements.txt`). Ubuntu 26.04's own Python is probably newer than 3.13, and the
  anki wheel is only known to work on 3.13. The flake already pins 3.13, so the
  service runs inside it (see Design).
- Start-up guard (auth): the server refuses to start without `PUBLIC_URL` and
  `OWNER_SECRET_HASH`. Secrets come from an env file, never the repo.

## Design

- **User and paths.** A dedicated unprivileged user `anki`, home `/srv/anki-bridge`:
  `app/` (the repo checkout), `data/` (collection, `auth.sqlite3`), `env` (secrets,
  mode 0600, owned by `anki`). The service never runs as root.
- **Getting the code.** `git clone` of the repo into `app/`, updated by `git pull` then
  restart. The remote is public, so no deploy key is needed, and that also means nothing secret may ever be committed (checked in the plan).
- **Runtime.** `nix develop` from the flake builds `.venv` on first use. The unit runs
  `nix develop --command python server.py` in `app/`. One command, same environment as
  development, nothing installed globally.
- **systemd unit** `anki-bridge.service`: `User=anki`, `EnvironmentFile=…/env`,
  `Restart=on-failure`, `WorkingDirectory=…/app`, hardening (`NoNewPrivileges`,
  `ProtectSystem=strict` with `ReadWritePaths` for `data/`, `PrivateTmp`,
  `ProtectHome`). Binds `127.0.0.1:8000` only.
- **Caddy site block** for `anki.panzeilkamp.com`: `reverse_proxy 127.0.0.1:8000`, and
  nothing else exposed. `TRUST_PROXY=1` in the env file, because the server takes the
  client IP from the last `X-Forwarded-For` entry that Caddy appends.
- **Env file** (template in `deploy/env.example`, no real values): `PUBLIC_URL`,
  `OWNER_SECRET_HASH`, `DATA_DIR`, `ANKIWEB_USER`, `ANKIWEB_PASS`, `TRUST_PROXY=1`,
  `HOST=127.0.0.1`, `PORT=8000`. `ALLOW_INSECURE_NO_AUTH` must be absent.
- **One-time bootstrap**: as user `anki`, `bootstrap.py download`, after the Anki
  desktop backup and sync described in README "First real use".
- **Update procedure**: `git pull`, `systemctl restart anki-bridge`, run the check script.
  A changed `OWNER_SECRET_HASH` makes Claude re-register, which is expected.
- **Check script** `deploy/check.sh <url>`: unauthenticated `POST /mcp` returns 401 with
  a `WWW-Authenticate` header; both `/.well-known/` documents are served and name the
  public URL; port 8000 is not reachable from outside. Used after every deploy.

## Commands

    Validate unit:   systemd-analyze verify deploy/anki-bridge.service
    Validate Caddy:  caddy validate --config deploy/Caddyfile.snippet   # if caddy is installed
    Run tests:       python -m pytest -q                                # unchanged, offline
    On the VPS:      see DEPLOY.md
    After deploy:    deploy/check.sh https://anki.panzeilkamp.com

## Project Structure

    DEPLOY.md                    runbook, in the order to run it, with expected output
    deploy/anki-bridge.service   systemd unit
    deploy/Caddyfile.snippet     site block to add to the existing Caddyfile
    deploy/env.example           variable names and comments, no secrets
    deploy/check.sh              post-deploy checks (curl only)

## Code Style

Shell scripts: `set -euo pipefail`, one purpose each, no secrets on the command line
(they would appear in `ps` and shell history; read them from the env file or a prompt).
The runbook shows each command with what a good result looks like.

## Testing Strategy

Little application logic, so most checks are runtime, run by me on the VPS:

- Offline, in this repo: `systemd-analyze verify` on the unit, and a small pytest that
  the env example lists every required variable the startup guard names and contains no
  value that looks like a secret.
- On the VPS, in order: nix works as `anki`; service starts and `journalctl` shows no
  warnings; `check.sh` passes; reboot and `check.sh` again.
- Real connector (the open questions from SPEC-auth): add the URL in Claude, confirm
  dynamic client registration works and the callback URL is accepted, log in, call
  `list_decks`, then do the README "First real use" checklist including the phone.
- Negative, from outside the VPS: `curl http://49.12.74.253:8000` fails; the Host header
  of another name gets rejected.

## Boundaries

- Always: run as `anki`, not root; keep secrets only in the 0600 env file; run
  `check.sh` after every deploy; do the Anki desktop backup before the first download.
- Ask first: opening any other port, touching the existing Caddy config beyond adding
  one block, enabling the Cloudflare proxy, changing `REDIRECT_ALLOWLIST`, automating
  anything that talks to AnkiWeb (cron sync).
- Never: run the server as root; put `OWNER_SECRET_HASH`, the AnkiWeb password or a token
  in the repo, a unit file, a command line or a log; set `ALLOW_INSECURE_NO_AUTH`; run
  `bootstrap.py upload` on the VPS; let the service do a full sync.

## Success Criteria

- [ ] `https://anki.panzeilkamp.com` has a valid certificate; `check.sh` passes.
- [ ] Service is enabled, runs as `anki`, restarts after a crash, and is back after a reboot.
- [ ] Port 8000 is unreachable from outside; the existing site on `panzeilkamp.com` still works.
- [ ] Claude's connector completes registration and login; `list_decks` returns my decks.
- [ ] One approved `add_notes` appears on the phone after sync; `sync` reports `synced`.
- [ ] No secret in the repo or `ps` output; env file is 0600 and owned by `anki`.
- [ ] README "Status" and SPEC-auth's connector questions updated with what was observed.

## Decisions

- Code reaches the VPS by `git clone` from the public remote (URL still to give).
- Keep the apt-installed nix if `nix develop` works there; otherwise switch installers.
- `anki` record is DNS only.

## Open Questions

1. What is the public remote URL?
2. How is Caddy installed? `systemctl status caddy` says the unit does not exist, so it is
   not the apt package. Candidates: a docker container, a binary started some other way,
   or a differently named unit. This decides where the site block goes and how it is
   reloaded. Run on the VPS: `ps aux | grep -i [c]addy`, `docker ps`, `which caddy`,
   `ss -ltnp | grep -E ':80|:443'`.
3. Is there an AAAA record for `anki.panzeilkamp.com`? If so it must point at this host
   (`2a01:4f8:c015:19f5::1`).
