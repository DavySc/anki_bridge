"""HTTP-level checks of server.py: auth in front of the tools. Run: python -m pytest -q"""

import base64
import hashlib
import json
import os
import tempfile
from urllib.parse import parse_qs, urlparse

import pytest
from anki.collection import Collection
from starlette.testclient import TestClient

import server
from auth import OWNER_SUBJECT, hash_secret
from server import Bridges, Config, ConfigError, build_server, load_config, transport_security
from users import User

PUBLIC = "https://anki.example"
SECRET = "correct horse battery"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
HEADERS = {"accept": "application/json, text/event-stream"}
SECRET_HASH = hash_secret(SECRET)


def env(**kw):
    base = {"PUBLIC_URL": PUBLIC, "OWNER_SECRET_HASH": SECRET_HASH, "DATA_DIR": tempfile.mkdtemp()}
    base.update(kw)
    return base


# ---- startup guard -------------------------------------------------------------------


@pytest.mark.parametrize(
    "e",
    [
        {},
        {"PUBLIC_URL": PUBLIC},
        {"OWNER_SECRET_HASH": SECRET_HASH},
        {"ALLOW_INSECURE_NO_AUTH": "1", "HOST": "0.0.0.0"},
        {"ALLOW_INSECURE_NO_AUTH": "1", "HOST": "192.168.1.5"},
        {"ALLOW_INSECURE_NO_AUTH": "0"},
        {"PUBLIC_URL": "http://anki.example", "OWNER_SECRET_HASH": SECRET_HASH},
        {"PUBLIC_URL": PUBLIC + "/mcp", "OWNER_SECRET_HASH": SECRET_HASH},
        {"PUBLIC_URL": PUBLIC, "OWNER_SECRET_HASH": "plaintext-password"},
    ],
)
def test_refuses_to_start_unsafely(e):
    with pytest.raises(ConfigError):
        load_config(e)


def test_insecure_flag_allowed_on_loopback_only_without_auth():
    cfg = load_config({"ALLOW_INSECURE_NO_AUTH": "1"})
    assert not cfg.auth_enabled
    assert not load_config({"ALLOW_INSECURE_NO_AUTH": "1", "HOST": "::1"}).auth_enabled


def test_full_config_enables_auth_and_http_loopback_is_ok_for_dev():
    assert load_config(env()).auth_enabled
    assert load_config(env(PUBLIC_URL="http://127.0.0.1:8000")).auth_enabled


def test_auth_wins_over_the_insecure_flag_when_configured():
    assert load_config(env(ALLOW_INSECURE_NO_AUTH="1")).auth_enabled


# ---- client helper ---------------------------------------------------------------------


def make_app(cfg):
    mcp, prov = build_server(cfg)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=transport_security(cfg)
    )
    return app, prov


def rpc(tc, method, params=None, token=None, id=1):
    h = dict(HEADERS)
    if token:
        h["authorization"] = f"Bearer {token}"
    return tc.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}, headers=h)


def tool(tc, name, args=None, token=None):
    r = rpc(tc, "tools/call", {"name": name, "arguments": args or {}}, token)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def oauth_login(tc, secret=SECRET, client_id=None, client_secret=None, verifier="v" * 50, username=None, extra=None):
    """register -> authorize -> login -> token. Returns (token_json, client_id, client_secret).
    username None posts the owner-only form (no username field); extra adds form fields."""
    if client_id is None:
        reg = tc.post(
            "/register",
            json={
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "test",
            },
        )
        assert reg.status_code == 201, reg.text
        client_id, client_secret = reg.json()["client_id"], reg.json()["client_secret"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    a = tc.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
            "resource": PUBLIC + "/mcp",
        },
        follow_redirects=False,
    )
    assert a.status_code == 302, a.text
    login_url = a.headers["location"]
    assert urlparse(login_url).path == "/login"
    req = parse_qs(urlparse(login_url).query)["req"][0]
    form = {"req": req, "secret": secret} if username is None else {"req": req, "username": username, "secret": secret}
    form.update(extra or {})
    l = tc.post("/login", data=form, follow_redirects=False)
    if l.status_code != 302:
        return l, client_id, client_secret
    code = parse_qs(urlparse(l.headers["location"]).query)["code"][0]
    t = tc.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "resource": PUBLIC + "/mcp",
        },
    )
    return t, client_id, client_secret


def seed_collection(cfg, subject=OWNER_SUBJECT, deck="Physics", notes=0):
    """A subject's collection as bootstrap.py would leave it, with `notes` Basic notes in `deck`.
    The server never creates one itself."""
    path = Bridges(cfg).collection_path(subject)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    col = Collection(path)
    did = col.decks.id(deck)
    for i in range(notes):
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = f"question {i}", "answer"
        col.add_note(note, did)
    col.close()


@pytest.fixture()
def served():
    cfg = load_config(env())
    seed_collection(cfg)
    app, prov = make_app(cfg)
    with TestClient(app, base_url=PUBLIC) as tc:
        yield tc, cfg, prov


# ---- the 401 wall ------------------------------------------------------------------------


def test_unauthenticated_mcp_is_401_with_metadata_pointer(served):
    tc, _, _ = served
    r = rpc(tc, "tools/list")
    assert r.status_code == 401
    assert 'resource_metadata="https://anki.example/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]


def test_metadata_documents_use_the_public_url(served):
    tc, _, _ = served
    pr = tc.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"] == PUBLIC + "/mcp" and pr["authorization_servers"] == [PUBLIC]
    asm = tc.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == PUBLIC and asm["registration_endpoint"] == PUBLIC + "/register"
    assert asm["authorization_endpoint"] == PUBLIC + "/authorize" and asm["revocation_endpoint"] == PUBLIC + "/revoke"


@pytest.mark.parametrize("auth", ["", "Bearer", "Bearer ", "Basic abc", "Bearer not-a-real-token", "bearer\x00x"])
def test_malformed_or_unknown_credentials_are_401(served, auth):
    tc, _, _ = served
    h = dict(HEADERS)
    h["authorization"] = auth
    r = tc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h)
    assert r.status_code == 401


def test_tool_code_is_never_reached_without_a_valid_token(served, monkeypatch):
    tc, _, _ = served
    called = []
    monkeypatch.setattr(server.Bridges, "for_subject", lambda self, s: called.append(s) or None)
    rpc(tc, "tools/call", {"name": "list_decks", "arguments": {}})
    rpc(tc, "tools/call", {"name": "list_decks", "arguments": {}}, token="garbage")
    assert called == []


def test_login_page_is_reachable_without_a_token(served):
    tc, _, _ = served
    assert tc.get("/login?req=x").status_code == 400  # our page, not a 401


# ---- the whole flow ----------------------------------------------------------------------


def test_full_flow_register_login_token_tool_refresh_revoke(served):
    tc, _, _ = served
    t, cid, csec = oauth_login(tc)
    assert t.status_code == 200, t.text
    tok = t.json()
    assert tok["token_type"].lower() == "bearer" and tok["refresh_token"]

    res = tool(tc, "list_decks", token=tok["access_token"])
    assert not res.get("isError"), res
    assert any("Default" in json.dumps(c) for c in res["content"])

    r = tc.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                "client_id": cid, "client_secret": csec})
    assert r.status_code == 200, r.text
    new = r.json()
    assert rpc(tc, "tools/list", token=tok["access_token"]).status_code == 401  # rotated out
    assert rpc(tc, "tools/list", token=new["access_token"]).status_code == 200

    rv = tc.post("/revoke", data={"token": new["access_token"], "client_id": cid, "client_secret": csec})
    assert rv.status_code == 200
    assert rpc(tc, "tools/list", token=new["access_token"]).status_code == 401
    again = tc.post("/token", data={"grant_type": "refresh_token", "refresh_token": new["refresh_token"],
                                    "client_id": cid, "client_secret": csec})
    assert again.status_code == 400


def test_wrong_owner_secret_gets_no_token(served):
    tc, _, _ = served
    r, _, _ = oauth_login(tc, secret="wrong")
    assert r.status_code == 401


def test_replaying_the_refresh_token_revokes_the_session(served):
    tc, _, _ = served
    t, cid, csec = oauth_login(tc)
    tok = t.json()
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid, "client_secret": csec}
    new = tc.post("/token", data=form).json()
    assert tc.post("/token", data=form).status_code == 400  # replay
    assert rpc(tc, "tools/list", token=new["access_token"]).status_code == 401  # whole grant gone


def test_token_for_another_resource_is_refused(served):
    tc, cfg, prov = served
    reg = tc.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post",
                                     "grant_types": ["authorization_code"], "response_types": ["code"]})
    cid = reg.json()["client_id"]
    a = tc.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                     "code_challenge": "x" * 43, "code_challenge_method": "S256",
                                     "resource": "https://evil.example/mcp"}, follow_redirects=False)
    assert a.status_code == 302 and "error=invalid_target" in a.headers["location"]


def test_dcr_rejects_foreign_redirect_uris(served):
    tc, _, _ = served
    r = tc.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "token_endpoint_auth_method": "client_secret_post",
                                   "grant_types": ["authorization_code"], "response_types": ["code"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_client_secret_is_not_in_the_auth_database(served):
    tc, cfg, prov = served
    t, cid, csec = oauth_login(tc)
    assert t.status_code == 200
    raw = open(prov.path, "rb").read().decode("latin-1")
    for secret in (csec, t.json()["access_token"], t.json()["refresh_token"], SECRET):
        assert secret not in raw
    # a wrong client secret is refused
    bad = tc.post("/token", data={"grant_type": "refresh_token", "refresh_token": t.json()["refresh_token"],
                                  "client_id": cid, "client_secret": "nope"})
    assert bad.status_code == 401


# ---- subject -> Bridge seam ----------------------------------------------------------------


def test_subject_reaches_a_sync_tool_in_a_worker_thread(served, monkeypatch):
    tc, _, _ = served
    seen = []
    orig = server.Bridges.for_subject
    monkeypatch.setattr(server.Bridges, "for_subject", lambda self, s: seen.append(s) or orig(self, s))
    t, _, _ = oauth_login(tc)
    tool(tc, "list_decks", token=t.json()["access_token"])
    assert seen == [OWNER_SUBJECT]


def test_each_subject_gets_its_own_collection_path():
    cfg = Config(data_dir=tempfile.mkdtemp())
    b = Bridges(cfg)
    owner, other = b.for_subject(OWNER_SUBJECT), b.for_subject("other")
    assert owner is not other and b.for_subject("other") is other
    assert owner.path == os.path.join(cfg.data_dir, "owner", "collection.anki2")
    assert other.path == os.path.join(cfg.data_dir, "other", "collection.anki2")
    assert other.sync_cfg is None


def test_anki_collection_env_overrides_only_the_owner_path():
    d = tempfile.mkdtemp()
    cfg = Config(data_dir=d, collection=os.path.join(d, "mine.anki2"))
    b = Bridges(cfg)
    assert b.for_subject(OWNER_SUBJECT).path == cfg.collection
    assert b.for_subject("other").path == os.path.join(d, "other", "collection.anki2")


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a b", "x" * 65, "ö"])
def test_subject_cannot_escape_the_data_directory(bad):
    with pytest.raises(server.BridgeError):
        Bridges(Config(data_dir=tempfile.mkdtemp())).for_subject(bad)


def test_other_subject_token_reaches_other_bridge(monkeypatch):
    """With a second authenticator issuing subject 'other', tools resolve a different Bridge."""

    class Other:
        def authenticate(self, c):
            return "other" if c.get("secret") == "other-pw" else None

    cfg = load_config(env())
    cfg.users = {"other": User("other", hash_secret("unused"))}  # tokens only work for configured people
    monkeypatch.setattr(server.AnkiWebSetup, "ankiweb_optional", lambda self, s: True)  # the seam, not AnkiWeb
    seed_collection(cfg, subject="other", deck="Only in other")
    mcp, _ = build_server(cfg, authenticator=Other())
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                  transport_security=transport_security(cfg))
    seen = []
    orig = server.Bridges.for_subject
    monkeypatch.setattr(server.Bridges, "for_subject", lambda self, s: seen.append(s) or orig(self, s))
    with TestClient(app, base_url=PUBLIC) as tc:
        t, _, _ = oauth_login(tc, secret="other-pw")
        assert t.status_code == 200, t.text
        res = tool(tc, "list_decks", token=t.json()["access_token"])
        assert oauth_login(tc, secret=SECRET)[0].status_code == 401  # the owner secret is not 'other'
    assert seen == ["other"]
    assert "Only in other" in json.dumps(res["content"])


def test_startup_warns_when_the_owner_collection_is_missing():
    cfg = load_config(env())
    assert any("bootstrap.py download" in w for w in server.startup_warnings(cfg))
    seed_collection(cfg)
    assert server.startup_warnings(cfg) == []


# ---- no-auth dev mode ----------------------------------------------------------------------


def test_insecure_dev_mode_serves_tools_for_the_owner_without_a_token():
    cfg = load_config({"ALLOW_INSECURE_NO_AUTH": "1", "DATA_DIR": tempfile.mkdtemp()})
    seed_collection(cfg)
    app, prov = make_app(cfg)
    assert prov is None
    with TestClient(app, base_url="http://127.0.0.1:8000") as tc:
        res = tool(tc, "list_decks")
        assert not res.get("isError")


# ---- approval guard unchanged -----------------------------------------------------------------


def test_approved_by_user_guard_still_applies_with_a_valid_token(served):
    tc, _, _ = served
    t, _, _ = oauth_login(tc)
    res = tool(tc, "add_notes", {"notes": []}, token=t.json()["access_token"])
    assert res["isError"] and "Not approved" in json.dumps(res["content"])


def test_bridge_error_messages_reach_the_model(served):
    """mcp 2.x masks ordinary exceptions; BridgeError text is meant for Claude (GOALS: reject and report)."""
    tc, _, _ = served
    t, _, _ = oauth_login(tc)
    tok = t.json()["access_token"]
    res = tool(tc, "update_note", {"note_id": 123456, "fields": {"Front": "x"}, "approved_by_user": True}, tok)
    assert res["isError"]
    assert "No note with id 123456." in json.dumps(res["content"])


@pytest.mark.parametrize("subject", [None, ""])
def test_missing_subject_fails_closed_under_auth(served, monkeypatch, subject):
    from mcp.server.auth.provider import AccessToken

    tc, _, _ = served
    t, _, _ = oauth_login(tc)
    monkeypatch.setattr(server, "get_access_token", lambda: None if subject is None else AccessToken(token="x", client_id="c", scopes=[], subject=subject))
    res = tool(tc, "list_decks", token=t.json()["access_token"])
    assert res["isError"] and "Not authenticated" in json.dumps(res["content"])


# ---- helpers for the review tests ------------------------------------------------------------


def text_of(result):
    return " ".join(c.get("text", "") for c in result["content"])


def tools_by_name(tc, token):
    r = rpc(tc, "tools/list", token=token)
    assert r.status_code == 200, r.text
    return {t["name"]: t for t in r.json()["result"]["tools"]}


# ---- anonymous registration ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"client_name": "A" * 10_000},
        {"redirect_uris": [f"http://127.0.0.1:{p}/cb" for p in range(1000, 1100)]},
        {"contacts": ["someone@example.org"] * 1000},
    ],
    ids=["huge-name", "100-redirect-uris", "1000-contacts"],
)
def test_oversized_client_registration_is_refused(served, extra):
    tc, _, prov = served
    body = {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code"], "response_types": ["code"], **extra}
    r = tc.post("/register", json=body)
    assert r.status_code == 400
    assert prov._exec("SELECT COUNT(*) FROM clients")[0][0] == 0


def test_client_registration_is_rate_limited_per_ip(served):
    tc, _, _ = served
    body = {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code"], "response_types": ["code"]}
    statuses = [tc.post("/register", json=body).status_code for _ in range(100)]
    assert statuses.count(201) < 100
    assert set(statuses) <= {201, 400, 429}


# ---- what Claude sees ------------------------------------------------------------------------


def test_add_notes_schema_describes_each_note(served):
    """Today each note is {"type": "object", "additionalProperties": true}: Claude has to guess."""
    tc, cfg, _ = served
    t, _, _ = oauth_login(tc)
    schema = tools_by_name(tc, t.json()["access_token"])["add_notes"]["inputSchema"]
    item = schema["properties"]["notes"]["items"]
    if "$ref" in item:
        item = schema["$defs"][item["$ref"].rsplit("/", 1)[-1]]
    assert {"deck", "note_type", "fields", "tags"} <= set(item.get("properties", {}))
    assert {"deck", "note_type", "fields"} <= set(item.get("required", []))
    assert item.get("additionalProperties") is False


def test_read_tools_are_annotated_read_only(served):
    """Lets the Claude connector allow reads automatically and ask before writes."""
    tc, _, _ = served
    t, _, _ = oauth_login(tc)
    tools = tools_by_name(tc, t.json()["access_token"])
    for name in ("list_decks", "list_note_types", "find_notes"):
        assert (tools[name].get("annotations") or {}).get("readOnlyHint") is True, name
    for name in ("add_notes", "update_note", "sync"):
        assert (tools[name].get("annotations") or {}).get("readOnlyHint") is False, name


def test_invalid_search_reaches_the_model_readably(served):
    tc, _, _ = served
    t, _, _ = oauth_login(tc)
    res = tool(tc, "find_notes", {"query": 'deck:"unclosed'}, t.json()["access_token"])
    assert res["isError"] and "quote" in text_of(res)


def test_malformed_note_reaches_the_model_readably(served):
    tc, _, _ = served
    t, _, _ = oauth_login(tc)
    note = {"deck": "Physics", "note_type": "Basic", "fields": "Front: q, Back: a"}
    res = tool(tc, "add_notes", {"notes": [note], "approved_by_user": True}, t.json()["access_token"])
    assert res["isError"] and "fields" in text_of(res)


def test_find_notes_tells_claude_how_many_matched(served):
    """With limit=5 and 30 matches, Claude must be able to tell the duplicate check saw only part."""
    tc, cfg, _ = served
    seed_collection(cfg, notes=30)
    t, _, _ = oauth_login(tc)
    res = tool(tc, "find_notes", {"query": "deck:Physics", "limit": 5}, t.json()["access_token"])
    assert not res.get("isError"), res

    def ints(value):
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            yield value
        elif isinstance(value, dict):
            for v in value.values():
                yield from ints(v)
        elif isinstance(value, list):
            for v in value:
                yield from ints(v)

    payload = [json.loads(c["text"]) for c in res["content"]] + [res.get("structuredContent")]
    assert 30 in set(ints(payload))


# ---- concurrency ------------------------------------------------------------------------------------


def test_simultaneous_first_calls_share_one_bridge():
    """Tools run in worker threads; two Bridges on one collection fail with "Anki already open"."""
    import threading

    for _ in range(20):
        bridges = Bridges(Config(data_dir=tempfile.mkdtemp()))
        barrier = threading.Barrier(8)
        got = []

        def first_call():
            barrier.wait()
            got.append(bridges.for_subject(OWNER_SUBJECT))

        threads = [threading.Thread(target=first_call) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len({id(b) for b in got}) == 1


# ---- configuration ------------------------------------------------------------------------------------


def test_load_config_reads_everything_from_the_given_env(monkeypatch):
    for name in ("ANKIWEB_USER", "ANKIWEB_PASS", "ANKIWEB_ENDPOINT", "REDIRECT_ALLOWLIST"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config(env(ANKIWEB_USER="me", ANKIWEB_PASS="pw", REDIRECT_ALLOWLIST="https://a.example"))
    assert cfg.sync is not None and cfg.sync.username == "me"
    assert cfg.redirect_allowlist == ["https://a.example"]


def test_non_numeric_port_is_a_config_error():
    with pytest.raises(ConfigError):
        load_config(env(PORT="eighty"))
