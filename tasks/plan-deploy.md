# Implementation Plan: deploy

Spec: [SPEC-deploy.md](../SPEC-deploy.md) (approved).
Tasks: [todo-deploy.md](todo-deploy.md).

## Overview

Put the finished server on the Hetzner VPS behind the existing nginx, at
`https://anki.panzeilkamp.com/mcp`. There is no new application logic. Work splits into
files I write and test here (unit, nginx block, env template, check script, runbook) and
steps you run on the VPS and report back. Each task ends with something observable.

## Architecture Decisions

- Files live in `deploy/`; `DEPLOY.md` is the single runbook, written in the order it is run.
- Already settled on the VPS (see the spec): nix from apt with the daemon socket running,
  flakes on, user `anki` in `nix-users`, repo cloned to `/srv/anki-bridge/app`, runtime
  confirmed (`anki 26.9.3`, `mcp 2.3.0`).
- Service port is 8765 (8000 belongs to the existing site). nginx and the unit agree on it,
  and the repo is the only place the number is written down besides the env file.
- The unit is started before nginx is touched, and checked on loopback first, so a bug in the
  service and a bug in the proxy are never diagnosed at the same time.
- certbot needs the port-80 block first, so the nginx change is two steps: HTTP block,
  `certbot --nginx`, then the full block.
- The collection is fetched (`bootstrap.py download`) last, after the connector works, because
  it needs the Anki desktop backup and is the only step that touches your real data.
- Secrets exist only in `/srv/anki-bridge/env` (0600). They are never typed on a command line.

## Dependency graph

    T1 repo artifacts + offline checks (here)
       └─ T2 host prep and env file (VPS)
            └─ T3 service up, checked on loopback (VPS)      -- checkpoint
                 └─ T4 nginx + certbot + check.sh (VPS)
                      └─ T5 reboot, crash, update (VPS)      -- checkpoint
                           └─ T6 real Claude connector
                                └─ T7 first real use with the collection
    T8 docs and status (after T7)

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| `ProtectSystem=strict` blocks `nix develop` (writes its cache, `.venv`, talks to the daemon) | Med | T3 starts the unit with hardening on and lists the needed `ReadWritePaths` from the journal; relax only the specific path, never drop the setting wholesale |
| `nix develop` at every start is slow or needs the network | Low | The flake is locked and cached after the first run; T5 measures restart time. If it is bad, switch `ExecStart` to the venv python with the library path |
| New nginx block disturbs `boatsched` | High | Own `server_name`, no `default_server`, `nginx -t` before every reload, `reload` not `restart`, and `boatsched` re-checked after each change |
| Claude's connector rejects dynamic registration or uses a different callback URL | Med | Known open question from `auth`. T6 reads the server log, adjusts `REDIRECT_ALLOWLIST` only with your approval, and records what was seen |
| Cloudflare proxy turned back on | Med | `check.sh` fails if the certificate issuer is not Let's Encrypt (a proxied record would show Cloudflare's); DNS only is part of the runbook |
| First download or write damages the real collection | High | Anki desktop backup first (README "First real use"), `download` only, one note at a time, never `upload` on the VPS |
| Secrets leak into the public repo | High | T1 test scans `deploy/` for secret-looking values; env example has names only |

## Task list

See [todo-deploy.md](todo-deploy.md): T1-T8, checkpoints after T3 and T5.

## Open Questions

- Whether Claude's connector registers dynamically and which callback URL it uses: settled in T6.
