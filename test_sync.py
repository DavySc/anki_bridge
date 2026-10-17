"""Sync checks against Anki's own sync server, offline. Run: python -m pytest -q test_sync.py

No test here touches real AnkiWeb. `python -m anki.syncserver` ships in the anki wheel and
speaks the same protocol, so the real client talks to a real server on localhost.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest
from anki.collection import Collection

from core import Bridge, BridgeError, SyncConfig, SyncRequired
from test_core import basic

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1)
    except urllib.error.HTTPError:
        return True  # any HTTP answer, even 404, means it is listening
    except OSError:
        return False
    return True


class Server:
    """Anki's sync server on localhost, one user. Started per test (about 0.7 s: it hashes the
    password at startup), so every test has an empty account and can stop the server."""

    USER, PASSWORD = "user", "pass"

    def __init__(self):
        self.base = tempfile.mkdtemp()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.log = open(os.path.join(self.base, "server.log"), "w")
        env = {
            **os.environ,
            "SYNC_BASE": self.base,
            "SYNC_HOST": "127.0.0.1",
            "SYNC_PORT": str(self.port),
            "RUST_LOG": "anki=warn",
            "SYNC_USER1": f"{self.USER}:{self.PASSWORD}",
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "anki.syncserver"], env=env, stdout=self.log, stderr=self.log
        )
        deadline = time.time() + 15
        while time.time() < deadline and self.proc.poll() is None and not _up(self.url):
            time.sleep(0.05)
        if self.proc.poll() is not None or not _up(self.url):
            self.stop()
            pytest.skip(f"local sync server did not start, see {self.log.name}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


@pytest.fixture()
def server():
    s = Server()
    try:
        yield s
    finally:
        s.stop()


class Account:
    def __init__(self, server: Server):
        self.server = server
        self.url, self.user, self.password = server.url, server.USER, server.PASSWORD
        self.dir = tempfile.mkdtemp()

    def config(self, **override) -> SyncConfig:
        cfg = SyncConfig(self.user, self.password, self.url)
        for k, v in override.items():
            setattr(cfg, k, v)
        return cfg

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name + ".anki2")

    def seed(self) -> str:
        """Make the 'server side' collection with a Physics deck and upload it as the account's data."""
        path = self._path("bridge")
        col = Collection(path)
        col.decks.add_normal_deck_with_name("Physics")
        self._full(col, upload=True)
        col.close()
        return path

    def phone(self, name: str = "phone") -> Collection:
        """A second device: a fresh collection that downloaded the account."""
        col = Collection(self._path(name))
        self._full(col, upload=False)
        return col

    def _full(self, col: Collection, upload: bool) -> None:
        auth = col.sync_login(self.user, self.password, self.url)
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=None, upload=upload)
        col.reopen(after_full_sync=True)

    def sync(self, col: Collection):
        auth = col.sync_login(self.user, self.password, self.url)
        return col.sync_collection(auth, sync_media=False)


@pytest.fixture()
def account(server):
    return Account(server)


@pytest.fixture()
def bridge(account):
    return Bridge(account.seed(), account.config())


def note_texts(col: Collection) -> dict[str, list[str]]:
    return {col.get_note(i)["Front"]: list(col.get_note(i).tags) for i in col.find_notes("")}


# ---- baseline round trip (required behaviour 5) -----------------------------------------


def test_add_and_update_reach_a_second_device(account, bridge):
    phone = account.phone()
    out = bridge.add_notes([basic(front="Why is the sky blue?", tags=["claude", "physics"])])
    # `required` only says whether a FULL sync is needed; after any completed normal sync it is
    # NO_CHANGES even when notes were pushed, so the label must not claim "no changes".
    assert out["sync_before"] == "synced"
    assert out["sync_after"] == "synced"

    account.sync(phone)
    got = note_texts(phone)
    assert got == {"Why is the sky blue?": ["claude", "physics"]}
    nid = out["added"][0]
    assert phone.get_note(nid)["Back"] == "Rayleigh scattering"

    upd = bridge.update_note(nid, fields={"Back": "Rayleigh scattering of sunlight"}, add_tags=["checked"])
    assert upd["sync_after"] == "synced"
    account.sync(phone)
    assert phone.get_note(nid)["Back"] == "Rayleigh scattering of sunlight"
    assert "checked" in phone.get_note(nid).tags
    phone.close()


# ---- readable errors, and a failing pre-sync writes nothing (G1, rule 2) ------------------

from anki.errors import NetworkError, SyncError, SyncErrorKind

from core import Bridge as _Bridge


def snapshot(path: str) -> tuple[int, int]:
    """(note count, collection mod time) read without syncing."""
    col = Collection(path)
    try:
        return len(col.find_notes("")), col.mod
    finally:
        col.close()


def test_wrong_password_is_a_readable_error_without_the_password(account):
    path = account.seed()
    b = Bridge(path, account.config(password="hunter2-wrong"))
    with pytest.raises(BridgeError) as e:
        b.sync()
    msg = str(e.value)
    assert "login failed" in msg.lower() and "ANKIWEB_USER" in msg and "ANKIWEB_PASS" in msg
    assert "hunter2-wrong" not in msg
    assert not isinstance(e.value, SyncRequired)


def test_unreachable_server_is_a_readable_error(account):
    path = account.seed()
    account.server.stop()
    started = time.time()
    with pytest.raises(BridgeError) as e:
        Bridge(path, account.config()).sync()
    assert "could not reach ankiweb" in str(e.value).lower()
    print(f"refused connection took {time.time() - started:.2f}s")  # informs the timeout question
    assert time.time() - started < 10


def test_unparseable_endpoint_is_a_readable_error(account):
    path = account.seed()
    with pytest.raises(BridgeError) as e:
        Bridge(path, account.config(endpoint="http://127.0.0.1:9/")).sync()
    assert "could not reach ankiweb" in str(e.value).lower()


@pytest.mark.parametrize("which", ["add", "update"])
@pytest.mark.parametrize("failure", ["auth", "network"])
def test_failing_pre_sync_writes_nothing(account, which, failure):
    path = account.seed()
    good = Bridge(path, account.config())
    nid = good.add_notes([basic(front="existing")])["added"][0]
    before = snapshot(path)

    if failure == "auth":
        bad = Bridge(path, account.config(password="wrong"))
    else:
        account.server.stop()
        bad = Bridge(path, account.config())
    with pytest.raises(BridgeError):
        if which == "add":
            bad.add_notes([basic(front="new one")])
        else:
            bad.update_note(nid, fields={"Back": "changed"})
    assert snapshot(path) == before


def test_other_sync_errors_pass_their_message_on_and_scrub_the_password():
    b = Bridge("unused", SyncConfig("me", "s3cret-pw", None))
    err = SyncError("server said no: s3cret-pw", None, None, None, SyncErrorKind.OTHER)
    out = b._explain(err)
    assert isinstance(out, BridgeError) and "server said no" in str(out)
    assert "s3cret-pw" not in str(out)
    net = b._explain(NetworkError("boom s3cret-pw", None, None, None))
    assert "could not reach ankiweb" in str(net).lower() and "s3cret-pw" not in str(net)


def test_sync_errors_reach_the_model_through_the_server(account):
    """mcp 2.x shows only ToolError text; the readable sync error must survive the wrapper."""
    import json

    from starlette.testclient import TestClient

    import test_server_auth as tsa
    from server import Config

    path = account.seed()
    cfg = Config(data_dir=tempfile.mkdtemp(), collection=path, sync=account.config(password="pw-wrong-1234"))
    app, _ = tsa.make_app(cfg)
    with TestClient(app, base_url="http://127.0.0.1:8000") as tc:
        res = tsa.tool(tc, "sync")
    assert res["isError"]
    text = json.dumps(res["content"])
    assert "login failed" in text.lower() and "pw-wrong-1234" not in text


# ---- a failing post-write sync keeps the result (G2, rule 3) -------------------------------


def fail_second_sync(bridge: Bridge, exc: Exception) -> None:
    """The pre-write sync runs for real; the sync after the write raises `exc`. Killing the server
    between the two is not controllable, so this is the one place a test injects a failure."""
    real, calls = bridge._sync, []

    def wrapper(col):
        calls.append(1)
        if len(calls) == 2:
            raise exc
        return real(col)

    bridge._sync = wrapper
    bridge._restore = lambda: setattr(bridge, "_sync", real)


def test_add_notes_returns_the_ids_when_the_sync_after_it_fails(account, bridge):
    phone = account.phone()
    fail_second_sync(bridge, BridgeError("Could not reach AnkiWeb (network error)."))
    out = bridge.add_notes([basic(front="kept")])  # does not raise
    assert len(out["added"]) == 1 and out["sync_before"] == "synced"
    assert out["sync_after"].startswith("failed:") and "Run sync" in out["sync_after"]
    account.sync(phone)
    assert note_texts(phone) == {}  # not on the other device yet

    bridge._restore()
    assert bridge.sync() == {"result": "synced"}  # the sync tool pushes the pending write
    account.sync(phone)
    assert list(note_texts(phone)) == ["kept"]
    phone.close()


def test_update_note_returns_the_note_when_the_sync_after_it_fails(account, bridge):
    nid = bridge.add_notes([basic(front="orig")])["added"][0]
    fail_second_sync(bridge, BridgeError("Could not reach AnkiWeb (network error)."))
    out = bridge.update_note(nid, fields={"Back": "edited"})
    assert out["note"]["fields"]["Back"] == "edited" and out["sync_after"].startswith("failed:")
    bridge._restore()
    bridge.sync()
    phone = account.phone()
    assert phone.get_note(nid)["Back"] == "edited"
    phone.close()


def test_retrying_after_a_failed_post_sync_is_rejected_as_a_duplicate(account, bridge):
    fail_second_sync(bridge, BridgeError("Could not reach AnkiWeb (network error)."))
    bridge.add_notes([basic(front="once")])
    bridge._restore()
    with pytest.raises(BridgeError) as e:
        bridge.add_notes([basic(front="once")])
    assert "Nothing was added" in str(e.value) and "duplicate" in str(e.value)
    assert snapshot(bridge.path)[0] == 1


def test_full_sync_required_after_the_write_is_reported_not_raised(account, bridge):
    fail_second_sync(bridge, SyncRequired("AnkiWeb requires a full sync. It was not done."))
    out = bridge.add_notes([basic(front="kept")])
    assert len(out["added"]) == 1
    assert out["sync_after"].startswith("failed:") and "full sync" in out["sync_after"]
    assert "Run sync to retry" not in out["sync_after"]  # retrying would not help
