"""Sync checks against Anki's own sync server, offline. Run: python -m pytest -q test_sync.py

No test here touches real AnkiWeb. `python -m anki.syncserver` ships in the anki wheel and
speaks the same protocol, so the real client talks to a real server on localhost.
"""

import os
import socket
import subprocess
import sys
import tempfile
import threading
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


DEAD = "http://127.0.0.1:9/"  # nothing listens here: stands in for a server that went away


class Server:
    """Anki's sync server on localhost, one user, shared by the whole session (it takes about
    0.7 s to start: it hashes the password). Tests share one account, so each begins with
    Account.seed(), which replaces what the server holds, or does not depend on its contents."""

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


@pytest.fixture(scope="session")
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


def test_a_write_in_the_same_millisecond_as_the_last_sync_is_still_sent(account, bridge):
    """Anki compares the collection's mod time with the last sync's and skips the sync when they
    are equal. Writing right after the pre-sync can land in that millisecond (seen about 1 in 6
    runs of the round trip above once logins stopped taking 300 ms)."""
    nid = bridge.add_notes([basic(front="q")])["added"][0]
    col = Collection(bridge.path)
    note = col.get_note(nid)
    note["Back"] = "edited"
    col.update_note(note)
    col.db.execute("UPDATE col SET mod = ls")  # as if the edit happened in the sync's millisecond
    col.close()

    assert bridge.sync() == {"result": "synced"}
    phone = account.phone()
    assert phone.get_note(nid)["Back"] == "edited"
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
    started = time.time()
    with pytest.raises(BridgeError) as e:
        Bridge(path, account.config(endpoint=DEAD)).sync()
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
        bad = Bridge(path, account.config(endpoint=DEAD))
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


# ---- spend no AnkiWeb round trips on requests that cannot succeed ------------------------------


def test_malformed_write_fails_before_contacting_ankiweb(account, bridge):
    """With AnkiWeb down, a malformed request must report the malformation, not the network."""
    bridge.sync_cfg.endpoint = DEAD
    with pytest.raises(BridgeError) as e:
        bridge.add_notes([basic(tags="claude")])  # tags must be a list
    assert "AnkiWeb" not in str(e.value)


def test_writes_reuse_one_ankiweb_login(account, bridge, monkeypatch):
    """Every write logged in twice, every sync once; AnkiWeb may rate-limit logins (H5)."""
    logins = []
    real = Collection.sync_login
    monkeypatch.setattr(Collection, "sync_login", lambda self, *a, **kw: logins.append(1) or real(self, *a, **kw))

    nid = bridge.add_notes([basic(front="one")])["added"][0]
    bridge.update_note(nid, fields={"Back": "edited"})
    bridge.sync()
    assert len(logins) == 1


def test_a_rejected_login_key_is_renewed_once(account, bridge, monkeypatch):
    """The cached key can expire or be revoked; AnkiWeb then answers as for a wrong password."""
    from anki.sync_pb2 import SyncAuth

    bridge.sync()
    bridge._auth = SyncAuth(hkey="revoked", endpoint=account.url)
    logins = []
    real = Collection.sync_login
    monkeypatch.setattr(Collection, "sync_login", lambda self, *a, **kw: logins.append(1) or real(self, *a, **kw))
    assert bridge.sync() == {"result": "synced"}
    assert len(logins) == 1


# ---- a stalled AnkiWeb must not stall every tool --------------------------------------------------

class Blackhole:
    """Accepts connections and never answers: a server that silently stalls. Anki waits ~31 s."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/"
        self.held: list[socket.socket] = []
        self.connected = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)
            self.connected.set()

    def close(self):
        """Ends the stall: the waiting client sees the connection close and fails at once."""
        self.sock.close()
        for conn in self.held:
            conn.close()


def run_into(out: list, fn) -> None:
    try:
        out.append(fn())
    except Exception as e:
        out.append(e)


@pytest.mark.parametrize("other", ["list_decks", "add_notes"])
def test_a_stalled_sync_does_not_block_other_tools(other, monkeypatch):
    monkeypatch.setattr("core.LOCK_WAIT", 0.3)  # the real 3 s only slows the test
    hole = Blackhole()
    path = os.path.join(tempfile.mkdtemp(), "c.anki2")
    col = Collection(path)
    col.decks.add_normal_deck_with_name("Physics")
    col.close()
    b = Bridge(path, SyncConfig("user", "pass", hole.url))

    first, second, waiting = [], [], None
    stalled = threading.Thread(target=run_into, args=(first, b.sync))
    stalled.start()
    try:
        assert hole.connected.wait(5), "the first sync never reached the server"
        call = b.list_decks if other == "list_decks" else (lambda: b.add_notes([basic(front="second")]))
        waiting = threading.Thread(target=run_into, args=(second, call))
        waiting.start()
        waiting.join(5)
        assert not waiting.is_alive(), f"{other} waited behind a stalled sync"
        # It may answer, or refuse with a readable "busy" error; anything else is a crash.
        assert not isinstance(second[0], Exception) or isinstance(second[0], BridgeError)
    finally:
        hole.close()
        stalled.join(10)
        if waiting is not None:
            waiting.join(10)


# ---- a full sync demanded after a write ---------------------------------------------------------------


def test_full_sync_after_a_write_warns_that_a_download_would_discard_it(account, bridge):
    """bootstrap.py says to "normally" download; after a write that would lose the new notes."""
    import re

    fail_second_sync(bridge, SyncRequired("AnkiWeb requires a full sync (upload or download). It was not done."))
    out = bridge.add_notes([basic(front="only here")])
    assert re.search(r"discard|lose|lost", out["sync_after"], re.I), out["sync_after"]


# ---- full sync is refused, names the direction, and is never done by the server ---------------------------


def _change_schema(col: Collection) -> None:
    nt = col.models.by_name("Basic")
    col.models.add_field(nt, col.models.new_field("Extra"))
    col.models.save(nt)


def server_notes(account) -> int:
    return len(account.phone("check").find_notes(""))


def test_schema_change_elsewhere_refuses_a_write_and_changes_nothing(account, bridge):
    """H2. Against the bundled server every full-sync demand arrives as FULL_SYNC (no direction)."""
    phone = account.phone()
    _change_schema(phone)
    account._full(phone, upload=True)
    before = snapshot(bridge.path)
    with pytest.raises(SyncRequired) as e:
        bridge.add_notes([basic(front="refused")])
    msg = str(e.value)
    assert "full sync" in msg and "yours to choose" in msg and "Nothing was written" in msg
    assert snapshot(bridge.path) == before
    assert server_notes(account) == 0


@pytest.mark.parametrize(
    "kind, words",
    [("FULL_UPLOAD", "uploaded"), ("FULL_DOWNLOAD", "downloaded")],
)
def test_a_directed_full_sync_names_the_direction(account, bridge, monkeypatch, kind, words):
    """Not reachable against the bundled server, so Anki's answer is faked at the one call."""
    from anki.sync_pb2 import SyncCollectionResponse

    fake = SyncCollectionResponse(required=getattr(SyncCollectionResponse.ChangesRequired, kind))
    monkeypatch.setattr(bridge, "_sync_collection", lambda col: fake)
    with pytest.raises(SyncRequired, match=words):
        bridge.sync()
    assert snapshot(bridge.path)[0] == 0


def test_the_server_cannot_reach_a_full_sync():
    """Only bootstrap.py, run by a human, may call full_upload_or_download."""
    here = os.path.dirname(__file__)
    for name in ("core.py", "server.py"):
        with open(os.path.join(here, name)) as f:
            assert "full_upload_or_download" not in f.read(), name


# ---- merging with another device (H1, H3) -----------------------------------------------------------------


def test_a_note_added_on_the_phone_is_merged_before_the_bridge_writes(account, bridge):
    """H1. add_notes syncs first, so the phone's note arrives and neither side loses a note."""
    phone = account.phone()
    note = phone.new_note(phone.models.by_name("Basic"))
    note["Front"], note["Back"] = "from phone", "b"
    phone.add_note(note, phone.decks.id("Physics"))
    account.sync(phone)

    bridge.add_notes([basic(front="from bridge")])

    account.sync(phone)
    on_phone = {phone.get_note(i)["Front"] for i in phone.find_notes("")}
    assert on_phone == {"from phone", "from bridge"}
    assert {*note_texts(Collection(bridge.path))} == on_phone


def test_a_note_edited_on_both_sides_resolves_and_is_returned(account, bridge):
    """H3. The bridge edit was made while disconnected (as Anki desktop would); the phone's edit
    reached AnkiWeb first. The pre-sync resolves it and update_note returns the merged note."""
    nid = bridge.add_notes([basic(front="shared", back="orig")])["added"][0]
    phone = account.phone()
    local = Collection(bridge.path)
    n = local.get_note(nid)
    n["Back"] = "bridge edit"
    local.update_note(n)
    local.close()

    p = phone.get_note(nid)
    p["Back"] = "phone edit"
    phone.update_note(p)
    # Note times have one-second resolution, so two edits in this test would tie; make the phone's later.
    phone.db.execute("update notes set mod = mod + 100 where id = ?", nid)
    account.sync(phone)

    out = bridge.update_note(nid, add_tags=["merged"])["note"]
    assert out["fields"]["Back"] == "phone edit"  # the later edit wins; the bridge's tag change is kept
    assert "merged" in out["tags"]
    account.sync(phone)
    assert phone.get_note(nid)["Back"] == out["fields"]["Back"]
    assert "merged" in phone.get_note(nid).tags


# ---- bootstrap.py, the only code that does a full sync (Task 6) ------------------------------------------


def run_bootstrap(account, direction: str, path: str, stdin: str = ""):
    env = {
        **os.environ,
        "ANKI_COLLECTION": path,
        "ANKIWEB_USER": account.user,
        "ANKIWEB_PASS": account.password,
        "ANKIWEB_ENDPOINT": account.url,
    }
    script = os.path.join(os.path.dirname(__file__), "bootstrap.py")
    return subprocess.run(
        [sys.executable, script, direction], env=env, input=stdin, capture_output=True, text=True, timeout=60
    )


def test_bootstrap_download_into_an_empty_path_gets_the_servers_notes(account, bridge):
    bridge.add_notes([basic(front="on the server")])
    path = os.path.join(tempfile.mkdtemp(), "new", "collection.anki2")  # parent directory does not exist yet
    done = run_bootstrap(account, "download", path)
    assert done.returncode == 0, done.stderr
    assert list(note_texts(Collection(path))) == ["on the server"]


def test_bootstrap_upload_without_typing_overwrite_changes_nothing(account, bridge):
    bridge.add_notes([basic(front="keep me")])
    other = os.path.join(tempfile.mkdtemp(), "c.anki2")
    col = Collection(other)
    col.decks.add_normal_deck_with_name("Other")
    col.close()
    done = run_bootstrap(account, "upload", other, stdin="yes\n")
    assert done.returncode != 0 and "aborted" in done.stderr
    assert list(note_texts(account.phone("check"))) == ["keep me"]


def test_bootstrap_upload_with_overwrite_replaces_the_server(account, bridge):
    bridge.add_notes([basic(front="will be replaced")])
    other = os.path.join(tempfile.mkdtemp(), "c.anki2")
    col = Collection(other)
    col.decks.add_normal_deck_with_name("Other")
    col.close()
    done = run_bootstrap(account, "upload", other, stdin="overwrite\n")
    assert done.returncode == 0, done.stderr
    check = account.phone("check")
    assert list(note_texts(check)) == [] and "Other" in [d.name for d in check.decks.all_names_and_ids()]
