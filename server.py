"""MCP server exposing six Anki tools. Run: python server.py

Env:
  ANKI_COLLECTION   path to collection.anki2 (default ./data/collection.anki2)
  ANKIWEB_USER / ANKIWEB_PASS   AnkiWeb login (omit to run without sync)
  HOST / PORT       bind address (default 127.0.0.1:8000, put Caddy in front)

Auth (OAuth) is not wired up yet. Do not expose this port publicly until it is.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from core import Bridge, BridgeError, SyncConfig

bridge = Bridge(
    os.environ.get("ANKI_COLLECTION", "./data/collection.anki2"),
    SyncConfig.from_env(),
)

mcp = MCPServer(
    "anki",
    instructions=(
        "Tools for the user's Anki collection. Propose cards in chat first and "
        "call add_notes or update_note only after the user explicitly approves "
        "them in this conversation. Check list_note_types for field names and "
        "run find_notes for near-duplicates before proposing."
    ),
)


def _guard(approved: bool) -> None:
    if not approved:
        raise BridgeError(
            "Not approved. Show the proposed cards to the user and call again "
            "with approved_by_user=true only after they say yes."
        )


@mcp.tool()
def list_decks() -> list[dict[str, Any]]:
    """List decks with the number of notes directly in each."""
    return bridge.list_decks()


@mcp.tool()
def list_note_types(with_examples: bool = False) -> list[dict[str, Any]]:
    """List note types with field names and templates. with_examples adds one existing note per type."""
    return bridge.list_note_types(with_examples)


@mcp.tool()
def find_notes(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search with Anki search syntax (e.g. 'deck:Physics tag:to-review', 'prop:lapses>3', or plain words). Use before proposing cards to catch near-duplicates."""
    return bridge.find_notes(query, limit)


@mcp.tool()
def add_notes(notes: list[dict[str, Any]], approved_by_user: bool = False) -> dict[str, Any]:
    """Add notes in one all-or-nothing batch (max 50). Each item: {deck, note_type, fields: {FieldName: text}, tags: [..]}. Existing decks and note types only. Syncs with AnkiWeb before and after. Requires approved_by_user=true, set only after the user approved these exact cards."""
    _guard(approved_by_user)
    return bridge.add_notes(notes)


@mcp.tool()
def update_note(
    note_id: int,
    fields: dict[str, str] | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    approved_by_user: bool = False,
) -> dict[str, Any]:
    """Change fields (partial) and tags of one note. Cannot change deck or note type, and cannot delete: tag a note 'to-review' instead. Requires approved_by_user=true."""
    _guard(approved_by_user)
    return bridge.update_note(note_id, fields, add_tags, remove_tags)


@mcp.tool()
def sync() -> dict[str, str]:
    """Normal sync with AnkiWeb. Fails if a full sync is required."""
    return bridge.sync()


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
