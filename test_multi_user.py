"""A second person on the same server (SPEC-multi-user.md), and that the owner notices nothing.
Offline; the two-account sync check is in test_sync.py. Run: python -m pytest -q"""

import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import auth
import server
from auth import OWNER_SUBJECT, OwnerAuthenticator, UsersAuthenticator, derive_server_key, hash_secret
from server import ConfigError, build_server, load_config
from test_server_auth import PUBLIC, REDIRECT, SECRET, SECRET_HASH, env, make_app, oauth_login, seed_collection, tool
from users import User, UsersFileError, load_users
from core import SyncConfig

ALICE_SECRET = "alice's own long secret"
ALICE_HASH = hash_secret(ALICE_SECRET)


def users_file(text: str, mode: int = 0o600) -> str:
    path = os.path.join(tempfile.mkdtemp(), "users.toml")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def alice_entry() -> str:
    return f"[users.alice]\nsecret_hash = '{ALICE_HASH}'\n"


def multi_env(users_text=None, **kw):
    e = env(**kw)
    e["USERS_FILE"] = users_file(users_text or alice_entry())
    return e


def decks(res) -> str:
    return json.dumps(res["content"])


def login_page(tc):
    """GET /login for a fresh authorization request; returns (html, req)."""
    reg = tc.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none", "client_name": "t"})
    a = tc.get(
        "/authorize",
        params={"response_type": "code", "client_id": reg.json()["client_id"], "redirect_uri": REDIRECT,
                "code_challenge": "x" * 43, "code_challenge_method": "S256", "resource": PUBLIC + "/mcp"},
        follow_redirects=False,
    )
    req = parse_qs(urlparse(a.headers["location"]).query)["req"][0]
    return tc.get("/login", params={"req": req}).text, req


@pytest.fixture()
def two(monkeypatch):
    """A server with the owner and alice, each with a seeded collection of their own. Logging in
    without an AnkiWeb login is allowed here; test_vault.py covers that part."""
    monkeypatch.setattr(server.AnkiWebSetup, "ankiweb_optional", lambda self, s: True)
    cfg = load_config(multi_env())
    seed_collection(cfg, deck="Owner deck")
    seed_collection(cfg, subject="alice", deck="Alice deck")
    app, prov = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        yield tc, cfg, prov


# ---- the owner notices nothing ------------------------------------------------------------


OLD_FORM = (
    '<p><b>test</b> asks for access to your Anki collection. After you log in, your browser goes to'
    ' <b>claude.ai</b>. If you did not just connect Claude to this server, close this page.</p>'
    '<form method="post"><input type="hidden" name="req" value="R">'
    '<input type="password" name="secret" placeholder="Owner secret" autofocus required>'
    "<button>Authorize</button></form>"
)  # auth._login_form as deployed before multi-user, byte for byte


def test_without_users_file_the_login_form_is_the_old_one_plus_the_ankiweb_section():
    """The owner's form is unchanged except for the collapsed AnkiWeb section (encrypted logins)."""
    form = auth._login_form("R", ("test", "claude.ai"), ankiweb=False)
    button = "<button>Authorize</button></form>"
    assert form == OLD_FORM.replace(button, auth._ANKIWEB_FIELDS.format(open="") + button)
    assert auth._login_form("R", ("test", "claude.ai")) == OLD_FORM  # no sync setup: exactly the old one


def test_without_users_file_nothing_changes():
    cfg = load_config(env())
    assert cfg.users == {}
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        page, _ = login_page(tc)
        assert 'name="username"' not in page and 'placeholder="Owner secret"' in page
        assert '<details><summary>AnkiWeb login' in page  # collapsed: the owner need not open it
        # an owner who types a username anyway is still just the owner: the field is ignored
        assert oauth_login(tc, username="whatever")[0].status_code == 200


def test_owner_grant_survives_adding_a_users_file():
    """The upgrade path: the owner logged in before; the service restarts with USERS_FILE.
    Their access token, refresh token and client secret keep working with no new login."""
    e = env()
    cfg = load_config(e)
    seed_collection(cfg, deck="Owner deck")
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        t, cid, csec = oauth_login(tc)
    tok = t.json()

    cfg2 = load_config({**e, "USERS_FILE": users_file(alice_entry())})
    assert derive_server_key(cfg2.owner_secret_hash) == derive_server_key(cfg.owner_secret_hash)
    app2, _ = make_app(cfg2)
    with TestClient(app2, base_url=PUBLIC) as tc:
        assert "Owner deck" in decks(tool(tc, "list_decks", token=tok["access_token"]))
        r = tc.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                    "client_id": cid, "client_secret": csec})
        assert r.status_code == 200, r.text
        assert "Owner deck" in decks(tool(tc, "list_decks", token=r.json()["access_token"]))


def test_owner_logs_in_with_an_empty_or_missing_username(two):
    tc, _, _ = two
    for username in (None, "", "  "):
        t, _, _ = oauth_login(tc, username=username)
        assert t.status_code == 200, (username, t.text)
        assert "Owner deck" in decks(tool(tc, "list_decks", token=t.json()["access_token"]))


def test_owner_env_and_collection_are_untouched_by_users():
    cfg = load_config(multi_env(ANKI_COLLECTION=os.path.join(tempfile.mkdtemp(), "mine.anki2"), ANKIWEB_USER="me@x", ANKIWEB_PASS="p"))
    b = server.Bridges(cfg)
    assert b.collection_path(OWNER_SUBJECT) == cfg.collection
    assert b.for_subject(OWNER_SUBJECT).sync_cfg == SyncConfig("me@x", "p", None)
    assert b.for_subject("alice").sync_cfg is None  # hers is encrypted, handed over per call
    assert b.collection_path("alice") == os.path.join(cfg.data_dir, "alice", "collection.anki2")


# ---- the second person ------------------------------------------------------------------------


def test_with_users_the_login_page_asks_for_a_username(two):
    tc, _, _ = two
    page, _ = login_page(tc)
    assert 'name="username"' in page and "empty for the owner" in page


def test_alice_sees_only_her_collection_and_the_owner_only_his(two):
    tc, _, _ = two
    alice = oauth_login(tc, username="alice", secret=ALICE_SECRET)[0].json()["access_token"]
    owner = oauth_login(tc)[0].json()["access_token"]
    a, o = decks(tool(tc, "list_decks", token=alice)), decks(tool(tc, "list_decks", token=owner))
    assert "Alice deck" in a and "Owner deck" not in a
    assert "Owner deck" in o and "Alice deck" not in o


def test_without_an_ankiweb_login_alice_can_read_but_not_write(two):
    """A write that never reaches AnkiWeb would not show up on her phone. The owner, with no env
    login and nothing saved, keeps writing with sync off, as before."""
    tc, _, _ = two
    alice = oauth_login(tc, username="Alice ", secret=ALICE_SECRET)[0].json()["access_token"]  # phone capitalises
    note = {"deck": "Alice deck", "note_type": "Basic", "fields": {"Front": "hers", "Back": "b"}, "tags": []}
    res = tool(tc, "add_notes", {"notes": [note], "approved_by_user": True}, alice)
    assert res["isError"] and "No AnkiWeb login is unlocked" in decks(res)
    assert json.loads(tool(tc, "find_notes", {"query": "hers"}, alice)["content"][0]["text"])["total"] == 0
    owner = oauth_login(tc)[0].json()["access_token"]
    note["deck"] = "Owner deck"
    res = tool(tc, "add_notes", {"notes": [note], "approved_by_user": True}, owner)
    assert not res.get("isError") and "sync not configured" in decks(res)


@pytest.mark.parametrize(
    "username, secret",
    [
        ("alice", SECRET),  # her name, the owner's secret
        ("", ALICE_SECRET),  # owner (empty name), her secret
        (None, ALICE_SECRET),  # owner-only form, her secret
        ("owner", SECRET),  # the owner logs in with an empty name, not this one
        ("bob", ALICE_SECRET),  # unknown name
        ("bob", SECRET),
        ("alice", ""),
    ],
)
def test_crossed_or_unknown_logins_are_refused(two, username, secret):
    tc, _, _ = two
    r = oauth_login(tc, username=username, secret=secret)[0]
    assert r.status_code == 401 and "Wrong username or secret." in r.text
    assert "code=" not in r.headers.get("location", "")


def test_a_failed_login_keeps_the_typed_username_escaped(two):
    tc, _, _ = two
    r = oauth_login(tc, username='<b>"x', secret="wrong")[0]
    assert r.status_code == 401 and "Wrong username or secret." in r.text
    assert 'value="&lt;b&gt;&quot;x"' in r.text and '<b>"x' not in r.text


def test_failed_logins_with_usernames_still_count_per_ip(two):
    tc, _, _ = two
    for name in ("a", "b", "c", "d", "e"):  # a new name each time does not reset anything
        assert oauth_login(tc, username=name, secret="wrong")[0].status_code == 401
    assert oauth_login(tc, username="alice", secret=ALICE_SECRET)[0].status_code == 429


def test_a_removed_user_is_cut_off_and_the_owner_is_not(monkeypatch):
    monkeypatch.setattr(server.AnkiWebSetup, "ankiweb_optional", lambda self, s: True)
    e = multi_env()
    cfg = load_config(e)
    seed_collection(cfg)
    seed_collection(cfg, subject="alice")
    app, _ = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        alice, acid, acsec = oauth_login(tc, username="alice", secret=ALICE_SECRET)
        owner = oauth_login(tc)[0].json()

    cfg2 = load_config({**e, "USERS_FILE": users_file("")})  # alice removed, service restarted
    app2, _ = make_app(cfg2)
    with TestClient(app2, base_url=PUBLIC) as tc:
        assert tc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"accept": "application/json, text/event-stream",
                                "authorization": f"Bearer {alice.json()['access_token']}"}).status_code == 401
        r = tc.post("/token", data={"grant_type": "refresh_token", "refresh_token": alice.json()["refresh_token"],
                                    "client_id": acid, "client_secret": acsec})
        assert r.status_code == 400
        assert not tool(tc, "list_decks", token=owner["access_token"]).get("isError")


def test_startup_warns_per_person_without_a_collection():
    cfg = load_config(multi_env())
    seed_collection(cfg)
    assert [w for w in server.startup_warnings(cfg) if "for alice" in w]
    seed_collection(cfg, subject="alice")
    assert server.startup_warnings(cfg) == []


# ---- authenticator ---------------------------------------------------------------------------------


def test_users_authenticator_maps_names_to_subjects():
    a = UsersAuthenticator(SECRET_HASH, {"alice": ALICE_HASH, OWNER_SUBJECT: ALICE_HASH})  # 'owner' entry ignored
    assert a.authenticate({"secret": SECRET}) == OWNER_SUBJECT
    assert a.authenticate({"username": "", "secret": SECRET}) == OWNER_SUBJECT
    assert a.authenticate({"username": "ALICE", "secret": ALICE_SECRET}) == "alice"
    assert a.authenticate({"username": "owner", "secret": ALICE_SECRET}) is None
    assert a.authenticate({"username": "owner", "secret": SECRET}) is None
    assert a.authenticate({"username": "alice"}) is None


def test_unknown_usernames_cost_a_scrypt_like_known_ones(monkeypatch):
    calls = []
    real = auth.verify_secret
    monkeypatch.setattr(auth, "verify_secret", lambda s, h: calls.append(h) or real(s, h))
    a = UsersAuthenticator(SECRET_HASH, {"alice": ALICE_HASH})
    a.authenticate({"username": "nobody", "secret": "x"})
    a.authenticate({"username": "alice", "secret": "x"})
    assert len(calls) == 2 and calls[0] not in (SECRET_HASH, ALICE_HASH)


def test_the_owner_only_server_uses_the_owner_only_authenticator(monkeypatch):
    made = []
    real = server.make_login_handler
    monkeypatch.setattr(server, "make_login_handler", lambda p, a, *r: made.append(a) or real(p, a, *r))
    build_server(load_config(env()))
    build_server(load_config(multi_env()))
    assert type(made[0]) is OwnerAuthenticator and type(made[1]) is UsersAuthenticator


# ---- users file ------------------------------------------------------------------------------------


def test_a_valid_users_file_loads():
    assert load_users(users_file(alice_entry())) == {"alice": User("alice", ALICE_HASH)}
    assert load_users(users_file("")) == {}


@pytest.mark.parametrize(
    "text, words",
    [
        ("[users.owner]\nsecret_hash = 'scrypt$1'\n", "not 'owner'"),
        ("[users.Alice]\nsecret_hash = 'scrypt$1'\n", "lowercase"),
        ("[users.'../x']\nsecret_hash = 'scrypt$1'\n", "lowercase"),
        ("[users.alice]\nsecret_hash = 'plain'\n", "auth.py hash alice"),
        ("[users.alice]\n", "auth.py hash alice"),
        ("[users.alice]\nsecret_hash = 'scrypt$1'\nankiweb_user = 'a@x'\nankiweb_pass = 'pw'\n", "do not go in this file"),
        ("[users.alice]\nsecret_hash = 'scrypt$1'\nankiweb_password = 'x'\n", "unknown key"),
        ("[users.alice]\nsecret_hash = 1\n", "auth.py hash alice"),
        ("users = 3\n", "only [users.<name>]"),
        ("[other]\n", "only [users.<name>]"),
        ("[users\n", "not valid TOML"),
    ],
)
def test_a_bad_users_file_is_refused(text, words):
    with pytest.raises(UsersFileError) as e:
        load_users(users_file(text))
    assert words in str(e.value)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_a_users_file_others_can_read_is_refused(mode):
    with pytest.raises(UsersFileError, match="chmod 600"):
        load_users(users_file(alice_entry(), mode))


def test_a_missing_users_file_is_refused():
    with pytest.raises(ConfigError, match="cannot read"):
        load_config({**env(), "USERS_FILE": "/nonexistent/users.toml"})


def test_users_file_problems_stop_the_server():
    with pytest.raises(ConfigError):
        load_config(multi_env("[users.owner]\nsecret_hash = 'scrypt$1'\n"))


def test_users_file_without_auth_is_refused():
    with pytest.raises(ConfigError, match="needs auth"):
        load_config({"ALLOW_INSECURE_NO_AUTH": "1", "USERS_FILE": users_file(alice_entry())})


# ---- tooling -------------------------------------------------------------------------------------


def test_hash_for_a_user_prints_a_users_file_entry():
    out = auth.main(["hash", "alice"], prompt=lambda _: "a long enough secret")
    path = users_file(out + "\n")
    user = load_users(path)["alice"]
    assert auth.verify_secret("a long enough secret", user.secret_hash)
    assert auth.main(["hash"], prompt=lambda _: "a long enough secret").startswith("OWNER_SECRET_HASH='scrypt$")


@pytest.mark.parametrize("argv", [["hash", "owner"], ["hash", "Alice"], ["hash", "a/b"], ["hash", "a", "b"], ["nope"]])
def test_hash_refuses_bad_usernames(argv):
    with pytest.raises(SystemExit):
        auth.main(argv, prompt=lambda _: "a long enough secret")
