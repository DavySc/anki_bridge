# Spec: sync-verification (module `sync-verification`)

Status: DRAFT, awaiting review. Part of [CAPABILITY-MAP.md](CAPABILITY-MAP.md).
Depends on: nothing. Required by: `deploy`.

## Objective

Prove that the sync behaviour in `core.py` and `bootstrap.py` works against a
real Anki sync server, and fix what the proof shows is wrong. GOALS.md promises
"sync before and after every write" and "never run a full sync automatically".
Today that is only tested with sync switched off.

Success looks like: an offline test suite that runs the real sync protocol against
a local sync server and passes, plus one manual run against a throwaway AnkiWeb
account. A failed sync never loses a successful write's result, never leaves
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
- H5: login every call is acceptable on AnkiWeb (measured once, manually).

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
    AnkiWeb smoke (manual, throwaway account only):
        ANKIWEB_USER=... ANKIWEB_PASS=... python smoke_ankiweb.py

## Project Structure

    core.py             error mapping, post-write sync failure handling (changes)
    test_sync.py        local-server fixture and sync scenarios (new)
    smoke_ankiweb.py    one-shot real-AnkiWeb check, prints a report (new)
    bootstrap.py        unchanged unless H4 finds a bug
    README.md           documents the smoke test and results

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
   `sync_before` as usual, and `sync_after` = `"failed: <reason>; run sync"`. It
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
- Manual: `smoke_ankiweb.py` against a throwaway AnkiWeb account, run once and
  again after any change to `core.py` sync code. Its report is pasted into the
  README Status. It checks login, no-op sync, add-then-sync, and visibility from a
  second collection that downloaded the account.

## Boundaries

- Always: run sync tests against the local server; use temporary collections;
  keep error messages free of credentials; run the full suite before committing.
- Ask first: running anything against a real AnkiWeb account, adding a
  dependency, changing the shape of a tool result beyond the `sync_after` string,
  caching the sync host key on disk.
- Never: run the smoke test or any test against the user's real account or
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
- [ ] `smoke_ankiweb.py` run once on a throwaway AnkiWeb account; report in README;
      H5 answered.

## Open Questions

1. Do you have a throwaway AnkiWeb account (free, a separate email)? Without one the
   real-AnkiWeb check is skipped and the local server is the only evidence. I
   recommend making one; it takes two minutes.
2. For a failed post-write sync, is returning success with `sync_after: "failed: ..."`
   the right call, versus raising? I recommend success, because the write happened and
   the ids must not be lost.
3. If H5 shows AnkiWeb rate-limits repeated logins, may I cache the host key in memory
   per process (not on disk)? Recommended, decided only if measurement demands it.
4. Should a sync get a timeout? A hung AnkiWeb call would block a tool call (and the
   collection lock). I would measure first and add one only if Anki's own is too long.
