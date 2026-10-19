# Deploying to the VPS

Target: `pzk-server` (Ubuntu), `https://anki.panzeilkamp.com/mcp`, behind the existing nginx.
Spec: [SPEC-deploy.md](SPEC-deploy.md) · Tasks: [tasks/todo-deploy.md](tasks/todo-deploy.md).
Commands run as **root** unless the heading says `anki`. Each step says what a good result looks like.

Prerequisite: the `anki` DNS record is **DNS only** (grey cloud) in Cloudflare.

## 1. Host preparation (once)

nix from apt; the daemon must run through its socket, flakes must be on, and the service
user must be in `nix-users` (the daemon socket is group-restricted on purpose).

    systemctl stop nix-daemon.service
    systemctl enable --now nix-daemon.socket
    systemctl status nix-daemon.socket --no-pager      # Active: active (listening)
    grep -q flakes /etc/nix/nix.conf || echo 'experimental-features = nix-command flakes' >> /etc/nix/nix.conf

    useradd --create-home --home-dir /srv/anki-bridge --shell /bin/bash anki
    usermod -aG nix-users anki
    install -d -o anki -g anki -m 700 /srv/anki-bridge/data

Check, as `anki` in a fresh login (`sudo -iu anki`):

    id                                                  # lists nix-users
    git clone https://github.com/DavySc/anki_bridge.git app
    cd app && nix develop --command python -c "from importlib.metadata import version; print(version('anki'), version('mcp'))"
    # 26.9.3 2.3.0   (the first run takes minutes: it downloads nixpkgs and builds .venv)
    nix develop --command python -m pytest -q           # all pass

## 2. Secrets (once)

As `anki`, in `/srv/anki-bridge/app`:

    nix develop --command python auth.py hash           # prompts for the owner secret, prints the hash

Pick a long secret and keep it in your password manager; you type it at the login page.
Then create the env file from the template (names only in the repo):

    cp deploy/env.example /srv/anki-bridge/env
    chmod 600 /srv/anki-bridge/env
    nano /srv/anki-bridge/env                           # paste OWNER_SECRET_HASH, leave ANKIWEB_* empty for now
    stat -c '%a %U' /srv/anki-bridge/env                # 600 anki

The hash contains `$` characters. `EnvironmentFile` takes it literally; do not quote it.

## 3. The service

    install -m 644 /srv/anki-bridge/app/deploy/anki-bridge.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now anki-bridge
    systemctl status anki-bridge --no-pager             # active (running); first start takes a while
    journalctl -u anki-bridge -n 30 --no-pager          # no errors. A "collection missing" warning is expected

Loopback checks (nginx not involved yet):

    curl -si -X POST http://127.0.0.1:8765/mcp | head -5     # 401 and a WWW-Authenticate header
    curl -s -H 'Host: anki.panzeilkamp.com' http://127.0.0.1:8765/.well-known/oauth-authorization-server
    ss -ltn | grep 8765                                      # 127.0.0.1:8765 only

If the unit fails with a permission error, the journal names the path. Add only that path to
`ReadWritePaths` in `deploy/anki-bridge.service`, commit, and tell me.

## 4. nginx and the certificate

The existing `panzeilkamp.com` blocks are not edited. This adds a separate site.

    cp /srv/anki-bridge/app/deploy/nginx-anki-http.conf /etc/nginx/sites-available/anki-bridge
    ln -s /etc/nginx/sites-available/anki-bridge /etc/nginx/sites-enabled/anki-bridge
    nginx -t && systemctl reload nginx
    certbot certonly --nginx -d anki.panzeilkamp.com          # certificate saved under /etc/letsencrypt/live/anki.panzeilkamp.com/

Then swap in the full block and reload (reload, not restart: the other site stays up):

    cp /srv/anki-bridge/app/deploy/nginx-anki.conf /etc/nginx/sites-available/anki-bridge
    nginx -t && systemctl reload nginx
    certbot renew --dry-run                                   # succeeds

Check from your own machine (not the VPS), and the other site:

    deploy/check.sh https://anki.panzeilkamp.com              # all checks passed
    curl -sI https://panzeilkamp.com | head -1                # still 200

## 5. Reboot, crash, update

    reboot                                                    # then: deploy/check.sh https://anki.panzeilkamp.com
    kill -9 "$(systemctl show -p MainPID --value anki-bridge)"; sleep 8; systemctl is-active anki-bridge   # active

Update, as `anki` then root:

    cd /srv/anki-bridge/app && git pull
    systemctl restart anki-bridge
    deploy/check.sh https://anki.panzeilkamp.com

A changed `OWNER_SECRET_HASH` makes Claude register again; that is expected.

## 6. Connect Claude, then 7. First real use

See [tasks/todo-deploy.md](tasks/todo-deploy.md) Tasks 6 and 7 and the README section
"First real use". Do not run `bootstrap.py upload` on the VPS, ever.

## 8. Encrypt your AnkiWeb login (optional, recommended)

Spec: [SPEC-multi-user.md](SPEC-multi-user.md). After updating (step 5) nothing changes yet: the
env login keeps working. To move it out of the env file:

1. In Claude, disconnect and reconnect the Anki connector. On the login page, open
   "AnkiWeb login", enter it, and authorize. Expected: back in Claude, no error.
2. As `anki`: remove the `ANKIWEB_USER` and `ANKIWEB_PASS` lines from `/srv/anki-bridge/env`.
3. As root: `systemctl restart anki-bridge`, then call `sync` from Claude. Expected: `synced`.

If step 3 says "No AnkiWeb login is unlocked for this connection", Claude is still using a
connection from before step 1: repeat step 1.

## 9. Add another person (optional)

First update and restart (step 5) *without* a users file, and check your own connector still
works.

As `anki`, in `/srv/anki-bridge/app`:

    nix develop --command python auth.py hash alice     # they type their secret; prints [users.alice] ...
    cp deploy/users.example.toml /srv/anki-bridge/users.toml
    chmod 600 /srv/anki-bridge/users.toml
    nano /srv/anki-bridge/users.toml                    # paste the entry (no AnkiWeb login goes here)
    echo 'USERS_FILE=/srv/anki-bridge/users.toml' >> /srv/anki-bridge/env

Then, as root:

    systemctl restart anki-bridge
    journalctl -u anki-bridge -n 30 --no-pager          # no "Refusing to start"
    deploy/check.sh https://anki.panzeilkamp.com

They add `https://anki.panzeilkamp.com/mcp` as a connector in their own Claude, log in with
username `alice`, open "AnkiWeb login" and enter theirs. That login downloads their collection
(it can take a minute for a big one); the journal says `downloading the collection of alice`.
Your login page now has a username field: leave it empty.

To remove someone: delete their table, restart. Their tokens stop working at once.
