"""Checks core.py against GOALS.md using a throwaway collection. Run: python -m pytest -q"""

import os
import tempfile

import pytest
from anki.collection import Collection

from core import Bridge, BridgeError


@pytest.fixture()
def bridge():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "c.anki2")
    col = Collection(path)
    col.decks.add_normal_deck_with_name("Physics")
    col.close()
    return Bridge(path, sync=None)


def basic(deck="Physics", front="Why is the sky blue?", back="Rayleigh scattering", tags=None):
    return {
        "deck": deck,
        "note_type": "Basic",
        "fields": {"Front": front, "Back": back},
        "tags": tags or ["claude"],
    }


def test_lists(bridge):
    assert "Physics" in [d["name"] for d in bridge.list_decks()]
    nts = {n["name"]: n for n in bridge.list_note_types()}
    assert nts["Basic"]["fields"] == ["Front", "Back"]
    assert nts["Cloze"]["kind"] == "cloze"


def test_add_and_find(bridge):
    r = bridge.add_notes([basic()])
    assert len(r["added"]) == 1
    hit = bridge.find_notes("sky")
    assert hit[0]["deck"] == "Physics" and hit[0]["tags"] == ["claude"]


def test_unknown_deck_note_type_and_field_rejected(bridge):
    for bad in (
        basic(deck="Nope"),
        {**basic(), "note_type": "Nope"},
        {**basic(), "fields": {"Front": "x", "Bak": "y"}},
    ):
        with pytest.raises(BridgeError):
            bridge.add_notes([bad])
    assert bridge.find_notes("deck:Physics") == []


def test_batch_is_all_or_nothing(bridge):
    with pytest.raises(BridgeError):
        bridge.add_notes([basic(), basic(deck="Nope", front="other")])
    assert bridge.find_notes("deck:Physics") == []


def test_duplicates_rejected_existing_and_in_batch(bridge):
    bridge.add_notes([basic()])
    with pytest.raises(BridgeError, match="duplicate"):
        bridge.add_notes([basic(back="different back")])
    with pytest.raises(BridgeError, match="duplicate"):
        bridge.add_notes([basic(front="q"), basic(front="q", back="z")])


def test_empty_first_field_and_bad_cloze_rejected(bridge):
    with pytest.raises(BridgeError):
        bridge.add_notes([basic(front="  ")])
    with pytest.raises(BridgeError, match="cloze"):
        bridge.add_notes(
            [{"deck": "Physics", "note_type": "Cloze", "fields": {"Text": "no deletion"}}]
        )
    bridge.add_notes(
        [{"deck": "Physics", "note_type": "Cloze", "fields": {"Text": "{{c1::Rayleigh}} scattering"}}]
    )


def test_update_partial_and_tags(bridge):
    nid = bridge.add_notes([basic()])["added"][0]
    r = bridge.update_note(nid, fields={"Back": "Rayleigh scattering of sunlight"}, add_tags=["to-review"])
    assert r["note"]["fields"]["Front"] == "Why is the sky blue?"
    assert r["note"]["fields"]["Back"].endswith("sunlight")
    assert set(r["note"]["tags"]) == {"claude", "to-review"}
    with pytest.raises(BridgeError):
        bridge.update_note(nid, fields={"Nope": "x"})
    with pytest.raises(BridgeError):
        bridge.update_note(999, fields={"Back": "x"})


def test_no_delete_or_restructure_surface():
    names = {m for m in dir(Bridge) if not m.startswith("_")}
    assert not {m for m in names if "delete" in m or "remove" in m or "create" in m}


def test_sync_not_configured_is_harmless(bridge):
    assert bridge.sync()["result"] == "sync not configured"
