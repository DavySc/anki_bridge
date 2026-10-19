# Spec: multi-user and encrypted AnkiWeb logins

Status: BUILT (offline, against Anki's local sync server). Not yet deployed.
Builds on the seams from [SPEC-auth.md](SPEC-auth.md) ("Multi-user seams").

## Objective

1. A second person adds the same connector (`https://anki.panzeilkamp.com/mcp`) in their own
   Claude, logs in with their own username and secret, and the tools work on *their* collection,
   synced with *their* AnkiWeb account. Neither person can reach the other's collection.
2. No AnkiWeb password is stored anywhere, and the AnkiWeb login that is stored opens only with
   its person's own secret or one of their live tokens. You as admin cannot read it, and neither
   can anyone holding the server's files or backups.

### What changes for the owner

- Nothing until you choose. The env vars (`OWNER_SECRET_HASH`, `ANKIWEB_*`, `ANKI_COLLECTION`,
  `DATA_DIR`) mean what they meant, your connector's tokens stay valid, no re-login.
- The login page gains a collapsed "AnkiWeb login" section, and a username field once
  `USERS_FILE` is set (you leave it empty).
- To encrypt your own AnkiWeb login: log in once more and enter it on the page, then remove
  `ANKIWEB_USER`/`ANKIWEB_PASS` from the env file and restart. While they are set, they win.
- `auth.sqlite3` gains a column and a table, added in place. The previous code still runs on the
  upgraded file (checked), so rolling back needs nothing.

Non-goals: self-service signup, roles, admin UI, quotas, protection against someone who
controls the *running* server (it necessarily sees a login while using it).

## Design

### Users file

`USERS_FILE=/srv/anki-bridge/users.toml` (unset = owner only), mode 600. Usually just names:

    [users.luca]                               # chooses his own secret with an invite
    [users.alice]
    secret_hash = 'scrypt$32768$8$...'         # optional: a secret the admin chose (python auth.py hash alice)

The server refuses to start when the file is readable by others, a name is not
`[a-z0-9][a-z0-9_-]{0,31}` or is `owner`, a key is unknown, or it holds AnkiWeb keys.

### Invites

Nobody hashes anything by hand, and the admin never learns a person's secret (which would let
them open that person's saved AnkiWeb login).

- `python auth.py invite luca` (with `USERS_FILE`, `DATA_DIR`) stores the SHA-256 of a fresh
  one-time code (120 random bits, `XXXXXX-XXXXXX-XXXXXX-XXXXXX`) in `auth.sqlite3` (`invites`),
  valid 7 days, replacing an earlier one, and prints the code. Only for entries without
  `secret_hash` (a file `secret_hash` wins). No restart needed.
- At the login page, username `luca` and the code as secret show a second page: choose a secret
  (12+ characters, twice) and the AnkiWeb login. On submit the code is checked again and used up,
  the scrypt hash of the new secret is stored (`chosen_secrets`), and the login continues.
- A wrong, used or expired code is a failed login (same rate limits). A bad choice re-asks and
  leaves the code valid. If the AnkiWeb step fails after that, the secret is kept and the page says
  to log in with it. A new invite resets a forgotten secret; the old AnkiWeb vault no longer opens,
  so the person enters their AnkiWeb login once more.

### Login

- Owner only: the secret, as before. With users: username (empty = owner) plus secret; an
  unknown name costs the same scrypt as a known one. Rate limits per IP and global, as before.
- Then the AnkiWeb step (`ankiweb_step` in `auth.py`, Anki side `AnkiWebSetup` in `server.py`):
  - AnkiWeb fields filled: the server logs in to AnkiWeb once, keeps only the sync key (hkey,
    as Anki desktop does), encrypts it, and downloads the person's collection if the server has
    none yet. A wrong AnkiWeb password is shown on the page; it is not a failed login.
  - Fields empty and a login saved: it is unlocked with the secret.
  - Nothing saved: other people must enter one; the owner may skip it (env login, or sync off,
    as before).
- Two people cannot save the same AnkiWeb account (keyed hash of the email; the owner's env
  account counts too).

### Encryption (`vault.py`)

    AnkiWeb login {username, hkey, endpoint} --AES-GCM--> under the person's data key (random)
    data key --wrapped--> under scrypt(secret, vault salt)        stored in `vaults`
    data key --wrapped--> under HMAC(code or token)               stored on each code/token row

- The data key moves login -> code -> access and refresh tokens -> every refreshed pair. A write
  tool unlocks it with the access token it arrives with.
- AES-GCM with the person (and purpose) as associated data: a blob moved to another row does not
  open. The vault salt differs from the login hash's salt, so the stored hash is not the key.
  Token keys are HMAC(token), not the SHA-256 the database stores to look tokens up.
- Saving again with the same secret keeps the data key, so the person's other connections stay
  unlocked. A changed secret cannot open the old vault: they enter their AnkiWeb login again.
- Library: `cryptography` (AESGCM), already installed as a dependency of `mcp`, now listed.

### Writes

`add_notes`, `update_note` and `sync` need an AnkiWeb login: the owner's env vars, or the one the
caller's token unlocks. A token that carries none (a connection made before the login was saved,
or by a person who never saved one) gets "log in again" and nothing is written; reads still work.
Exception, as before: the owner with neither env login nor saved login runs with sync off.
A saved sync key AnkiWeb rejects (password changed on AnkiWeb) says to log in again and enter it.

### Full syncs

`bootstrap.py` still holds the only full-sync calls. The server reaches exactly one of them,
`download_new`: a download into a path that does not exist. It never replaces a collection and
never uploads. A "full sync required" later is still refused; for another person the way out is
to move their collection away and log in again (fresh download), or Anki's own client.

### Tokens of a removed user

Codes and tokens whose subject is not the owner or in the users file load as None (401), and
carry no key. Re-adding the name revives unexpired grants; to cut someone off for good also
delete their rows (`DELETE FROM access_tokens/refresh_tokens/codes/vaults WHERE subject = 'alice'`).

## Testing Strategy

pytest, offline. `test_multi_user.py` (people, login, isolation), `test_vault.py` (encryption,
key flow, schema upgrade, login page AnkiWeb step, which login a write uses; AnkiWeb faked),
`test_sync.py` (the whole flow through the login page against Anki's sync server: two people
save their logins, alice's collection is downloaded, both write at once, each phone sees only
its own note, tokens still sync after a restart, no password or sync key in the database).

Checked once by hand (not a test, needs git history): a database and tokens made by the
previously deployed code serve tool calls and refresh under the new code, with and without
`USERS_FILE`; and the previous code still works on the upgraded database.

## Boundaries

- Always: hashes for login secrets; AnkiWeb passwords never stored; sync keys only encrypted;
  no plaintext key material in the database; constant work for unknown usernames.
- Never: change the owner's subject or the server-key derivation; remove or rename auth DB
  columns (add only); let the server upload or replace a collection; key a rate limit on the
  claimed username alone; let a user name reach a path unvalidated.

## Rollout (on the VPS)

1. `git pull`, restart. Check your connector still works (it uses the env login, as before).
2. Optional, to encrypt your own login: in Claude, disconnect and reconnect the connector, log in,
   open "AnkiWeb login" and enter it. Then remove `ANKIWEB_USER`/`ANKIWEB_PASS` from the env file
   and restart. Check `sync` from Claude.
3. Another person: add `[users.luca]` to `/srv/anki-bridge/users.toml` (`chmod 600`), set
   `USERS_FILE` in the env file, restart, run `python auth.py invite luca` and send the code. They
   add the connector, log in as `luca` with the code, choose their secret and enter their AnkiWeb
   login; that login downloads their collection.
