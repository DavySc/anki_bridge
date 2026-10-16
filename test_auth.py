"""Checks auth.py: the single-user OAuth provider. Run: python -m pytest -q"""

import asyncio
import os
import tempfile

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from auth import (
    DEFAULT_REDIRECT_ALLOWLIST,
    OWNER_SUBJECT,
    OwnerAuthenticator,
    OwnerProvider,
    hash_secret,
    verify_secret,
)


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
    return OwnerProvider(path)


# ---- clients ---------------------------------------------------------------


def test_registered_client_round_trips(provider):
    run(provider.register_client(client()))
    got = run(provider.get_client("c1"))
    assert got is not None
    assert got.client_id == "c1"
    assert got.client_secret == "s3cret"
    assert [str(u) for u in got.redirect_uris] == ["https://claude.ai/api/mcp/auth_callback"]


def test_unknown_client_is_none(provider):
    assert run(provider.get_client("nope")) is None


def test_client_survives_reopening_the_database(provider):
    run(provider.register_client(client()))
    again = OwnerProvider(provider.path)
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
    p = OwnerProvider(path, redirect_allowlist=["https://example.org"])
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
