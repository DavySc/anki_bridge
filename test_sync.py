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
