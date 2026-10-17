# Spec: sync-verification (module `sync-verification`)

Status: APPROVED (no AnkiWeb smoke test, see Open Questions). Part of [CAPABILITY-MAP.md](CAPABILITY-MAP.md).
Depends on: nothing. Required by: `deploy`.

## Objective

Prove that the sync behaviour in `core.py` and `bootstrap.py` works against a
real Anki sync server, and fix what the proof shows is wrong. GOALS.md promises
"sync before and after every write" and "never run a full sync automatically".
Today that is only tested with sync switched off.

Success looks like: an offline test suite that runs the real sync protocol against
a local sync server and passes. There is no test against real AnkiWeb (the owner
has no throwaway account and will not use the real one for tests); the first real
use is a supervised checklist owned by `deploy`. A failed sync never loses a successful write's result, never leaves
Claude with an unreadable error, and never does a full upload or download.

Non-goals: media sync, a self-hosted sync server in production, conflict
resolution UI, background or scheduled sync, retries beyond one attempt.

## What reading the code and a probe already show

Probed against Anki's bundled sync server (see Test harness). These are the gaps
this module must close; each becomes a failing test first.

| # | Finding | Why it matters |
|---|---|---|
| G1 | A bad password raises `anki.errors.SyncError(kind=AUTH)`, a dead server raises `NetworkError`. Neither is a `BridgeError`, so mcp 2.x hides the text and Claude sees only "Error executing tool". | Claude cannot tell the user "AnkiWeb login failed" or "offline". |
| G2 | In `add_notes` and `update_note` the write is committed locally, then the post-write `_sync` runs. If that sync fails, the exception replaces the result, so the note ids are lost. | The user is told it failed, retries, and the retry is rejected as a duplicate. Worse, the note exists on the server but not on the phone, silently. |
| G3 | `_sync` treats every `required` value other than NO_CHANGES and NORMAL_SYNC as "full sync". The enum also has FULL_SYNC, FULL_UPLOAD and FULL_DOWNLOAD. | Probably correct, but unverified, and the message does not say which direction Anki wants. |
| G4 | `sync_login` runs on every call. | AnkiWeb may rate-limit logins; each tool call that writes logs in twice. Unmeasured. |
| G5 | `ANKIWEB_ENDPOINT` already exists in `SyncConfig`, so a local server can stand in for AnkiWeb with no code change. | Makes the whole module testable offline. |

## Hypotheses to confirm or refute with tests

- H1: a normal sync after edits on another device merges them, and our write then
  syncs cleanly (`sync_before` = "synced").
- H2: when the other side made a schema change, the pre-sync returns a full-sync
  requirement, raises `SyncRequired`, and nothing is written locally.
- H3: a note edited on both sides is resolved by Anki (last modification wins) and
  `update_note` still returns the merged note.
- H4: `bootstrap.py download` and `upload` work against the local server and
  `server.py` never calls them.
- H5: login every call is acceptable on AnkiWeb. Cannot be tested offline; stays an
  open risk, watched during the supervised first use.

## Tech Stack

Python 3.13, `anki==26.9.3` (client and bundled sync server), pytest. No new
dependencies.

## Test harness

`python -m anki.syncserver` is Anki's own sync server, bundled in the wheel:

    SYNC_USER1=user:pass SYNC_BASE=<tmpdir> SYNC_HOST=127.0.0.1 SYNC_PORT=<free> python -m anki.syncserver

A pytest fixture starts it as a subprocess on a free port with a temp base
directory and waits until `/sync/hostKey` answers, then tears it down. The
endpoint URL needs the trailing slash (`http://127.0.0.1:PORT/`). Two collections
play "server side" and "phone" (device B) so conflicts are real.

## Commands

    Test (all, offline):  python -m pytest -q
    Sync tests only:      python -m pytest -q test_sync.py
    Local server by hand: SYNC_USER1=u:p SYNC_BASE=$(mktemp -d) SYNC_PORT=8091 python -m anki.syncserver

## Project Structure

    core.py             error mapping, post-write sync failure handling (changes)
    test_sync.py        local-server fixture and sync scenarios (new)
    bootstrap.py        unchanged unless H4 finds a bug
    README.md           documents the sync tests and the first-real-use checklist

## Findings so far (updated as tasks land)

- **G6 (new, task 1): the old status labels lied.** `sync_collection` returns
  `required = NO_CHANGES` after every completed normal sync, even when it just pushed
  notes; `NORMAL_SYNC` never appeared in any scenario. `required` only says whether a
  *full* sync is needed. A completed sync now reports `"synced"`; `"no changes"` is gone.
- **G1 confirmed and fixed (task 2).** Bad password raises `SyncError(kind=AUTH)`, a dead
  or refused server raises `NetworkError`; `SyncErrorKind` only has AUTH and OTHER. All
  become `BridgeError` text, with the password scrubbed. A refused connection fails in
  0.01 s; a silently dropped one is untestable offline (timeout question stays open).
- **G2 confirmed and fixed (task 3).** The write was committed, the post-sync raised, and
  the ids were lost. Now the call returns normally with `sync_after = "failed: ..."`.
- **Writes say "Nothing was written."** on a failed pre-sync, but the full-sync message
  itself no longer claims it (it is also used after a write).
- **Silent drop measured (review, 2026-10-17).** A server that accepts and never answers is
  testable offline (a socket that never replies): each AnkiWeb request gives up after ~31 s,
  holding the collection. Other calls now wait at most `LOCK_WAIT` (3 s) and answer "busy".
- **G4 addressed.** The login key is cached per `Bridge` and renewed once when AnkiWeb
  rejects it (the local server answers a stale key like a wrong password, `kind=AUTH`).
- **G7 (new): a write in the same millisecond as the sync before it was not sent.** Anki
  skips a sync when the collection's `mod` equals the server's (set at the last sync). A
  write right after the pre-sync can land in that millisecond; the post-sync then reported
  "synced" without sending it (1 in ~6 round trips once logins stopped taking 300 ms). Fixed
  by moving `col.mod` past the last sync when notes or cards are unsent. A clock behind
  AnkiWeb's is not affected: only equality skips.
- **H2 confirmed (task 4).** A schema change on either side, on both, an empty server and
  an empty local collection all give `required = FULL_SYNC` (2) from the bundled server.
  `FULL_UPLOAD` and `FULL_DOWNLOAD` never appeared, so they are tested with a faked
  response; the message names the direction when Anki gives one and otherwise says the
  choice is the human's. Nothing is written locally or on the server in any case, and a
  test fails if `core.py` or `server.py` mentions `full_upload_or_download` (H4's server half).
- **H1 confirmed (task 5).** A note added on the phone and synced first arrives in the
  bridge's pre-sync; after `add_notes` both devices hold both notes.
- **H3 confirmed (task 5), with a caveat.** For a note edited on both sides, Anki keeps the
  later edit field-wise whole-note: the phone's later `Back` won and the bridge's tag change
  from `update_note` survived. Note modification times have one-second resolution, so two
  edits within the same second tie, and the test sets the phone's time later to be exact.
  Only edits made outside the bridge (e.g. Anki desktop on the same file) can conflict,
  because the bridge syncs before every write.
- **H4 confirmed (task 6).** `bootstrap.py download` creates the parent directory and the
  collection; `upload` without typing `overwrite` aborts and leaves the server unchanged;
  with `overwrite` it replaces the server's collection.
- **Suite time.** One sync server for the session; every login still costs the server
  about 0.4 s of password hashing, which now dominates the sync tests.

## Required behaviour (what the tests assert)

1. **Readable sync errors (G1).** Wrong credentials, unreachable server, timeout and
   any other `SyncError` become `BridgeError` with a message for Claude and the
   user, e.g. "AnkiWeb login failed. Check ANKIWEB_USER and ANKIWEB_PASS on the
   server." / "Could not reach AnkiWeb (network). Nothing was written." The
   password never appears in a message or log line.
2. **Pre-sync failure writes nothing.** If the sync before a write fails or demands a
   full sync, `add_notes` and `update_note` raise and the collection is unchanged
   (note count, usn, modification times).
3. **Post-sync failure keeps the result (G2).** If the write succeeded and the sync
   after it fails, the call returns normally with the added ids or updated note,
   `sync_before` as usual, and `sync_after` = `"failed: <reason> Run sync to retry."` (for a full-sync requirement: `"failed: <reason>"`, since retrying cannot help). It
   does not raise. A later `sync` tool call pushes the pending write.
4. **Full sync is refused, never done (G3).** FULL_SYNC, FULL_UPLOAD and FULL_DOWNLOAD
   each raise `SyncRequired` naming the direction Anki asked for. Neither
   `core.py` nor `server.py` can reach `full_upload_or_download`. A test greps the
   call graph (the only caller is `bootstrap.py`).
5. **Round trip.** Add via the bridge, sync, then open a second collection (the phone),
   sync, and see the note with the same fields and tags. Same for `update_note`.
6. **No sync configured** stays a clean "sync not configured" with the write still
   done, as today.
7. **`sync` tool** returns "no changes", "synced", or raises per rule 1 and 4.

## Code Style

Match `core.py`: small methods, messages written for Claude, no MCP imports.

    def _sync(self, col: Collection) -> str:
        try:
            ...
        except SyncError as e:
            if e.kind == SyncErrorKind.AUTH:
                raise BridgeError("AnkiWeb login failed. ...") from e
            raise BridgeError(f"AnkiWeb sync failed: {e}") from e
        except NetworkError as e:
            raise BridgeError("Could not reach AnkiWeb (network).") from e

## Testing Strategy

- Level: integration. Real Anki client, real Anki sync server, real protocol,
  temporary directories. No mocks of Anki.
- One scenario per hypothesis and per required behaviour above; each starts red.
- Failure injection without mocks: stop the server mid-test for network errors,
  use a wrong password for auth errors, change a note type on device B and sync it
  to force a full-sync requirement.
- The suite must stay fast (< 30 s) and need no network or account.
- No automated test touches real AnkiWeb. Real AnkiWeb is covered by a supervised
  first-use checklist (README, run in `deploy`): export a backup from Anki desktop,
  `bootstrap.py download`, a no-change `sync`, then one approved single-note
  `add_notes`, then confirm it on the phone.
- Known residual risk: behaviour that only AnkiWeb shows (login rate limits, its
  timeouts, its full-sync rules) is not covered. H5 stays unanswered.

## Boundaries

- Always: run sync tests against the local server; use temporary collections;
  keep error messages free of credentials; run the full suite before committing.
- Ask first: running anything against a real AnkiWeb account (never as an automated or
  test step), adding a
  dependency, changing the shape of a tool result beyond the `sync_after` string,
  caching the sync host key on disk.
- Never: run any test against the user's real account or
  collection; call `full_upload_or_download` from `core.py` or `server.py`;
  delete notes; swallow a sync failure without telling the caller.

## Success Criteria

- [ ] Local sync server fixture starts, serves, and stops cleanly in the test run.
- [ ] G1: wrong password, dead server and unreachable endpoint each give a readable
      `BridgeError`, and the text reaches the model through the server wrapper.
- [ ] G2: post-write sync failure returns the ids and a failed `sync_after`; a later
      `sync` pushes the write; no duplicate is created by retrying.
- [ ] G3: all three full-sync kinds raise `SyncRequired` with the direction; nothing
      is written; no call path reaches `full_upload_or_download` outside `bootstrap.py`.
- [ ] H1, H2, H3, H4 each confirmed or refuted by a test, with the finding recorded
      in this spec.
- [ ] Round trip add and update visible on the second collection.
- [ ] Existing 114 tests still pass.
- [ ] README has the first-real-use checklist and states that real AnkiWeb is untested
      (H5 open).

## Open Questions

Decided:
1. No throwaway AnkiWeb account. No real-AnkiWeb test; supervised first use instead.
2. A failed post-write sync returns success with `sync_after: "failed: ...; run sync"`
   (approved).

3. The sync host key is cached in memory (decided in review, before first real use).
4. No timeout on the sync itself (Anki gives up after ~31 s per request); instead other
   calls wait at most 3 s for the collection and answer "busy".
