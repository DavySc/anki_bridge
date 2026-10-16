# anki-claude-bridge: goals

Personal tool, single user. Turn insights from Claude conversations (mainly on
the phone) into well-formed notes in my existing Anki decks.

## Goal

From a Claude conversation, one request ("make cards from this") leads to a
proposal in chat. After I approve, the notes are added to my existing decks
and reach AnkiDroid / AnkiMobile via AnkiWeb.

## Must

- List my decks and note types (with field names), and show existing tags.
- Search for near-duplicates before adding.
- Add notes in bulk to existing decks using existing note types.
- Edit a note afterwards.
- Sync with AnkiWeb before and after every write.
- Run headless on an always-on Hetzner VPS (my PC stays off).

## Never

- Create or modify note types or decks.
- Delete notes. Tag them `to-review` instead.
- Write anything without my explicit approval in that conversation.
- Run a full sync automatically. A full sync overwrites one side, so it needs me.
- Guess field names or silently drop fields. Reject and report instead.

## Proposal flow

The chat is the staging area. Claude proposes numbered cards (deck, note type,
fields, tags), pre-checked with `find_notes` for duplicates. I approve or edit.
Only then is `add_notes` called. There is no server-side pending queue.

## Tools (six)

`list_decks`, `list_note_types`, `find_notes`, `add_notes`, `update_note`, `sync`

## Out of scope

Reviewing in chat, media, scheduling changes, deck or note-type restructuring,
multi-user.

## Decisions

- Sync: AnkiWeb (self-hosted sync server maybe later).
- Host: Hetzner VPS, Python under systemd, Caddy for HTTPS.
- Library: `anki` (headless), `mcp` 2.x (`MCPServer`).
- Auth: OAuth, only I can authorize. Still to be built.

## Open

- Note-type-to-deck map goes in the Claude project instructions.
- Card-writing spec (one fact per card, standalone, cloze vs basic) goes there too.
