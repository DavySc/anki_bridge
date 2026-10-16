"""Anki operations for the bridge. No MCP code here, so it can be tested alone.

Every public method opens the collection, does its work, and closes it again,
under one lock. Writes sync with AnkiWeb before and after. A sync that would
need a full upload/download is never resolved automatically: it raises
SyncRequired and the write is not attempted.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from anki.collection import Collection
from anki.notes import Note
from anki.sync_pb2 import SyncCollectionResponse

# Note.fields_check() returns a plain int.
FIELD_CHECK = {
    1: "empty first field",
    2: "duplicate of an existing note",
    3: "cloze note without a cloze deletion",
    4: "cloze deletion in a non-cloze note type",
    5: "cloze deletion in a field that cannot hold one",
}

MAX_BATCH = 50
MAX_FIND = 100


class BridgeError(Exception):
    """A problem the caller should see, with a message meant for Claude."""


class SyncRequired(BridgeError):
    """AnkiWeb wants a full sync. A human has to choose upload or download."""


@dataclass
class SyncConfig:
    username: str
    password: str
    endpoint: str | None = None

    @classmethod
    def from_env(cls) -> "SyncConfig | None":
        user = os.environ.get("ANKIWEB_USER")
        pw = os.environ.get("ANKIWEB_PASS")
        if not user or not pw:
            return None
        return cls(user, pw, os.environ.get("ANKIWEB_ENDPOINT") or None)


class Bridge:
    def __init__(self, collection_path: str, sync: SyncConfig | None = None):
        self.path = collection_path
        self.sync_cfg = sync
        self._lock = threading.Lock()

    # ---- plumbing -------------------------------------------------------

    @contextmanager
    def _col(self) -> Iterator[Collection]:
        with self._lock:
            col = Collection(self.path)
            try:
                yield col
            finally:
                col.close()

    def _sync(self, col: Collection) -> str:
        """Normal sync only. Returns a short status string."""
        if self.sync_cfg is None:
            return "sync not configured"
        cfg = self.sync_cfg
        auth = col.sync_login(cfg.username, cfg.password, cfg.endpoint)
        out = col.sync_collection(auth, sync_media=False)
        req = out.required
        R = SyncCollectionResponse.ChangesRequired
        if req == R.NO_CHANGES:
            return "no changes"
        if req == R.NORMAL_SYNC:
            return "synced"
        raise SyncRequired(
            "AnkiWeb requires a full sync (upload or download). Nothing was "
            "written. Run bootstrap.py by hand and choose the direction."
        )

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _notetype(col: Collection, name: str) -> dict[str, Any]:
        nt = col.models.by_name(name)
        if nt is None:
            names = ", ".join(sorted(m.name for m in col.models.all_names_and_ids()))
            raise BridgeError(f"Unknown note type {name!r}. Available: {names}")
        return nt

    @staticmethod
    def _deck_id(col: Collection, name: str) -> int:
        did = col.decks.id_for_name(name)
        if not did:
            names = ", ".join(sorted(d.name for d in col.decks.all_names_and_ids()))
            raise BridgeError(f"Unknown deck {name!r}. Available: {names}")
        return int(did)

    @staticmethod
    def _check_fields(nt: dict[str, Any], fields: dict[str, str], *, partial: bool) -> None:
        known = [f["name"] for f in nt["flds"]]
        unknown = [k for k in fields if k not in known]
        if unknown:
            raise BridgeError(
                f"Note type {nt['name']!r} has no field(s) {unknown}. Fields: {known}"
            )
        if not partial:
            first = known[0]
            if not fields.get(first, "").strip():
                raise BridgeError(
                    f"Field {first!r} (the sort field of {nt['name']!r}) is empty."
                )

    @staticmethod
    def _note_view(col: Collection, note: Note) -> dict[str, Any]:
        cards = note.cards()
        deck = col.decks.name(cards[0].did) if cards else None
        return {
            "id": int(note.id),
            "note_type": note.note_type()["name"],
            "deck": deck,
            "fields": dict(note.items()),
            "tags": list(note.tags),
        }

    # ---- read tools -----------------------------------------------------

    def list_decks(self) -> list[dict[str, Any]]:
        with self._col() as col:
            out = []
            for d in col.decks.all_names_and_ids():
                out.append(
                    {
                        "name": d.name,
                        "notes": len(col.find_notes(f'deck:"{d.name}" -deck:"{d.name}::*"')),
                    }
                )
            return sorted(out, key=lambda x: x["name"])

    def list_note_types(self, with_examples: bool = False) -> list[dict[str, Any]]:
        with self._col() as col:
            out = []
            for nt in col.models.all():
                item: dict[str, Any] = {
                    "name": nt["name"],
                    "kind": "cloze" if nt["type"] == 1 else "standard",
                    "fields": [f["name"] for f in nt["flds"]],
                    "templates": [t["name"] for t in nt["tmpls"]],
                }
                if with_examples:
                    ids = col.find_notes(f'"note:{nt["name"]}"')[:1]
                    item["example"] = (
                        self._note_view(col, col.get_note(ids[0])) if ids else None
                    )
                out.append(item)
            return sorted(out, key=lambda x: x["name"])

    def find_notes(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, MAX_FIND))
        with self._col() as col:
            ids = col.find_notes(query)
            return [self._note_view(col, col.get_note(i)) for i in ids[:limit]]

    # ---- write tools ----------------------------------------------------

    def add_notes(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """All-or-nothing: every item is validated before any is added."""
        if not items:
            raise BridgeError("No notes given.")
        if len(items) > MAX_BATCH:
            raise BridgeError(f"At most {MAX_BATCH} notes per call.")
        with self._col() as col:
            pre = self._sync(col)
            staged: list[tuple[Note, int]] = []
            problems: list[str] = []
            seen: set[tuple[str, str]] = set()
            for n, item in enumerate(items, 1):
                try:
                    nt = self._notetype(col, item["note_type"])
                    did = self._deck_id(col, item["deck"])
                    fields = {k: str(v) for k, v in item["fields"].items()}
                    self._check_fields(nt, fields, partial=False)
                    note = col.new_note(nt)
                    for k, v in fields.items():
                        note[k] = v
                    note.tags = list(item.get("tags", []))
                    chk = note.fields_check()
                    if chk != 0:
                        problems.append(
                            f"#{n}: {FIELD_CHECK.get(chk, f'field check code {chk}')}"
                        )
                        continue
                    key = (nt["name"], note.fields[0].strip())
                    if key in seen:
                        problems.append(f"#{n}: duplicate of another note in this batch")
                        continue
                    seen.add(key)
                    staged.append((note, did))
                except (BridgeError, KeyError) as e:
                    problems.append(f"#{n}: {e}")
            if problems:
                raise BridgeError(
                    "Nothing was added. Fix and resend:\n" + "\n".join(problems)
                )
            ids = []
            for note, did in staged:
                col.add_note(note, did)
                ids.append(int(note.id))
            post = self._sync(col)
            return {"added": ids, "sync_before": pre, "sync_after": post}

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._col() as col:
            pre = self._sync(col)
            try:
                note = col.get_note(note_id)  # type: ignore[arg-type]
            except Exception as e:
                raise BridgeError(f"No note with id {note_id}.") from e
            nt = note.note_type()
            if fields:
                self._check_fields(nt, fields, partial=True)
                for k, v in fields.items():
                    note[k] = str(v)
            for t in add_tags or []:
                note.add_tag(t)
            for t in remove_tags or []:
                note.remove_tag(t)
            col.update_note(note)
            post = self._sync(col)
            return {
                "note": self._note_view(col, col.get_note(note.id)),
                "sync_before": pre,
                "sync_after": post,
            }

    def sync(self) -> dict[str, str]:
        with self._col() as col:
            return {"result": self._sync(col)}
