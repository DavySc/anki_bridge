"""Checks auth.py: the single-user OAuth provider. Run: python -m pytest -q"""

import asyncio
import os
import tempfile

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from auth import (
    derive_server_key,
    forwarded_client_ip,
    DEFAULT_REDIRECT_ALLOWLIST,
    OWNER_SUBJECT,
    OwnerAuthenticator,
    OwnerProvider,
    hash_secret,
    verify_secret,
)


KEY = b"k" * 32


def run(coro):
    return asyncio.run(coro)


def client(client_id="c1", redirects=("https://claude.ai/api/mcp/auth_callback",), **kw):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=kw.pop("client_secret", "s3cret"),
        redirect_uris=list(redirects) if redirects is not None else None,
        token_endpoint_auth_method="client_secret_post",
        **kw,
    )


@pytest.fixture()
def provider():
    path = os.path.join(tempfile.mkdtemp(), "auth.sqlite3")
    return OwnerProvider(path, server_key=KEY)


# ---- clients ---------------------------------------------------------------


def test_registered_client_round_trips(provider):
    info = client()
    run(provider.register_client(info))
    got = run(provider.get_client("c1"))
    assert got is not None
    assert got.client_id == "c1"
    assert got.client_secret == info.client_secret  # the derived one the client was handed
    assert [str(u) for u in got.redirect_uris] == ["https://claude.ai/api/mcp/auth_callback"]


def test_unknown_client_is_none(provider):
    assert run(provider.get_client("nope")) is None


def test_client_survives_reopening_the_database(provider):
    run(provider.register_client(client()))
    again = OwnerProvider(provider.path, server_key=KEY)
    assert run(again.get_client("c1")) is not None


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/cb",
        "http://claude.ai/cb",  # wrong scheme
        "https://claude.ai.evil.example/cb",  # suffix trick
        "https://evil.example/claude.ai",  # host only in path
        "https://user@claude.ai/cb",  # userinfo
        "https://claude.ai/cb#frag",  # fragment
        "http://example.com:8000/cb",  # not loopback
        "custom-scheme://claude.ai/cb",
    ],
)
def test_disallowed_redirect_uri_rejected(provider, uri):
    with pytest.raises(RegistrationError) as e:
        run(provider.register_client(client(redirects=[uri])))
    assert e.value.error == "invalid_redirect_uri"
    assert run(provider.get_client("c1")) is None


def test_one_bad_uri_rejects_the_whole_registration(provider):
    with pytest.raises(RegistrationError):
        run(provider.register_client(client(redirects=["https://claude.ai/cb", "https://evil.example/cb"])))


@pytest.mark.parametrize("redirects", [None, []])
def test_registration_without_redirect_uris_rejected(provider, redirects):
    with pytest.raises(RegistrationError):
        run(provider.register_client(client(redirects=redirects)))


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://127.0.0.1:6274/oauth/callback",
        "http://localhost:3000/cb",
        "http://[::1]:5000/cb",
    ],
)
def test_default_allowlist_accepts(provider, uri):
    run(provider.register_client(client(redirects=[uri])))
    assert run(provider.get_client("c1")) is not None


def test_allowlist_is_configurable():
    path = os.path.join(tempfile.mkdtemp(), "a.sqlite3")
    p = OwnerProvider(path, server_key=KEY, redirect_allowlist=["https://example.org"])
    run(p.register_client(client(redirects=["https://example.org/cb"])))
    with pytest.raises(RegistrationError):
        run(p.register_client(client(client_id="c2", redirects=["https://claude.ai/cb"])))


def test_allowlist_from_env(monkeypatch):
    from auth import redirect_allowlist_from_env

    monkeypatch.delenv("REDIRECT_ALLOWLIST", raising=False)
    assert redirect_allowlist_from_env() == DEFAULT_REDIRECT_ALLOWLIST
    monkeypatch.setenv("REDIRECT_ALLOWLIST", "https://a.example, https://b.example")
    assert redirect_allowlist_from_env() == ["https://a.example", "https://b.example"]


# ---- owner authenticator ----------------------------------------------------


def test_hash_verifies_only_the_right_secret():
    h = hash_secret("correct horse")
    assert "correct horse" not in h
    assert verify_secret("correct horse", h)
    assert not verify_secret("wrong", h)
    assert not verify_secret("correct horse", "garbage")


def test_hash_is_salted():
    assert hash_secret("x") != hash_secret("x")


def test_owner_authenticator_returns_owner_subject():
    auth = OwnerAuthenticator(hash_secret("pw"))
    assert auth.authenticate({"secret": "pw"}) == OWNER_SUBJECT
    assert auth.authenticate({"secret": "nope"}) is None
    assert auth.authenticate({}) is None
    # username is accepted and ignored today (multi-user seam)
    assert auth.authenticate({"username": "anyone", "secret": "pw"}) == OWNER_SUBJECT


# ---- authorize -> login -> code ----------------------------------------------

import time
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from auth import GLOBAL_FAILURE_LIMIT, LoginLimiter, make_login_handler

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
SECRET = "correct horse battery"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def env():
    clock = Clock()
    path = os.path.join(tempfile.mkdtemp(), "auth.sqlite3")
    prov = OwnerProvider(path, server_key=KEY, public_url="https://anki.example", now=clock)
    run(prov.register_client(client()))
    limiter = LoginLimiter(now=clock)
    app = Starlette(
        routes=[
            Route(
                "/login",
                make_login_handler(prov, OwnerAuthenticator(hash_secret(SECRET)), limiter),
                methods=["GET", "POST"],
            )
        ]
    )
    tc = TestClient(app, follow_redirects=False, base_url="https://anki.example")
    return prov, tc, clock, limiter


def login_code(prov, tc, **kw):
    _, req = start(prov, **kw)
    r = tc.post("/login", data={"req": req, "secret": SECRET})
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def start(prov, state="xyz", challenge="chal", resource="https://anki.example/mcp"):
    c = run(prov.get_client("c1"))
    params = AuthorizationParams(
        state=state,
        scopes=[],
        code_challenge=challenge,
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )
    url = run(prov.authorize(c, params))
    return url, parse_qs(urlparse(url).query)["req"][0]


def test_authorize_points_at_our_login_page(env):
    prov, _, _, _ = env
    url, req = start(prov)
    assert url.startswith("https://anki.example/login?req=")
    assert len(req) >= 40


def test_login_form_renders_and_sets_security_headers(env):
    prov, tc, _, _ = env
    _, req = start(prov)
    r = tc.get("/login", params={"req": req})
    assert r.status_code == 200 and 'name="secret"' in r.text and req in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


def test_right_secret_redirects_to_registered_uri_with_code_and_state(env):
    prov, tc, _, _ = env
    _, req = start(prov, state="abc")
    r = tc.post("/login", data={"req": req, "secret": SECRET})
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    q = parse_qs(loc.query)
    assert q["state"] == ["abc"] and len(q["code"][0]) >= 40


def test_code_loads_with_subject_challenge_and_resource(env):
    prov, tc, _, _ = env
    _, req = start(prov, challenge="CH", resource="https://anki.example/mcp")
    code = parse_qs(urlparse(tc.post("/login", data={"req": req, "secret": SECRET}).headers["location"]).query)["code"][0]
    got = run(prov.load_authorization_code(run(prov.get_client("c1")), code))
    assert got.subject == OWNER_SUBJECT
    assert got.code_challenge == "CH"
    assert got.resource == "https://anki.example/mcp"
    assert str(got.redirect_uri) == REDIRECT and got.redirect_uri_provided_explicitly


def test_code_is_bound_to_its_client(env):
    prov, tc, _, _ = env
    run(prov.register_client(client(client_id="c2")))
    _, req = start(prov)
    code = parse_qs(urlparse(tc.post("/login", data={"req": req, "secret": SECRET}).headers["location"]).query)["code"][0]
    assert run(prov.load_authorization_code(run(prov.get_client("c2")), code)) is None


def test_wrong_secret_never_yields_a_code(env):
    prov, tc, _, _ = env
    _, req = start(prov)
    r = tc.post("/login", data={"req": req, "secret": "nope"})
    assert r.status_code == 401 and "location" not in r.headers
    assert prov._exec("SELECT COUNT(*) FROM codes")[0][0] == 0
    # the pending request survives a typo so the owner can retry
    assert tc.post("/login", data={"req": req, "secret": SECRET}).status_code == 302


def test_sixth_failure_is_rate_limited_even_with_the_right_secret(env):
    prov, tc, clock, _ = env
    _, req = start(prov)
    for _ in range(5):
        assert tc.post("/login", data={"req": req, "secret": "nope"}).status_code == 401
    assert tc.post("/login", data={"req": req, "secret": "nope"}).status_code == 429
    assert tc.post("/login", data={"req": req, "secret": SECRET}).status_code == 429
    clock.t += 901  # window over (the old login link has expired too, so start again)
    _, req = start(prov)
    assert tc.post("/login", data={"req": req, "secret": SECRET}).status_code == 302


def test_changing_the_username_does_not_reset_the_limit(env):
    prov, tc, _, _ = env
    _, req = start(prov)
    for i in range(5):
        tc.post("/login", data={"req": req, "secret": "nope", "username": f"u{i}"})
    assert tc.post("/login", data={"req": req, "secret": SECRET, "username": "fresh"}).status_code == 429


def test_global_limit_covers_rotating_ips():
    clock = Clock()
    lim = LoginLimiter(now=clock)
    for i in range(GLOBAL_FAILURE_LIMIT):
        lim.fail("global")
        lim.fail(f"ip:{i}")
    assert not lim.blocked("ip:1") and lim.blocked("global", GLOBAL_FAILURE_LIMIT)


def test_pending_request_is_single_use(env):
    prov, tc, _, _ = env
    _, req = start(prov)
    assert tc.post("/login", data={"req": req, "secret": SECRET}).status_code == 302
    again = tc.post("/login", data={"req": req, "secret": SECRET})
    assert again.status_code == 400 and "location" not in again.headers


def test_pending_request_expires(env):
    prov, tc, clock, _ = env
    _, req = start(prov)
    clock.t += 601
    assert tc.get("/login", params={"req": req}).status_code == 400
    assert tc.post("/login", data={"req": req, "secret": SECRET}).status_code == 400


@pytest.mark.parametrize("req", ["", "garbage", "../../etc"])
def test_unknown_request_id_never_redirects(env, req):
    _, tc, _, _ = env
    for r in (tc.get("/login", params={"req": req}), tc.post("/login", data={"req": req, "secret": SECRET})):
        assert r.status_code == 400 and "location" not in r.headers


def test_code_expires(env):
    prov, tc, clock, _ = env
    c = run(prov.get_client("c1"))
    code = login_code(prov, tc)
    assert run(prov.load_authorization_code(c, code)) is not None
    clock.t += 301
    assert run(prov.load_authorization_code(c, code)) is None


def test_db_holds_no_plaintext_request_id_or_code(env):
    prov, tc, _, _ = env
    _, req = start(prov)
    dump_pending = str(prov._exec("SELECT * FROM pending"))
    assert req not in dump_pending
    code = parse_qs(urlparse(tc.post("/login", data={"req": req, "secret": SECRET}).headers["location"]).query)["code"][0]
    assert code not in str(prov._exec("SELECT * FROM codes"))
    assert SECRET not in open(prov.path, "rb").read().decode("latin-1")


def test_form_escapes_request_id(env):
    _, tc, _, _ = env
    r = tc.get("/login", params={"req": '"><script>x</script>'})
    assert "<script>" not in r.text  # invalid ids get the generic page, never echoed


def test_hash_cli_prints_a_verifiable_hash():
    from auth import main

    line = main(["hash"], prompt=lambda _: "a very long secret")
    assert line.startswith("OWNER_SECRET_HASH='scrypt$")
    assert verify_secret("a very long secret", line.split("'")[1])


def test_hash_cli_rejects_short_or_mismatched_secrets():
    from auth import main

    with pytest.raises(SystemExit):
        main(["hash"], prompt=lambda _: "short")
    answers = iter(["a very long secret", "different long secret"])
    with pytest.raises(SystemExit):
        main(["hash"], prompt=lambda _: next(answers))
    with pytest.raises(SystemExit):
        main([])


# ---- tokens ----------------------------------------------------------------------

from mcp.server.auth.provider import AuthorizeError, TokenError

RESOURCE = "https://anki.example/mcp"


def tokens_for(prov, tc, client_id="c1"):
    c = run(prov.get_client(client_id))
    code = run(prov.load_authorization_code(c, login_code(prov, tc, resource=RESOURCE)))
    return c, code, run(prov.exchange_authorization_code(c, code))


def test_code_exchange_issues_working_tokens(env):
    prov, tc, _, _ = env
    c, code, tok = tokens_for(prov, tc)
    assert tok.token_type == "Bearer" and tok.expires_in == 3600 and tok.refresh_token
    at = run(prov.load_access_token(tok.access_token))
    assert at.subject == OWNER_SUBJECT and at.client_id == "c1" and at.resource == RESOURCE


def test_code_cannot_be_exchanged_twice_and_second_try_revokes_the_grant(env):
    prov, tc, _, _ = env
    c, code, tok = tokens_for(prov, tc)
    with pytest.raises(TokenError):
        run(prov.exchange_authorization_code(c, code))
    assert run(prov.load_access_token(tok.access_token)) is None
    assert run(prov.load_refresh_token(c, tok.refresh_token)) is None


def test_access_token_expires(env):
    prov, tc, clock, _ = env
    _, _, tok = tokens_for(prov, tc)
    clock.t += 3599
    assert run(prov.load_access_token(tok.access_token)) is not None
    clock.t += 2
    assert run(prov.load_access_token(tok.access_token)) is None


def test_unknown_or_garbage_access_token_is_none(env):
    prov, _, _, _ = env
    assert run(prov.load_access_token("nope")) is None
    assert run(prov.load_access_token("")) is None


def test_refresh_rotates_both_tokens(env):
    prov, tc, _, _ = env
    c, _, tok = tokens_for(prov, tc)
    rt = run(prov.load_refresh_token(c, tok.refresh_token))
    new = run(prov.exchange_refresh_token(c, rt, rt.scopes))
    assert new.access_token != tok.access_token and new.refresh_token != tok.refresh_token
    assert run(prov.load_access_token(tok.access_token)) is None
    at = run(prov.load_access_token(new.access_token))
    assert at.subject == OWNER_SUBJECT and at.resource == RESOURCE


def test_reusing_a_rotated_refresh_token_revokes_the_grant(env):
    prov, tc, _, _ = env
    c, _, tok = tokens_for(prov, tc)
    old = run(prov.load_refresh_token(c, tok.refresh_token))
    new = run(prov.exchange_refresh_token(c, old, old.scopes))
    stale = run(prov.load_refresh_token(c, tok.refresh_token))
    assert stale is not None and stale.rotated  # must reach exchange so reuse is detected
    with pytest.raises(TokenError):
        run(prov.exchange_refresh_token(c, stale, stale.scopes))
    assert run(prov.load_access_token(new.access_token)) is None
    assert run(prov.load_refresh_token(c, new.refresh_token)) is None


def test_refresh_token_expires_after_30_days(env):
    prov, tc, clock, _ = env
    c, _, tok = tokens_for(prov, tc)
    clock.t += 30 * 24 * 3600 - 1
    assert run(prov.load_refresh_token(c, tok.refresh_token)) is not None
    clock.t += 2
    assert run(prov.load_refresh_token(c, tok.refresh_token)) is None


def test_revoking_either_token_ends_the_whole_grant(env):
    prov, tc, _, _ = env
    for which in ("access", "refresh"):
        c, _, tok = tokens_for(prov, tc)
        t = run(prov.load_access_token(tok.access_token)) if which == "access" else run(prov.load_refresh_token(c, tok.refresh_token))
        run(prov.revoke_token(t))
        assert run(prov.load_access_token(tok.access_token)) is None
        assert run(prov.load_refresh_token(c, tok.refresh_token)) is None


def test_revoking_a_made_up_token_does_nothing(env):
    prov, tc, _, _ = env
    _, _, tok = tokens_for(prov, tc)
    from mcp.server.auth.provider import AccessToken

    run(prov.revoke_token(AccessToken(token="x", client_id="c1", scopes=[])))
    assert run(prov.load_access_token(tok.access_token)) is not None


def test_refresh_token_belongs_to_its_client(env):
    prov, tc, _, _ = env
    run(prov.register_client(client(client_id="c2")))
    _, _, tok = tokens_for(prov, tc)
    assert run(prov.load_refresh_token(run(prov.get_client("c2")), tok.refresh_token)) is None


class OtherAuthenticator:
    """Test-only second user: proves the subject seam, not a feature."""

    def authenticate(self, credentials):
        return "other" if credentials.get("secret") == "other-pw" else None


def test_second_subject_gets_its_own_isolated_grant(env):
    prov, tc, clock, limiter = env
    c, _, owner_tok = tokens_for(prov, tc)
    app = Starlette(routes=[Route("/login", make_login_handler(prov, OtherAuthenticator(), limiter), methods=["POST", "GET"])])
    tc2 = TestClient(app, follow_redirects=False, base_url="https://anki.example")
    _, req = start(prov, resource=RESOURCE)
    r = tc2.post("/login", data={"req": req, "secret": "other-pw"})
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    oc = run(prov.load_authorization_code(c, code))
    other_tok = run(prov.exchange_authorization_code(c, oc))

    assert run(prov.load_access_token(other_tok.access_token)).subject == "other"
    assert run(prov.load_access_token(owner_tok.access_token)).subject == OWNER_SUBJECT
    # revoking or rotating the other user's grant leaves the owner's alone
    run(prov.revoke_token(run(prov.load_access_token(other_tok.access_token))))
    assert run(prov.load_access_token(other_tok.access_token)) is None
    assert run(prov.load_access_token(owner_tok.access_token)) is not None
    assert run(prov.load_refresh_token(c, owner_tok.refresh_token)) is not None


def test_authorize_binds_to_the_resource_when_configured():
    path = os.path.join(tempfile.mkdtemp(), "r.sqlite3")
    prov = OwnerProvider(path, server_key=KEY, public_url="https://anki.example", resource_url=RESOURCE)
    run(prov.register_client(client()))
    c = run(prov.get_client("c1"))

    def params(resource):
        return AuthorizationParams(state=None, scopes=[], code_challenge="x", redirect_uri=REDIRECT,
                                   redirect_uri_provided_explicitly=True, resource=resource)

    with pytest.raises(AuthorizeError) as e:
        run(prov.authorize(c, params("https://evil.example/mcp")))
    assert e.value.error == "invalid_target"
    run(prov.authorize(c, params(RESOURCE + "/")))  # trailing slash tolerated
    run(prov.authorize(c, params(None)))  # omitted resource defaults to ours


def test_missing_resource_defaults_to_ours_on_the_token():
    clock = Clock()
    path = os.path.join(tempfile.mkdtemp(), "r.sqlite3")
    prov = OwnerProvider(path, server_key=KEY, public_url="https://anki.example", resource_url=RESOURCE, now=clock)
    run(prov.register_client(client()))
    app = Starlette(routes=[Route("/login", make_login_handler(prov, OwnerAuthenticator(hash_secret(SECRET)), LoginLimiter(now=clock)), methods=["GET", "POST"])])
    tc = TestClient(app, follow_redirects=False, base_url="https://anki.example")
    c = run(prov.get_client("c1"))
    code = run(prov.load_authorization_code(c, login_code(prov, tc, resource=None)))
    tok = run(prov.exchange_authorization_code(c, code))
    assert run(prov.load_access_token(tok.access_token)).resource == RESOURCE


def test_db_holds_no_plaintext_tokens(env):
    prov, tc, _, _ = env
    c, _, tok = tokens_for(prov, tc)
    raw = open(prov.path, "rb").read().decode("latin-1")
    dump = raw + str(prov._exec("SELECT * FROM access_tokens")) + str(prov._exec("SELECT * FROM refresh_tokens"))
    assert tok.access_token not in dump and tok.refresh_token not in dump and SECRET not in dump


# ---- derived client secret ---------------------------------------------------------


def test_client_secret_is_derived_and_never_stored(provider):
    info = client(client_secret="minted-by-sdk")
    run(provider.register_client(info))
    assert info.client_secret != "minted-by-sdk"  # the SDK echoes this object to the client
    assert run(provider.get_client("c1")).client_secret == info.client_secret
    assert info.client_secret not in open(provider.path, "rb").read().decode("latin-1")
    assert "minted-by-sdk" not in open(provider.path, "rb").read().decode("latin-1")


def test_client_secret_depends_on_server_key_and_client_id(provider):
    run(provider.register_client(client()))
    run(provider.register_client(client(client_id="c2")))
    other = OwnerProvider(provider.path, server_key=b"z" * 32)
    a, b = run(provider.get_client("c1")), run(provider.get_client("c2"))
    assert a.client_secret != b.client_secret
    assert run(other.get_client("c1")).client_secret != a.client_secret


def test_public_clients_keep_no_secret(provider):
    info = OAuthClientInformationFull(
        client_id="pub", redirect_uris=["https://claude.ai/cb"], token_endpoint_auth_method="none"
    )
    run(provider.register_client(info))
    assert run(provider.get_client("pub")).client_secret is None


def test_derive_server_key_is_stable_and_secret_dependent():
    assert derive_server_key("h1") == derive_server_key("h1") != derive_server_key("h2")


def test_forwarded_ip_uses_the_proxy_appended_entry():
    from starlette.requests import Request as R

    def req(h):
        return R({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in h.items()], "client": ("10.0.0.1", 1)})

    assert forwarded_client_ip(req({"x-forwarded-for": "6.6.6.6, 1.2.3.4"})) == "1.2.3.4"  # spoofed prefix ignored
    assert forwarded_client_ip(req({})) == "10.0.0.1"
