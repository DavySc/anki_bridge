# Implementation Plan: auth

Spec: [SPEC-auth.md](../SPEC-auth.md) (approved). Map: [CAPABILITY-MAP.md](../CAPABILITY-MAP.md).
Tasks tracked in [tasks/todo.md](todo.md).

## Overview

Make `server.py` a single-user OAuth 2.1 authorization + resource server using
the mcp 2.3 SDK's `OAuthAuthorizationServerProvider`. We write only the provider
(SQLite storage, owner-secret login, token lifecycle); the SDK supplies the
protocol endpoints and bearer middleware.

## Architecture Decisions

- One new module `auth.py`; `core.py` is untouched. Keeps auth testable without
  Anki and Anki testable without auth.
- Provider is built bottom-up in vertical slices that each have tests:
  clients -> authorize/login -> tokens -> wiring. Each slice is exercised through
  the provider API first, and through HTTP only once wired (task 4).
- stdlib only: `sqlite3`, `hashlib.scrypt`, `secrets`, `hmac.compare_digest`.
- Auth is fail-closed: missing config stops startup; the only escape hatch is
  `ALLOW_INSECURE_NO_AUTH=1` on a loopback bind.
- Multi-user is not built, but four seams are (GOALS.md "Multi-user readiness"):
  `subject` on all grants/tokens, an `Authenticator` interface, `bridge_for(subject)`
  instead of a global Bridge, and per-subject data paths. Proven by a test-only
  second authenticator, so the seams are verified, not just claimed.
- Time is injected (a `now()` callable) so expiry tests do not sleep.

## Dependency graph

    T1 clients + redirect allow-list
       └─ T2 authorize -> login -> code   (needs clients, owner secret)
            └─ T3 token exchange / refresh / revoke / verify
                 └─ T4 wire into server.py, startup guard, HTTP tests
                      └─ T5 docs + manual-test checklist

Linear on purpose: each step needs the previous one's data model. No safe
parallelism; the tasks are small enough that it does not matter.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| SDK 2.3 provider/route API differs from the 1.x docs I know | Med | T1 starts by reading `mcp/server/auth/*` and writing one failing flow test; adjust the plan after T1 if needed |
| `validate_token_resource=True` rejects tokens if `resource` is not propagated | Med | Store and return `resource` on codes and tokens; covered by a T3 test and the T4 HTTP flow |
| Claude's real client behaves differently (DCR, redirect URIs) | High | Cannot be tested until `deploy`. Redirect allow-list is a config list, and a fixed pre-registered client is a small fallback (spec open Q1/Q2) |
| `custom_route` login page bypasses bearer auth by design; mistakes there are the main attack surface | High | Single-use opaque request id, constant-time compare, rate limit, no open redirect (redirect only to the registered redirect_uri); adversarial tests in T2 |
| `get_access_token()` contextvar may not reach sync tool functions run in a worker thread | High | Verify in T4 first, with a test that a tool sees the caller's subject; if not, make tools async or pass subject explicitly |
| Anki collection and auth DB share `data/` | Low | Separate file, `data/` already gitignored |

## Task list

See [todo.md](todo.md): T1-T5 with a checkpoint after T3 and at the end.

## Open Questions (carried from the spec, resolved at the real test)

1. DCR vs pre-registered client in the Claude connector.
2. Exact Claude callback URLs for the allow-list.
