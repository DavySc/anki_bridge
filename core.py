"""Anki operations for the bridge. No MCP code here, so it can be tested alone.

Every public method opens the collection, does its work, and closes it again,
under one lock. Writes sync with AnkiWeb before and after. A sync that would
need a full upload/download is never resolved automatically: it raises
SyncRequired and the write is not attempted. A call that cannot get the lock
within LOCK_WAIT seconds (another call is stuck on AnkiWeb) answers "busy".
"""

from __future__ import annotations

import html
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from anki.collection import AddNoteRequest, Collection
from anki.errors import NetworkError, SearchError, SyncError, SyncErrorKind
from anki.notes import Note
from anki.sync_pb2 import SyncAuth, SyncCollectionResponse

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
LOCK_WAIT = 3  # seconds; a stalled AnkiWeb request takes ~31 s to fail
NOTE_KEYS = ("deck", "note_type", "fields", "tags")


class BridgeError(Exception):
    """A problem the caller should see, with a message meant for Claude."""


class SyncRequired(BridgeError):
    """AnkiWeb wants a full sync. A human has to choose upload or download."""


def _plain(text: str) -> str:
    """Anki wraps names in Unicode isolation marks (U+2068, U+2069); in a message for Claude they are noise."""
    return text.replace("\u2068", "").replace("\u2069", "")


# ---- card HTML ------------------------------------------------------------------
# Anki, AnkiDroid and AnkiMobile render fields as HTML with JavaScript on, so a field from a
# prompt-injected conversation could run script or load a tracking pixel on every device. Fields
# get an allowlist of formatting tags, checked by a strict scan: every "<" that a browser reads as
# markup must open a complete, allowed tag. (html.parser silently drops an unclosed tag at the
# end of the text, which the card template's next ">" would then complete.)

SAFE_TAGS = frozenset(
    "a abbr b big blockquote br center cite code dd del div dl dt em font h1 h2 h3 h4 h5 h6 hr i img"
    " ins kbd li mark ol p pre q rp rt ruby s samp small span strike strong sub sup table tbody td"
    " tfoot th thead tr tt u ul var wbr".split()
)
SAFE_ATTRS = frozenset(
    "align alt border cellpadding cellspacing class color colspan dir face height lang rowspan size"
    " start style title type valign value width".split()
)  # and href on <a>, src on <img>: see _unsafe_attr
_MARKUP = re.compile(r"<[A-Za-z/!?]")  # what a browser reads as the start of a tag or comment
_TAG = re.compile(
    r"""<(/?)([A-Za-z][A-Za-z0-9]*)((?:\s+[^\s"'<>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*/?>"""
)
_ATTR = re.compile(r"""\s+([^\s"'<>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")
_CSS_LOADS = re.compile(r"url\(|src\(|image\(|image-set\(|expression\(|@import|binding|behavior|\\|/\*")
_URL_NOISE = re.compile(r"[\x00-\x20]")  # browsers drop these from a URL before reading its scheme


def _unsafe_html(text: str) -> str | None:
    """Why `text` is not allowed as card HTML, or None."""
    pos = 0
    while (start := _MARKUP.search(text, pos)) is not None:
        tag = _TAG.match(text, start.start())
        if tag is None:
            return "a '<' that does not open a complete tag must be written as &lt;"
        closing, name, attrs = tag.group(1), tag.group(2).lower(), tag.group(3)
        if name not in SAFE_TAGS:
            return f"<{name}> is not allowed"
        if not closing:  # browsers ignore attributes on end tags
            for attr in _ATTR.finditer(attrs):
                problem = _unsafe_attr(name, attr.group(1).lower(), attr.group(2) or "")
                if problem:
                    return problem
        pos = tag.end()
    return None


def _unsafe_attr(tag: str, name: str, raw: str) -> str | None:
    value = html.unescape(raw[1:-1] if raw[:1] in ("'", '"') else raw)
    url = _URL_NOISE.sub("", value).lower()
    if tag == "a" and name == "href":
        return None if url.startswith(("http://", "https://", "mailto:")) else "links must be http, https or mailto"
    if tag == "img" and name == "src":
        local = url and ":" not in url and "\\" not in url and not url.startswith("/")
        return None if local else "<img src> must name a media file, not a URL"
    if name not in SAFE_ATTRS:
        return f"attribute {name} on <{tag}> is not allowed"
    if name == "style" and _CSS_LOADS.search(value.lower()):
        return "a style may not load anything (url(), @import, ...)"
    return None


@dataclass
class SyncConfig:
    username: str
    password: str
    endpoint: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "SyncConfig | None":
        user = env.get("ANKIWEB_USER")
        pw = env.get("ANKIWEB_PASS")
        if not user or not pw:
            return None
        return cls(user, pw, env.get("ANKIWEB_ENDPOINT") or None)


class Bridge:
    def __init__(self, collection_path: str, sync: SyncConfig | None = None):
        self.path = collection_path
        self.sync_cfg = sync
        self._lock = threading.Lock()
        self._auth: SyncAuth | None = None  # AnkiWeb login key, reused until AnkiWeb rejects it

    # ---- plumbing -------------------------------------------------------

    @contextmanager
    def _col(self) -> Iterator[Collection]:
        # Waiting without a bound would park every call (and a worker thread each) behind one
        # stalled sync. Anki opens a collection exclusively, so there is nothing to read meanwhile.
        if not self._lock.acquire(timeout=LOCK_WAIT):
            raise BridgeError(
                "The collection is busy with another call, usually an AnkiWeb sync. "
                "Nothing was done; try again in a minute."
            )
        try:
            # Collection() would create an empty collection, whose reads say "no duplicates".
            if not os.path.exists(self.path):
                raise BridgeError(
                    "The Anki collection has not been downloaded to the server yet. "
                    "Run `python bootstrap.py download` there once (see README)."
                )
            col = Collection(self.path)
            try:
                yield col
            finally:
                col.close()
        finally:
            self._lock.release()

    def _sync(self, col: Collection) -> str:
        """Normal sync only. Returns a short status string."""
        if self.sync_cfg is None:
            return "sync not configured"
        self._mark_unsent_changes(col)
        try:
            out = self._sync_collection(col)
        except (SyncError, NetworkError) as e:
            raise self._explain(e) from e
        req = out.required
        R = SyncCollectionResponse.ChangesRequired
        # `required` says whether a full sync is needed, not whether anything moved: a completed
        # normal sync reports NO_CHANGES even after pushing notes (see test_sync.py).
        if req in (R.NO_CHANGES, R.NORMAL_SYNC):
            return "synced"
        # FULL_UPLOAD / FULL_DOWNLOAD say which side Anki would keep; FULL_SYNC leaves the choice to us.
        # Only FULL_SYNC occurs against the bundled server (test_sync.py); the others are AnkiWeb's.
        direction = {
            R.FULL_UPLOAD: "AnkiWeb wants this collection uploaded, replacing what is on AnkiWeb.",
            R.FULL_DOWNLOAD: "AnkiWeb wants its copy downloaded, replacing this collection.",
        }.get(req, "The direction (upload or download) is yours to choose.")
        raise SyncRequired(
            f"AnkiWeb requires a full sync. {direction} It was not done. "
            "Run bootstrap.py by hand and choose the direction."
        )

    @staticmethod
    def _mark_unsent_changes(col: Collection) -> None:
        """Anki skips a sync when the collection's mod time equals the last sync's. A write in the
        same millisecond as the sync before it leaves them equal, and the sync after it would then
        report success without sending the write. Anki has no API for this; col.mod is this column."""
        mod, last_sync = col.db.first("SELECT mod, ls FROM col")
        if mod == last_sync and col.db.scalar(
            "SELECT EXISTS(SELECT 1 FROM notes WHERE usn = -1) OR EXISTS(SELECT 1 FROM cards WHERE usn = -1)"
        ):
            col.db.execute("UPDATE col SET mod = ?", last_sync + 1)

    def _sync_collection(self, col: Collection) -> SyncCollectionResponse:
        """Logs in only without a key or after AnkiWeb rejected the cached one, not on every
        sync: AnkiWeb may rate-limit logins."""
        if self._auth is not None:
            try:
                return col.sync_collection(self._auth, sync_media=False)
            except SyncError as e:
                if e.kind != SyncErrorKind.AUTH:
                    raise
                self._auth = None  # expired or revoked: log in again, once
        cfg = self.sync_cfg
        assert cfg is not None
        self._auth = col.sync_login(cfg.username, cfg.password, cfg.endpoint)
        return col.sync_collection(self._auth, sync_media=False)

    def _explain(self, exc: Exception) -> BridgeError:
        """A sync failure as a message for Claude. mcp 2.x shows the model only BridgeError text,
        and the password must never be in it."""
        if isinstance(exc, SyncError) and exc.kind == SyncErrorKind.AUTH:
            return BridgeError(
                "AnkiWeb login failed. Check ANKIWEB_USER and ANKIWEB_PASS on the server."
            )
        if isinstance(exc, NetworkError):
            return BridgeError("Could not reach AnkiWeb (network error).")
        text = str(exc)
        if self.sync_cfg and self.sync_cfg.password:
            text = text.replace(self.sync_cfg.password, "***")
        return BridgeError(f"AnkiWeb sync failed: {text}")

    def _sync_before_write(self, col: Collection) -> str:
        """The sync that must succeed before a write. If it fails, nothing has been written."""
        try:
            return self._sync(col)
        except SyncRequired as e:
            raise SyncRequired(f"{e} Nothing was written.") from e
        except BridgeError as e:
            raise BridgeError(f"{e} Nothing was written.") from e

    def _sync_after_write(self, col: Collection) -> str:
        """The sync after a write. The write is already saved locally, so a failure here must not
        hide its result: report it in the status instead of raising."""
        try:
            return self._sync(col)
        except SyncRequired as e:
            return f"failed: {e} Until then this write is only on this server: a download would discard it."
        except BridgeError as e:
            return f"failed: {e} Run sync to retry."

    # ---- input shape ----------------------------------------------------
    # Checked before syncing, so a malformed request costs no AnkiWeb round trip. Bad input is
    # rejected, never coerced: str(None) would become the text "None", list("a b") single letters.

    @staticmethod
    def _tags(tags: Any) -> list[str]:
        if tags is None:
            return []
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.split() == [t] for t in tags):
            raise BridgeError('tags must be a list of words without spaces, like ["physics", "optics"].')
        return tags

    @staticmethod
    def _fields(fields: Any) -> dict[str, str]:
        if fields is None:
            return {}
        if not isinstance(fields, dict):
            raise BridgeError('fields must map field names to text, like {"Front": "...", "Back": "..."}.')
        not_text = [k for k, v in fields.items() if not isinstance(v, str)]
        if not_text:
            raise BridgeError(f"The value of field(s) {not_text} must be text.")
        for name, text in fields.items():
            problem = _unsafe_html(text)
            if problem:
                raise BridgeError(
                    f"Field {name!r}: {problem}. Card HTML runs on every device that shows the card, "
                    "so only text formatting, links and local images are allowed."
                )
        return fields

    @classmethod
    def _parse_note(cls, item: Any) -> tuple[str, str, dict[str, str], list[str]]:
        """One add_notes item as (deck, note type, fields, tags)."""
        if not isinstance(item, dict):
            raise BridgeError(f"Each note must be an object with {', '.join(NOTE_KEYS)}.")
        unknown = sorted(str(k) for k in item if k not in NOTE_KEYS)
        if unknown:
            raise BridgeError(f"Unknown key(s) {unknown}. A note has only {', '.join(NOTE_KEYS)}.")
        for key in ("deck", "note_type"):
            if not isinstance(item.get(key), str):
                raise BridgeError(f"{key} must be the name of an existing {key.replace('_', ' ')}.")
        return item["deck"], item["note_type"], cls._fields(item.get("fields")), cls._tags(item.get("tags", []))

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
        if col.decks.is_filtered(did):  # Anki would silently put the cards in Default
            raise BridgeError(f"{name!r} is a filtered deck. Add to a normal deck instead.")
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
            # One query instead of a search per deck: deck names can hold search syntax (_ * " \),
            # and per-deck searches took ~1 s at 600 decks. Like `deck:`, a card in a filtered deck
            # also counts in its home deck.
            counts = dict(
                col.db.all(
                    "SELECT did, COUNT(DISTINCT nid) FROM"
                    " (SELECT did, nid FROM cards UNION SELECT odid, nid FROM cards WHERE odid != 0)"
                    " GROUP BY did"
                )
            )
            out = [{"name": d.name, "notes": counts.get(d.id, 0)} for d in col.decks.all_names_and_ids()]
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
                    ids = col.find_notes(f"mid:{nt['id']}")[:1]  # by id: names hold search syntax
                    item["example"] = (
                        self._note_view(col, col.get_note(ids[0])) if ids else None
                    )
                out.append(item)
            return sorted(out, key=lambda x: x["name"])

    def find_notes(self, query: str, limit: int = 20) -> dict[str, Any]:
        """The first `limit` matching notes, and how many match in all (so a caller can tell a
        duplicate check saw only part)."""
        limit = max(1, min(limit, MAX_FIND))
        with self._col() as col:
            try:
                ids = col.find_notes(query)
            except SearchError as e:
                raise BridgeError(_plain(str(e))) from e
            return {"total": len(ids), "notes": [self._note_view(col, col.get_note(i)) for i in ids[:limit]]}

    # ---- write tools ----------------------------------------------------

    def add_notes(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """All-or-nothing: every item is validated before any is added."""
        if not items:
            raise BridgeError("No notes given.")
        if len(items) > MAX_BATCH:
            raise BridgeError(f"At most {MAX_BATCH} notes per call.")
        parsed: list[tuple[str, str, dict[str, str], list[str]]] = []
        problems: list[str] = []
        for n, item in enumerate(items, 1):
            try:
                parsed.append(self._parse_note(item))
            except BridgeError as e:
                problems.append(f"#{n}: {e}")
        if problems:
            raise BridgeError("Nothing was added. Fix and resend:\n" + "\n".join(problems))
        with self._col() as col:
            pre = self._sync_before_write(col)
            staged: list[tuple[Note, int]] = []
            seen: set[tuple[str, str]] = set()
            for n, (deck, note_type, fields, tags) in enumerate(parsed, 1):
                try:
                    nt = self._notetype(col, note_type)
                    did = self._deck_id(col, deck)
                    self._check_fields(nt, fields, partial=False)
                    note = col.new_note(nt)
                    for k, v in fields.items():
                        note[k] = v
                    note.tags = list(tags)
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
                except BridgeError as e:
                    problems.append(f"#{n}: {e}")
            if problems:
                raise BridgeError(
                    "Nothing was added. Fix and resend:\n" + "\n".join(problems)
                )
            requests = [AddNoteRequest(note=note, deck_id=did) for note, did in staged]
            col.add_notes(requests)  # one operation: every note is stored, or none
            ids = [int(r.note.id) for r in requests]
            post = self._sync_after_write(col)
            return {"added": ids, "sync_before": pre, "sync_after": post}

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        fields, add_tags, remove_tags = self._fields(fields), self._tags(add_tags), self._tags(remove_tags)
        with self._col() as col:
            pre = self._sync_before_write(col)
            try:
                note = col.get_note(note_id)  # type: ignore[arg-type]
            except Exception as e:
                raise BridgeError(f"No note with id {note_id}.") from e
            nt = note.note_type()
            if fields:
                self._check_fields(nt, fields, partial=True)
                before = note.fields_check()
                for k, v in fields.items():
                    note[k] = v
                # Only refuse what this edit breaks: a note that already was, say, a duplicate
                # must still take an edit (or a 'to-review' tag).
                after = note.fields_check()
                if after not in (0, before):
                    raise BridgeError(f"Nothing was changed: {FIELD_CHECK.get(after, f'field check code {after}')}.")
            for t in add_tags:
                note.add_tag(t)
            for t in remove_tags:
                note.remove_tag(t)
            col.update_note(note)
            post = self._sync_after_write(col)
            return {
                "note": self._note_view(col, col.get_note(note.id)),
                "sync_before": pre,
                "sync_after": post,
            }

    def sync(self) -> dict[str, str]:
        with self._col() as col:
            return {"result": self._sync(col)}
