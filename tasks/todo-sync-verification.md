# Tasks: sync-verification

Spec: ../SPEC-sync-verification.md · Plan: plan-sync-verification.md
Test command for every task: `python -m pytest -q` (all 114 existing tests must keep passing).
Each task: write the failing test first, then the smallest `core.py` change.

## Task 1: Local sync server fixture and baseline round trip

**Description:** Add `test_sync.py` with a module-scoped fixture that starts
`python -m anki.syncserver` (free port, temp base dir, one user), waits until it
answers, and tears it down. Helpers create device collections and a `Bridge`
pointed at the server via `SyncConfig(endpoint=...)`. First scenario is a
characterization test: add through the bridge, sync a second collection, see the
note (required behaviour 5, H1 baseline).

**Acceptance criteria:**
- [x] Fixture starts, serves, stops cleanly; skips with a clear reason if the server cannot start
- [x] `add_notes` then phone sync shows the same fields and tags; `update_note` likewise
- [x] `sync_before` and `sync_after` say "synced" (finding: `required` is NO_CHANGES even after a push, so the old "no changes" label was wrong)

**Verification:**
- [x] `python -m pytest -q test_sync.py` passes (about 5 s; the server starts per test)
- [x] No leftover server process after the run (`pgrep -f anki.syncserver`)

**Dependencies:** None
**Files:** `test_sync.py`
**Scope:** Small

## Task 2: Readable sync errors; pre-sync failure writes nothing

**Description:** Map `SyncError` (AUTH and others) and `NetworkError` in
`_sync` to `BridgeError` messages written for Claude. Passwords never appear in
messages. Prove a failing pre-sync leaves the collection untouched.

**Acceptance criteria:**
- [x] Wrong password: "AnkiWeb login failed..." naming the env vars, no password in text
- [x] Server down (connection refused): "Could not reach AnkiWeb (network). Nothing was written."
- [x] `add_notes` and `update_note` with a failing pre-sync leave note count and modification times unchanged
- [x] The message reaches the model through the server wrapper (one test via `surfaced`)
- [x] `sync` tool raises the same readable errors

**Verification:**
- [x] `python -m pytest -q test_sync.py` passes
- [x] Time of the refused-connection case noted: 0.01 s. A blackhole (silent drop) is not testable offline

**Dependencies:** Task 1
**Files:** `core.py`, `test_sync.py`
**Scope:** Small

## Task 3: Post-write sync failure keeps the result

**Description:** When the write succeeded but the sync after it fails, return
normally with ids or the updated note and `sync_after = "failed: <reason>; run sync"`.
Failure injected on the second `_sync` call only (counter wrapper).

**Acceptance criteria:**
- [x] `add_notes` returns the added ids with a failed `sync_after`; does not raise
- [x] `update_note` returns the note with a failed `sync_after`
- [x] A following `sync` call pushes the write; the phone then sees it
- [x] Retrying the same `add_notes` is rejected as a duplicate (documented, expected)
- [x] A `SyncRequired` after the write is also reported in `sync_after`, not raised

**Verification:**
- [x] `python -m pytest -q test_sync.py` passes

**Dependencies:** Task 2
**Files:** `core.py`, `test_sync.py`
**Scope:** Small

## Checkpoint: after Tasks 1-3
- [x] All tests pass (128)
- [x] The data-loss gap (G2) and the unreadable-error gap (G1) are closed
- [ ] Review with human before the remaining scenarios

## Task 4: Full-sync refusal names the direction

**Description:** Force full-sync requirements locally (schema change on the phone
side, or on ours) and observe which `required` values occur. Raise
`SyncRequired` with the direction Anki asked for. Prove nothing is written and no
code path outside `bootstrap.py` reaches `full_upload_or_download`.

**Acceptance criteria:**
- [ ] Each reachable full-sync kind (FULL_SYNC / FULL_UPLOAD / FULL_DOWNLOAD) raises `SyncRequired` naming the direction
- [ ] After the refusal the local collection is unchanged and the server is unchanged
- [ ] A test fails if `core.py` or `server.py` references `full_upload_or_download`
- [ ] H2 recorded in the spec as confirmed or refuted, with the observed enum values

**Verification:**
- [ ] `python -m pytest -q test_sync.py` passes

**Dependencies:** Task 1
**Files:** `core.py`, `test_sync.py`, `SPEC-sync-verification.md`
**Scope:** Medium

## Task 5: Merge and conflict behaviour

**Description:** Edits on the phone merge before our write (H1), and a note edited
on both sides resolves and is returned by `update_note` (H3).

**Acceptance criteria:**
- [ ] Phone adds a note, bridge `add_notes` syncs before writing and both notes end up on both sides
- [ ] Same note edited on both sides: `update_note` succeeds and returns the post-merge note
- [ ] H1 and H3 recorded in the spec as confirmed or refuted

**Verification:**
- [ ] `python -m pytest -q test_sync.py` passes

**Dependencies:** Task 1
**Files:** `test_sync.py`, `SPEC-sync-verification.md` (core.py only if a bug appears)
**Scope:** Small

## Task 6: bootstrap.py against the local server

**Description:** Run `bootstrap.py download` and `upload` as subprocesses against the
local server with `ANKIWEB_ENDPOINT`. Check the upload confirmation guard.

**Acceptance criteria:**
- [ ] `download` into an empty path produces a collection with the server's notes
- [ ] `upload` without typing `overwrite` aborts and changes nothing on the server
- [ ] `upload` with `overwrite` replaces the server's collection
- [ ] H4 recorded: `server.py` has no path to either direction (covered by Task 4's test)

**Verification:**
- [ ] `python -m pytest -q test_sync.py` passes

**Dependencies:** Task 1
**Files:** `test_sync.py`, `bootstrap.py` (only if a bug appears)
**Scope:** Small

## Task 7: Docs and first-real-use checklist

**Description:** Document the local-server tests and write the supervised first-use
checklist that `deploy` will run, since real AnkiWeb is untested. Update README
status, the spec's findings, and GOALS if behaviour changed.

**Acceptance criteria:**
- [ ] README explains how to run the sync tests and what they do not cover
- [ ] First-use checklist: Anki desktop backup/export, `bootstrap.py download`, no-change `sync`, one approved single-note `add_notes`, confirm on the phone, then watch for login rate-limit errors
- [ ] Spec "Findings" section records G1-G5 and H1-H5 outcomes (H5 open)

**Verification:**
- [ ] Follow the README from a clean shell: sync tests run offline

**Dependencies:** Tasks 1-6
**Files:** `README.md`, `SPEC-sync-verification.md`, `GOALS.md`
**Scope:** Small

## Checkpoint: Complete
- [ ] All SPEC-sync-verification.md success criteria met
- [ ] Full suite green, runs offline in under 30 s
- [ ] Review with human, then `SPEC-deploy.md`
