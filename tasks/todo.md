# Tasks: auth

Spec: ../SPEC-auth.md · Plan: plan.md
Test command for every task: `python -m pytest -q` (existing `test_core.py` must keep passing).

## Task 1: Provider skeleton, client registration, redirect allow-list

**Description:** Create `auth.py` with `OwnerProvider` backed by SQLite
(schema created on open). Implement `get_client` / `register_client`. Reject
registration whose redirect URIs are not on the allow-list (Claude domains plus
loopback, configurable). Start by reading the 2.3 SDK auth code and confirm the
protocol method list.

**Acceptance criteria:**
- [x] Registered client round-trips through `get_client`; unknown id returns None
- [x] Registration with a disallowed redirect URI raises `RegistrationError`
- [x] Defines the owner subject constant in one place; `Authenticator` protocol and `OwnerAuthenticator` exist
- [x] Allow-list is configurable (env/arg), default covers claude.ai, claude.com, loopback

**Verification:**
- [x] `python -m pytest -q test_auth.py` passes
- [x] Manual: SDK method list noted in a comment/PR if it differs from the spec

**Dependencies:** None
**Files:** `auth.py`, `test_auth.py`
**Scope:** Small

## Task 2: Authorize, login, authorization code

**Description:** `authorize` stores a pending request (10 min, single use) and
returns `/login?req=<id>`. Add owner-secret verification (scrypt hash, constant
time), the `python auth.py hash` helper, the login handler (GET form, POST
verify), a per-IP failure limiter (5 / 15 min), and authorization-code issuance
(5 min, single use, bound to client, redirect_uri, PKCE challenge, resource).
`load_authorization_code` implemented.

**Acceptance criteria:**
- [x] Right secret yields a redirect to the registered redirect_uri with `code` and `state`
- [x] Wrong secret never yields a code; 6th failure within 15 min returns 429
- [x] Pending request and code are single use and expire (injected clock)
- [x] Unknown/expired `req` id never redirects anywhere (no open redirect)
- [x] `subject` from the authenticator is stored on the pending request and the code
- [x] Rate limiter keys on IP plus a global bucket, not the claimed username
- [x] Hash helper output verifies against the typed secret; plaintext is never stored or logged

**Verification:**
- [x] `python -m pytest -q test_auth.py` passes
- [x] Manual: `python auth.py hash` prompts and prints a hash

**Dependencies:** Task 1
**Files:** `auth.py`, `test_auth.py`
**Scope:** Medium

## Task 3: Token exchange, refresh rotation, revoke, verify

**Description:** `exchange_authorization_code` (PKCE verified by the SDK; we bind
and consume the code), opaque access (1 h) and refresh (30 d) tokens stored as
SHA-256 hashes, `load_access_token`, `load_refresh_token`,
`exchange_refresh_token` with rotation and reuse detection (revokes that
client's grant), `revoke_token`. Propagate `resource`.

**Acceptance criteria:**
- [x] Code exchange returns tokens once; second use fails
- [x] Access token verifies until expiry, then returns None
- [x] Refresh rotates; reusing the old refresh token revokes the grant
- [x] Revoked tokens fail verification
- [x] `subject` propagates code -> access/refresh token; refresh-reuse revocation and `revoke` are scoped to the grant (one login), which carries the subject and client
- [x] A test-only second authenticator issues tokens for subject `other` that cannot revoke or refresh the owner's
- [x] DB inspected in a test: no plaintext token, code or secret present

**Verification:**
- [x] `python -m pytest -q test_auth.py` passes

**Dependencies:** Task 2
**Files:** `auth.py`, `test_auth.py`
**Scope:** Medium

## Checkpoint: after Tasks 1-3
- [ ] All tests pass, including unchanged `test_core.py`
- [ ] Provider drives the whole OAuth flow in-process with no HTTP
- [ ] Review with human: token lifetimes, allow-list defaults, anything the SDK forced to change

## Task 4: Wire into server.py, startup guard, HTTP-level tests

**Description:** First, verify that `get_access_token()` is visible inside a tool call (risk in plan). Then configure `MCPServer` with `AuthSettings` (issuer/resource from
`PUBLIC_URL`, `validate_token_resource=True`, DCR and revocation enabled) and the
provider; register `/login` via `custom_route`. Startup refuses without
`PUBLIC_URL` + `OWNER_SECRET_HASH` unless `ALLOW_INSECURE_NO_AUTH=1` on a loopback
host. Confirm the actual MCP endpoint path (spec assumes `/mcp`).

**Acceptance criteria:**
- [x] Unauthenticated `POST /mcp` returns 401 with `WWW-Authenticate` pointing at protected-resource metadata
- [x] Both `/.well-known/` metadata documents served with the public URL
- [x] End-to-end over Starlette TestClient: register -> authorize -> login -> token -> `list_decks` -> refresh -> revoke -> call fails
- [x] Malformed, expired, revoked, wrong-resource tokens all 401 and never reach tool code
- [x] Missing config refuses to start; insecure flag refused on a non-loopback host
- [x] `approved_by_user` guard unchanged
- [x] Tools resolve the Bridge via `bridge_for(subject)` from `get_access_token()`; no module-level Bridge; no subject under auth fails closed
- [x] Test proves the contextvar reaches a tool call (sync tool in a worker thread) and `other` maps to a different Bridge path (`data/other/...`)
- [x] Data path is `data/<subject>/collection.anki2`, `ANKI_COLLECTION` still overrides for the owner

**Verification:**
- [x] `python -m pytest -q` passes (all files)
- [x] Manual: real server process started with auth on (401 + metadata via curl) and refuses to start unsafely
- [x] Manual: MCP Inspector completing the browser flow (needs a human at a browser)

**Dependencies:** Task 3
**Files:** `server.py`, `auth.py`, `test_auth.py` (or `test_server_auth.py`)
**Scope:** Medium (watch the size; split the `bridge_for` seam into 4b if it exceeds ~5 files)

## Task 5: Docs and manual-test checklist

**Description:** Update README (env vars, run commands, status), GOALS.md
(auth no longer "still to be built"), `.envrc`/docs for generating
`OWNER_SECRET_HASH`. Add the real-connector test as an unchecked item to carry
into `deploy`.

**Acceptance criteria:**
- [ ] README documents the multi-user seams and what is intentionally not built
- [ ] README lists `PUBLIC_URL`, `OWNER_SECRET_HASH`, `ALLOW_INSECURE_NO_AUTH`, redirect allow-list setting
- [ ] GOALS.md and README status updated; "do not expose the port" note replaced with accurate guidance
- [ ] Real Claude connector test recorded as pending in `deploy`'s inputs

**Verification:**
- [ ] Follow the README from a clean shell and reach a working local authenticated run

**Dependencies:** Task 4
**Files:** `README.md`, `GOALS.md`, `server.py` docstring
**Scope:** Small

## Checkpoint: Complete
- [ ] All SPEC-auth.md success criteria met
- [ ] `python -m pytest -q` green
- [ ] Review with human, then start `SPEC-sync-verification.md` / `SPEC-deploy.md`
