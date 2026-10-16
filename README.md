# anki-claude-bridge

Headless MCP server that lets Claude add and edit notes in my Anki collection,
syncing through AnkiWeb. Spec and boundaries: [GOALS.md](GOALS.md).

## Run locally

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    python -m pytest -q            # uses a throwaway collection

    export ANKI_COLLECTION=./data/collection.anki2
    export ANKIWEB_USER=... ANKIWEB_PASS=...
    python bootstrap.py download   # one time, by hand
    python server.py               # 127.0.0.1:8000, streamable HTTP

## Status

- Done: six tools, validation, tests (core logic against a temp collection).
- Untested: real AnkiWeb sync.
- Not built: OAuth (do not expose the port publicly), systemd unit, Caddy config.

Note: this uses mcp 2.x (`MCPServer`), not the 1.x `FastMCP`.
