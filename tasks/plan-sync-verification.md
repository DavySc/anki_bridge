# Implementation Plan: sync-verification

Spec: [SPEC-sync-verification.md](../SPEC-sync-verification.md) (approved).
Tasks: [todo-sync-verification.md](todo-sync-verification.md).
(Separate files because `tasks/plan.md` and `tasks/todo.md` still hold the finished
`auth` plan, which has a few unticked review boxes. Rename or merge them as you prefer.)

## Overview

Prove sync works against a real Anki sync server, entirely offline, using Anki's
bundled `python -m anki.syncserver`. Every task starts with a failing test for one
gap or hypothesis from the spec, then makes the smallest change in `core.py`.
No test touches real AnkiWeb.

## Architecture Decisions

- One fixture, one file: `test_sync.py` starts the local server once per module
  (subprocess, free port, temp base dir, readiness poll) and hands out two
  collections, "server side" and "phone".
- Tests are integration tests with the real Anki client and server. The only
  injection is failing the second `_sync` call by wrapping `Bridge._sync` with a
  counter, because killing the server exactly between the pre- and post-write sync
  is not controllable. Real Anki still performs the first sync.
- `core.py` changes stay inside `_sync` and the two write methods. No change to the
  tool signatures or the MCP layer.
- Errors are mapped once, in `_sync`, so all four callers get readable messages.

## Dependency graph

    T1 fixture + baseline round trip
       ├─ T2 readable errors, pre-sync failure writes nothing   (G1, rule 2)
       │    └─ T3 post-write sync failure keeps the result      (G2, rule 3)
       ├─ T4 full-sync refusal with direction                    (G3, H2)
       ├─ T5 merge and conflict                                  (H1, H3)
       └─ T6 bootstrap download/upload                           (H4)
    T7 docs, first-real-use checklist (needs all)

T2-T6 only depend on T1; the order is by risk (data-loss gap G2 first).

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Local server behaves differently from AnkiWeb | High | Accepted and documented: same protocol and client, but AnkiWeb-only behaviour (rate limits, timeouts, size limits) is untested. Mitigated by the supervised first use in `deploy` and an Anki desktop backup before it |
| Bundled sync server flaky or slow to start | Med | Readiness poll with timeout, `skip` with a clear reason if it cannot start, never a hang. Suite budget under 40 s |
| Forcing a full-sync requirement is fiddly | Med | Schema change on device B (modify a note type) then sync; T4 first discovers which enum value each scenario yields |
| Python-side test of "no path reaches full upload" is brittle | Low | A small AST/grep test limited to `core.py` and `server.py`, failing with the offending line |
| Fixing G2 changes tool output shape | Low | Only the existing `sync_after` string changes; spec marks any other shape change as ask-first |

## Task list

See [todo-sync-verification.md](todo-sync-verification.md): T1-T7, checkpoints after T3 and at the end.

## Open Questions

- Host-key caching and sync timeouts: decided only if first real use shows a need.
