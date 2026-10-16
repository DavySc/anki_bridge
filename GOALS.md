# anki-claude-bridge: goals

Personal tool, one user today. Built so that adding users later is a contained
change (see "Multi-user readiness"). Turn insights from Claude conversations (mainly on
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

## Multi-user readiness

Only one user is supported and built. These rules keep a later per-user
expansion cheap, and cost almost nothing now:

- Every authorization code and token carries a `subject` (the user id). Today it
  is always `owner`.
- Login goes through one function, `authenticate(credentials) -> subject | None`.
  Today it checks the single owner secret.
- Tools never use a global `Bridge`. They call `bridge_for(subject)`, where the
  subject comes from the access token. Today it returns the one Bridge.
- `core.py` stays free of auth and MCP code and holds no per-process user state;
  a `Bridge` is one user's collection, lock and sync credentials.
- Per-user data lives under one root: `data/<subject>/collection.anki2`
  (today `data/owner/`, with `ANKI_COLLECTION` still overriding the path).
- Login rate limits key on client IP plus a global bucket, never on the claimed
  username alone (the attacker chooses it); a per-user key may be added later.
- Per-user AnkiWeb credentials are not stored yet (env vars). Storing other
  people's AnkiWeb passwords needs encryption at rest and is a decision for
  that later project.

Not built, on purpose: user table, signup or invite flow, per-user credential
storage, per-user bootstrap tooling, quotas.

## Out of scope

Reviewing in chat, media, scheduling changes, deck or note-type restructuring,
multi-user accounts (only the seams above).

## Decisions

- Sync: AnkiWeb (self-hosted sync server maybe later).
- Host: Hetzner VPS, Python under systemd, Caddy for HTTPS.
- Library: `anki` (headless), `mcp` 2.x (`MCPServer`).
- Auth: built-in single-user OAuth, only I can authorize. See SPEC-auth.md.

## Open

- Note-type-to-deck map goes in the Claude project instructions.
- Card-writing spec (one fact per card, standalone, cloze vs basic) goes there too.
