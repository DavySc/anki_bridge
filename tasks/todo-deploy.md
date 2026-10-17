# Tasks: deploy

Spec: ../SPEC-deploy.md · Plan: plan-deploy.md
Who: **[me]** = I write and test it here and push. **[you]** = you run it on the VPS and paste the result.
Before every push: `jj bookmark set main -r @-` (jj does not move `main` by itself).

## Task 1: Repo artifacts and offline checks [me]

**Description:** Write `deploy/anki-bridge.service`, `deploy/nginx-anki-http.conf` (port 80, for certbot) and `deploy/nginx-anki.conf` (the full block), `deploy/env.example`,
`deploy/check.sh`, and `DEPLOY.md` skeleton. Add a pytest that keeps them honest.

**Acceptance criteria:**
- [x] Unit passes `systemd-analyze verify`; runs as `anki`, loopback port 8765, env file, hardening on
- [x] nginx block sets `Host`, `X-Forwarded-For`, `X-Forwarded-Proto`, HTTP/1.1, `proxy_buffering off`, long read timeout, and no `default_server`
- [x] `env.example` lists every variable the startup guard needs, has no values that look like secrets, and omits `ALLOW_INSECURE_NO_AUTH`
- [x] `check.sh` does the checks in the spec and exits non-zero on the first failure; no secrets on its command line
- [x] Test: names in `env.example` match `server.py`'s config; unit and nginx file use the same port as the example

**Verification:**
- [x] `python -m pytest -q` passes (under 40 s)
- [x] `systemd-analyze verify deploy/anki-bridge.service` is clean

**Dependencies:** None
**Files:** `deploy/*`, `DEPLOY.md`, `test_deploy.py`
**Scope:** Medium

## Task 2: Host preparation and env file [you, with my runbook]

**Description:** Capture what you already did by hand in `DEPLOY.md` (nix socket order, flakes,
`nix-users` group, user `anki`, clone), then create the env file. The owner secret is hashed on
the VPS; the plain secret never leaves your head or password manager.

**Acceptance criteria:**
- [ ] `git pull` in `/srv/anki-bridge/app` is current (includes `deploy/`)
- [ ] `nix develop --command python auth.py hash` prints an `OWNER_SECRET_HASH`
- [ ] `/srv/anki-bridge/env` exists, mode 0600, owner `anki`, with the values from `env.example`; `PUBLIC_URL=https://anki.panzeilkamp.com`, `TRUST_PROXY=1`, `PORT=8765`
- [ ] No `ANKIWEB_*` yet if you want the service to start with sync off first (optional)

**Verification:**
- [ ] `stat -c '%a %U' /srv/anki-bridge/env` prints `600 anki`
- [ ] `nix develop --command python -m pytest -q` passes on the VPS

**Dependencies:** Task 1
**Files:** `DEPLOY.md`
**Scope:** Small

## Task 3: Service up, checked on loopback [you]

**Description:** Install the unit, start it, and check it on `127.0.0.1:8765` only, with nginx
untouched. Fix hardening problems here, not later.

**Acceptance criteria:**
- [x] `systemctl enable --now anki-bridge` active, running as `anki`
- [x] `journalctl -u anki-bridge` shows no errors; the missing-collection warning is expected
- [x] `curl -si -X POST http://127.0.0.1:8765/mcp` gives 401 with `WWW-Authenticate`
- [x] `curl -s -H 'Host: anki.panzeilkamp.com' http://127.0.0.1:8765/.well-known/oauth-authorization-server` names `https://anki.panzeilkamp.com`
- [x] `ss -ltn | grep 8765` shows `127.0.0.1` only

**Verification:**
- [x] Any `ReadWritePaths` or hardening change is committed back to `deploy/anki-bridge.service` (with a test if it affects the port or user)

**Dependencies:** Task 2
**Files:** `deploy/anki-bridge.service`, `DEPLOY.md`
**Scope:** Small

## Checkpoint: Service runs
- [x] Task 3 criteria met; `boatsched` unaffected
- [x] Review with human before opening it to the internet

## Task 4: nginx and certificate [you]

**Description:** Install `nginx-anki-http.conf`, run `certbot certonly --nginx -d
anki.panzeilkamp.com` (certonly keeps certbot from rewriting our file), then install the full `nginx-anki.conf`, reload. Never edit the `panzeilkamp.com` blocks.

**Acceptance criteria:**
- [ ] `nginx -t` clean before each reload; `systemctl reload nginx`
- [ ] `https://anki.panzeilkamp.com/.well-known/oauth-protected-resource` serves a valid certificate and the public URL
- [ ] `deploy/check.sh https://anki.panzeilkamp.com` passes
- [ ] `https://panzeilkamp.com` still works; `curl http://49.12.74.253:8765` from outside fails
- [ ] `certbot renew --dry-run` succeeds

**Verification:**
- [ ] The final nginx file in the repo equals what is on the VPS (`diff`)

**Dependencies:** Task 3
**Files:** `deploy/nginx-anki.conf`, `DEPLOY.md`
**Scope:** Small

## Task 5: Reboot, crash, update [you]

**Acceptance criteria:**
- [ ] After `reboot`, the service and `check.sh` come back with no action
- [ ] `kill -9` of the server process: systemd restarts it
- [ ] Update procedure from the spec (`git pull`, restart, `check.sh`) works; restart time recorded

**Verification:**
- [ ] `journalctl -u anki-bridge -b` has no warnings beyond the expected one

**Dependencies:** Task 4
**Files:** `DEPLOY.md`
**Scope:** Small

## Checkpoint: Reachable and durable
- [ ] Tasks 4 and 5 met
- [ ] Review with human before connecting Claude or touching the collection

## Task 6: Real Claude connector [you, me for fixes]

**Description:** Add `https://anki.panzeilkamp.com/mcp` as a custom connector in Claude (web,
then phone). Watch the server log. Record what Claude does.

**Acceptance criteria:**
- [ ] Registration succeeds (dynamic client registration) or the failure is recorded with the request seen in the log
- [ ] The callback URL Claude uses is accepted by `REDIRECT_ALLOWLIST`, or the default is corrected with your approval
- [ ] Login page shows the client name; wrong secret is refused; right secret completes
- [ ] A tool call reaches the server with a valid token; before Task 7 it answers that the collection is missing, which is expected
- [ ] Phone app connects too

**Verification:**
- [ ] SPEC-auth Open Questions 1 and 2 answered in the spec with what was observed
- [ ] If code changed: full suite green, pushed, pulled on the VPS, `check.sh` passes

**Dependencies:** Task 5
**Files:** `SPEC-auth.md`, `server.py`/`auth.py` only if something is wrong
**Scope:** Medium (unknowns)

## Task 7: First real use [you]

**Description:** The checklist from README "First real use", on the real collection.

**Acceptance criteria:**
- [ ] Anki desktop backup made and synced
- [ ] `bootstrap.py download` as `anki` (with `ANKIWEB_*` in the environment), collection appears in `data/owner/`
- [ ] `sync` through Claude returns `synced`
- [ ] One approved `add_notes` with a single note; it shows on the phone after sync
- [ ] No login errors from AnkiWeb in the journal after a day of use

**Verification:**
- [ ] Notes counts on desktop, phone and via `find_notes` agree

**Dependencies:** Task 6
**Files:** `DEPLOY.md`
**Scope:** Small

## Task 8: Docs and status [me]

**Acceptance criteria:**
- [ ] README "Status" and "Run" reflect the deployment; stale "not built" lines removed
- [ ] SPEC-deploy success criteria ticked; CAPABILITY-MAP shows all modules built
- [ ] `tasks/todo.md` review boxes from `auth` ticked or dropped with a note

**Verification:**
- [ ] `python -m pytest -q` passes; pushed

**Dependencies:** Task 7
**Files:** `README.md`, `SPEC-deploy.md`, `CAPABILITY-MAP.md`, `tasks/todo.md`
**Scope:** Small
