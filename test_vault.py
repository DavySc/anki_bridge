"""Encrypted AnkiWeb logins (vault.py and its use in auth.py and server.py). Offline: AnkiWeb is
faked at ankiweb_login and download_new; test_sync.py runs the same flow against a real sync
server. Run: python -m pytest -q"""

import json
import os
import sqlite3
import tempfile
import threading

import pytest
from starlette.testclient import TestClient

import auth
import server
import vault
from auth import OWNER_SUBJECT, LoginProblem, OwnerProvider, hash_secret
from core import Bridge, BridgeError, SyncConfig
from server import load_config
from test_multi_user import ALICE_SECRET, alice_entry, decks, login_page, multi_env
from test_server_auth import PUBLIC, SECRET, env, make_app, oauth_login, seed_collection, tool

KEY = b"k" * 32
CRED = {"username": "alice@x", "hkey": "HKEY-alice", "endpoint": ""}


# ---- vault.py -----------------------------------------------------------------------------


def test_seal_round_trips_and_refuses_anything_else():
    k = vault.new_key()
    blob = vault.seal(k, b"sync key", "ankiweb|alice")
    assert vault.unseal(k, blob, "ankiweb|alice") == b"sync key"
    assert b"sync key" not in blob
    assert vault.unseal(vault.new_key(), blob, "ankiweb|alice") is None  # wrong key
    assert vault.unseal(k, blob, "ankiweb|bob") is None  # moved to another person
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    assert vault.unseal(k, tampered, "ankiweb|alice") is None
    assert vault.unseal(k, None, "x") is None and vault.unseal(k, b"\x02" + blob[1:], "ankiweb|alice") is None
    assert vault.seal(k, b"x", "c") != vault.seal(k, b"x", "c")  # fresh nonce each time


def test_derived_keys_differ_from_what_the_database_stores():
    salt = os.urandom(16)
    assert vault.secret_key("pw", salt) == vault.secret_key("pw", salt) != vault.secret_key("pw", os.urandom(16))
    token = "t" * 43
    assert vault.token_key(token).hex() != auth._sha256(token)  # the lookup hash is not the key


# ---- provider: the key travels login -> code -> tokens -> refresh ---------------------------


def provider(path=None, **kw):
    path = path or os.path.join(tempfile.mkdtemp(), "auth.sqlite3")
    return OwnerProvider(path, server_key=KEY, public_url="https://anki.example", **kw)


def test_a_saved_login_opens_only_with_its_secret():
    p = provider()
    assert not p.has_vault("alice") and p.unlock_vault("alice", ALICE_SECRET) is None
    dk = p.save_vault("alice", ALICE_SECRET, CRED)
    assert p.has_vault("alice")
    assert p.unlock_vault("alice", ALICE_SECRET) == dk
    assert p.unlock_vault("alice", "another secret") is None
    assert p.vault_credential("alice", dk) == CRED
    assert p.vault_credential("alice", vault.new_key()) is None
    assert p.vault_credential("bob", dk) is None


def test_saving_again_keeps_the_data_key_so_other_connections_stay_unlocked():
    p = provider()
    dk = p.save_vault("alice", ALICE_SECRET, CRED)
    assert p.save_vault("alice", ALICE_SECRET, {**CRED, "hkey": "NEW"}) == dk
    assert p.vault_credential("alice", dk)["hkey"] == "NEW"
    # a changed secret cannot open the old key: a new one, and the old connections are locked out
    dk2 = p.save_vault("alice", "a changed secret!", CRED)
    assert dk2 != dk and p.vault_credential("alice", dk) is None


def test_one_ankiweb_account_cannot_be_saved_by_two_people():
    p = provider()
    p.save_vault("alice", ALICE_SECRET, CRED)
    with pytest.raises(LoginProblem, match="already used"):
        p.save_vault("bob", "bob's secret", {**CRED, "username": " Alice@X "})
    p.save_vault("alice", ALICE_SECRET, CRED)  # saving your own again is fine


def grant(p, subject, data_key, client="c1"):
    """A login's code exchanged for tokens, as the SDK does it."""
    from auth import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from test_server_auth import REDIRECT

    import asyncio

    run = asyncio.run
    if run(p.get_client(client)) is None:
        run(p.register_client(OAuthClientInformationFull(client_id=client, redirect_uris=[REDIRECT], token_endpoint_auth_method="none")))
    c = run(p.get_client(client))
    params = AuthorizationParams(state=None, scopes=[], code_challenge="x", redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True)
    from urllib.parse import parse_qs, urlparse

    req = parse_qs(urlparse(run(p.authorize(c, params))).query)["req"][0]
    code = parse_qs(urlparse(p.complete_login(req, subject, data_key)).query)["code"][0]
    tok = run(p.exchange_authorization_code(c, run(p.load_authorization_code(c, code))))
    return c, tok


def test_the_data_key_survives_code_exchange_and_every_refresh():
    import asyncio

    p = provider()
    dk = p.save_vault("alice", ALICE_SECRET, CRED)
    c, tok = grant(p, "alice", dk)
    assert p.data_key(tok.access_token) == dk
    for _ in range(3):
        tok = asyncio.run(p.exchange_refresh_token(c, asyncio.run(p.load_refresh_token(c, tok.refresh_token)), []))
        assert p.data_key(tok.access_token) == dk
    assert p.data_key("not a token") is None


def test_a_grant_without_a_key_has_none_and_still_works():
    import asyncio

    p = provider()
    c, tok = grant(p, OWNER_SUBJECT, None)
    assert p.data_key(tok.access_token) is None
    assert asyncio.run(p.load_access_token(tok.access_token)).subject == OWNER_SUBJECT


def test_expired_or_removed_tokens_carry_no_key():
    clock = [1_000_000.0]
    allowed = {"alice"}
    p = provider(now=lambda: clock[0], subject_ok=lambda s: s in allowed)
    dk = p.save_vault("alice", ALICE_SECRET, CRED)
    _, tok = grant(p, "alice", dk)
    allowed.clear()
    assert p.data_key(tok.access_token) is None
    allowed.add("alice")
    clock[0] += auth.ACCESS_TTL + 1
    assert p.data_key(tok.access_token) is None


def test_blobs_moved_to_another_person_in_the_database_do_not_open():
    """Someone who can write the database still cannot hand alice's login or key to bob."""
    p = provider()
    dk = p.save_vault("alice", ALICE_SECRET, CRED)
    p.save_vault("bob", "bob's own secret", {**CRED, "username": "bob@x"})
    _, tok = grant(p, "alice", dk)
    p._exec("UPDATE vaults SET credential = (SELECT credential FROM vaults WHERE subject = 'alice') WHERE subject = 'bob'")
    assert p.vault_credential("bob", dk) is None
    p._exec("UPDATE access_tokens SET subject = 'bob'")
    assert p.data_key(tok.access_token) is None


def test_the_database_holds_no_sync_key_password_or_data_key_in_the_clear():
    p = provider()
    dk = p.save_vault("alice", ALICE_SECRET, CRED)
    _, tok = grant(p, "alice", dk)
    raw = open(p.path, "rb").read()
    for secret in (CRED["hkey"].encode(), CRED["username"].encode(), dk, ALICE_SECRET.encode(), tok.access_token.encode()):
        assert secret not in raw, secret


def test_an_auth_database_from_before_gets_the_new_column_and_its_grants_keep_working():
    """The deployed database has no wrapped_key columns and no vaults table."""
    import asyncio

    path = os.path.join(tempfile.mkdtemp(), "auth.sqlite3")
    p = provider(path)
    c, tok = grant(p, OWNER_SUBJECT, None)
    p._db.close()
    db = sqlite3.connect(path)
    for table in ("codes", "access_tokens", "refresh_tokens"):  # back to the old schema
        db.execute(f"ALTER TABLE {table} DROP COLUMN wrapped_key")
    db.execute("DROP TABLE vaults")
    db.commit()
    db.close()

    p2 = provider(path)
    assert asyncio.run(p2.load_access_token(tok.access_token)).subject == OWNER_SUBJECT
    assert p2.data_key(tok.access_token) is None
    new = asyncio.run(p2.exchange_refresh_token(c, asyncio.run(p2.load_refresh_token(c, tok.refresh_token)), []))
    assert asyncio.run(p2.load_access_token(new.access_token)) is not None
    provider(path)  # opening again does not try to add the column twice


# ---- the login page's AnkiWeb step, with AnkiWeb faked ---------------------------------------


@pytest.fixture()
def fake_ankiweb(monkeypatch):
    """ankiweb_login accepts password 'right' and returns a sync key; download_new seeds a collection."""
    calls = {"login": [], "download": []}

    def login(username, password, endpoint=None):
        calls["login"].append(username)
        if password != "right":
            raise BridgeError("AnkiWeb did not accept that email and password.")
        return SyncConfig(username, "", endpoint, f"HKEY-{username}")

    def download(path, sync):
        calls["download"].append((path, sync))
        from anki.collection import Collection

        os.makedirs(os.path.dirname(path), exist_ok=True)
        col = Collection(path)
        col.decks.id(f"Downloaded for {sync.username}")
        col.close()
        return "FULL_DOWNLOAD"

    monkeypatch.setattr(server, "ankiweb_login", login)
    monkeypatch.setattr(server, "download_new", download)
    return calls


@pytest.fixture()
def site(fake_ankiweb, monkeypatch):
    """Owner (no env login) and alice; Bridge._sync stubbed to report whose login it used."""
    monkeypatch.setattr(Bridge, "_sync", lambda self, col: f"synced as {self.sync_cfg.username}" if self.sync_cfg else "sync not configured")
    cfg = load_config(multi_env())
    seed_collection(cfg, deck="Owner deck")
    app, prov = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        yield tc, cfg, prov, fake_ankiweb


def alice_login(tc, secret=ALICE_SECRET, user="", password=""):
    return oauth_login(tc, username="alice", secret=secret, extra={"ankiweb_user": user, "ankiweb_pass": password})[0]


NOTE = {"deck": "Downloaded for alice@x", "note_type": "Basic", "fields": {"Front": "f", "Back": "b"}, "tags": []}


def test_the_login_page_offers_the_ankiweb_fields(site):
    tc, *_ = site
    page, _ = login_page(tc)
    assert 'name="ankiweb_user"' in page and 'name="ankiweb_pass"' in page and "encrypted" in page


def test_alice_saves_her_login_her_collection_is_downloaded_and_her_writes_use_it(site):
    tc, cfg, prov, calls = site
    r = alice_login(tc, user="alice@x", password="right")
    assert r.status_code == 200, r.text
    assert calls["download"][0][0] == os.path.join(cfg.data_dir, "alice", "collection.anki2")
    token = r.json()["access_token"]
    res = tool(tc, "add_notes", {"notes": [NOTE], "approved_by_user": True}, token)
    assert not res.get("isError"), res
    assert "synced as alice@x" in decks(res)
    assert "right" not in open(os.path.join(cfg.data_dir, "auth.sqlite3"), "rb").read().decode("latin-1")


def test_later_logins_unlock_the_saved_login_without_typing_it(site):
    tc, _, _, calls = site
    alice_login(tc, user="alice@x", password="right")
    r = alice_login(tc)  # no AnkiWeb fields this time
    assert r.status_code == 200, r.text
    assert len(calls["login"]) == 1 and len(calls["download"]) == 1  # nothing asked of AnkiWeb again
    res = tool(tc, "sync", token=r.json()["access_token"])
    assert "synced as alice@x" in decks(res)


def test_alice_must_enter_an_ankiweb_login_the_first_time(site):
    tc, *_ = site
    r = alice_login(tc)
    assert r.status_code == 400 and "Enter your AnkiWeb login" in r.text and "<details open>" in r.text
    assert 'value="alice"' in r.text  # the username stays filled in


@pytest.mark.parametrize("user, password", [("alice@x", ""), ("", "right")])
def test_half_an_ankiweb_login_is_refused(site, user, password):
    tc, *_ = site
    r = alice_login(tc, user=user, password=password)
    assert r.status_code == 400 and "both your AnkiWeb email and password" in r.text


def test_a_rejected_ankiweb_login_is_shown_and_is_not_a_failed_login(site):
    tc, _, prov, _ = site
    for _ in range(7):  # more than the failed-login limit
        r = alice_login(tc, user="alice@x", password="wrong")
        assert r.status_code == 400 and "did not accept" in r.text
    assert not prov.has_vault("alice")
    assert alice_login(tc, user="alice@x", password="right").status_code == 200


def test_the_owners_ankiweb_account_cannot_be_saved_by_someone_else(fake_ankiweb):
    cfg = load_config(multi_env(ANKIWEB_USER="me@x", ANKIWEB_PASS="p"))
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        r = alice_login(tc, user="ME@x", password="right")
    assert r.status_code == 400 and "already used" in r.text and fake_ankiweb["login"] == []


def test_a_changed_secret_asks_for_the_ankiweb_login_again(site, monkeypatch):
    tc, cfg, prov, _ = site
    alice_login(tc, user="alice@x", password="right")
    new_secret = "alice changed her secret"
    cfg.users["alice"] = server.User("alice", hash_secret(new_secret))  # as if the users file changed
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc2:
        r = alice_login(tc2, secret=new_secret)
        assert r.status_code == 400 and "cannot be opened with this secret" in r.text
        assert alice_login(tc2, secret=new_secret, user="alice@x", password="right").status_code == 200


def test_no_download_when_the_collection_exists(site):
    tc, cfg, _, calls = site
    seed_collection(cfg, subject="alice")
    assert alice_login(tc, user="alice@x", password="right").status_code == 200
    assert calls["download"] == []


def test_a_failed_download_is_shown_and_the_next_login_tries_again(site, monkeypatch):
    tc, _, _, calls = site

    def broken(path, sync):
        raise BridgeError("Expected AnkiWeb to need a full download here, but it said NO_CHANGES. Nothing was changed.")

    real = server.download_new
    monkeypatch.setattr(server, "download_new", broken)
    r = alice_login(tc, user="alice@x", password="right")
    assert r.status_code == 400 and "Downloading your collection from AnkiWeb failed" in r.text
    monkeypatch.setattr(server, "download_new", real)
    assert alice_login(tc).status_code == 200  # the login was saved; this login downloads
    assert len(calls["download"]) == 1


def test_two_downloads_for_one_person_do_not_run_at_once():
    cfg = load_config(multi_env())
    setup = server.AnkiWebSetup(cfg, server.Bridges(cfg))
    started, release = threading.Event(), threading.Event()

    def slow(path, sync):
        started.set()
        release.wait(5)
        return "FULL_DOWNLOAD"

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as m:
        m.setattr(server, "download_new", slow)
        t = threading.Thread(target=setup.prepare, args=("alice", CRED))
        t.start()
        started.wait(5)
        with pytest.raises(LoginProblem, match="being downloaded already"):
            setup.prepare("alice", CRED)
        release.set()
        t.join(5)


# ---- which login a write uses ----------------------------------------------------------------


def test_a_connection_made_before_the_login_was_saved_cannot_write(site):
    """Its tokens carry no key. Reads still work; writes say to log in again."""
    tc, _, prov, _ = site
    alice_login(tc, user="alice@x", password="right")
    import asyncio

    p = prov
    c, old = grant(p, "alice", None, client="old-client")
    assert not tool(tc, "list_decks", token=old.access_token).get("isError")
    res = tool(tc, "add_notes", {"notes": [NOTE], "approved_by_user": True}, old.access_token)
    assert res["isError"] and "No AnkiWeb login is unlocked" in decks(res)


def test_the_owner_with_env_login_uses_it_as_before(fake_ankiweb, monkeypatch):
    monkeypatch.setattr(Bridge, "_sync", lambda self, col: f"synced as {self.sync_cfg.username}")
    cfg = load_config(env(ANKIWEB_USER="me@x", ANKIWEB_PASS="p"))
    seed_collection(cfg)
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        token = oauth_login(tc)[0].json()["access_token"]  # no AnkiWeb fields, as before
        res = tool(tc, "sync", token=token)
    assert "synced as me@x" in decks(res) and fake_ankiweb["login"] == []


def test_the_owner_moves_from_env_to_a_saved_login(fake_ankiweb, monkeypatch):
    """The rollout: log in once more entering the AnkiWeb login (env still set), then remove the
    env vars and restart. The new connection keeps working; the old one can no longer write."""
    monkeypatch.setattr(Bridge, "_sync", lambda self, col: f"synced as {self.sync_cfg.username}")
    e = env(ANKIWEB_USER="me@x", ANKIWEB_PASS="p")
    cfg = load_config(e)
    seed_collection(cfg)
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        old = oauth_login(tc)[0].json()["access_token"]
        new = oauth_login(tc, extra={"ankiweb_user": "me@x", "ankiweb_pass": "right"})[0].json()["access_token"]
        assert "synced as me@x" in decks(tool(tc, "sync", token=new))  # env still wins here

    cfg2 = load_config({k: v for k, v in e.items() if not k.startswith("ANKIWEB_")})
    app2, _ = make_app(cfg2)
    with TestClient(app2, base_url=PUBLIC) as tc:
        res = tool(tc, "sync", token=new)
        assert not res.get("isError") and "synced as me@x" in decks(res)
        res = tool(tc, "sync", token=old)
        assert res["isError"] and "No AnkiWeb login is unlocked" in decks(res)


def test_the_server_starts_no_full_sync_but_the_download_into_nothing():
    """bootstrap.py holds the only full-sync calls; the server reaches only download_new."""
    here = os.path.dirname(__file__)
    for name in ("core.py", "server.py", "auth.py"):
        with open(os.path.join(here, name)) as f:
            text = f.read()
        assert "full_upload_or_download" not in text and "upload=True" not in text, name
    with open(os.path.join(here, "server.py")) as f:
        assert "from bootstrap import download_new\n" in f.read()
