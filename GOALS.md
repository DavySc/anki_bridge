# anki-claude-bridge: goals

Personal tool for me, and a few people I add by hand (see "Multi-user readiness",
[SPEC-multi-user.md](SPEC-multi-user.md)). Turn insights from Claude conversations (mainly on
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
  One exception that overwrites nothing: when a person saves their AnkiWeb login on the
  login page and has no collection on the server yet, theirs is downloaded into the empty place.
- Guess field names or silently drop fields. Reject and report instead.

## Proposal flow

The chat is the staging area. Claude proposes numbered cards (deck, note type,
fields, tags), pre-checked with `find_notes` for duplicates. I approve or edit.
Only then is `add_notes` called. There is no server-side pending queue.

## Tools (six)

`list_decks`, `list_note_types`, `find_notes`, `add_notes`, `update_note`, `sync`

## Multi-user readiness

Built: other people listed in a users file (`USERS_FILE`), each with their own
login, collection and AnkiWeb account. Without the file, nothing differs from the
single-user server. The rules that made this a contained change still hold:

- Every authorization code and token carries a `subject` (the user id): `owner`
  for me, the username for anyone else.
- Login goes through one function, `authenticate(credentials) -> subject | None`.
  Owner only: the owner secret. With users: username (empty = owner) plus secret.
- Tools never use a global `Bridge`. They call `bridge_for(subject)`, where the
  subject comes from the access token.
- `core.py` stays free of auth and MCP code and holds no per-process user state;
  a `Bridge` is one user's collection, lock and sync credentials.
- Per-user data lives under one root: `data/<subject>/collection.anki2`
  (`data/owner/` for me, with `ANKI_COLLECTION` still overriding my path).
- Login rate limits key on client IP plus a global bucket, never on the claimed
  username alone (the attacker chooses it); a per-user key may be added later.
- No AnkiWeb password is stored. Each person enters their AnkiWeb login on the
  login page; only AnkiWeb's sync key is kept, encrypted so that it opens only with
  that person's secret or their live tokens (vault.py). The admin cannot read it.
  Mine may stay in the env file until I move it.

Not built, on purpose: signup or invite flow, roles, admin UI, quotas, encrypted
credential storage.

## Out of scope

Reviewing in chat, media, scheduling changes, deck or note-type restructuring,
self-service accounts (people are added by hand in the users file).

## Decisions

- Sync: AnkiWeb (self-hosted sync server maybe later).
- Host: Hetzner VPS, Python under systemd, nginx for HTTPS.
- Library: `anki` (headless), `mcp` 2.x (`MCPServer`).
- Auth: built-in single-user OAuth, only I can authorize. See SPEC-auth.md.

## Open

- Note-type-to-deck map goes in the Claude project instructions.
- Card-writing spec (one fact per card, standalone, cloze vs basic) goes there too.
