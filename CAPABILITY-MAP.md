# Capability Map: anki-claude-bridge, remaining work

Status: APPROVED. Specs: SPEC-auth.md (built), SPEC-sync-verification.md (built).

The six tools, validation and core tests are done (see README "Status"). Three
capabilities are left. Each can ship and be verified on its own.

| Module id | Responsibility | Depends on |
|---|---|---|
| `sync-verification` | Prove `Bridge._sync` and `bootstrap.py` work against a real AnkiWeb account, including the "full sync required" refusal path. | — |
| `auth` | Single-user OAuth in the server itself: only the owner can authorize Claude to call tools. | — |
| `deploy` | Run on the Hetzner VPS: systemd unit, nginx HTTPS, secrets, one-time bootstrap download, backups. | `auth`, `sync-verification` |

Build order: `sync-verification` and `auth` (independent, either first) → `deploy`

## Why these boundaries

- `sync-verification` touches only `core.py`, `bootstrap.py` and tests. It needs a
  real (ideally throwaway) AnkiWeb account, not the server.
- `auth` touches only `server.py` and a new auth module. It is testable locally
  with no VPS and no Anki sync.
- `deploy` is configuration, not application logic. It must not start until the
  port can be exposed safely (`auth`) and the collection can be trusted to sync
  (`sync-verification`).

## Interfaces at the boundaries

- `auth` -> `deploy`: `auth` defines the env vars it needs (e.g. the owner secret,
  public base URL for OAuth metadata). `deploy` only supplies them.
- `sync-verification` -> `deploy`: `sync-verification` defines the exact
  bootstrap procedure and the failure behaviour. `deploy` documents running it.

## Assumptions I'm making

1. `auth` is a built-in single-user OAuth authorization server (your choice), not
   an external IdP. Exact flow (dynamic client registration, PKCE, token
   lifetime) is decided in `SPEC-auth.md`, driven by what the `mcp` 2.x SDK and
   Claude's remote-MCP connector require.
2. Claude connects as a remote MCP (custom connector) over streamable HTTP,
   including from the phone app.
3. `sync-verification` runs almost entirely offline against Anki's bundled sync
   server (`python -m anki.syncserver`). A throwaway AnkiWeb account is only needed
   for one final manual smoke test; never use your real account.
4. The VPS runs NixOS or a generic Linux with nix available. This affects
   `deploy` only. Tell me which.
5. Self-hosted sync server stays out of scope (GOALS.md: "maybe later").

## Open questions for review

- Are the three module boundaries and the build order right?
- Should `deploy` include automated backups of `collection.anki2`, or is
  AnkiWeb itself the backup?
- Order preference: `sync-verification` first (lower risk, validates the core) or
  `auth` first (unblocks the most)?
