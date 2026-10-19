"""The deploy files must agree with the server and with each other. No VPS needed."""

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

from auth import hash_secret
from server import ConfigError, load_config

HERE = os.path.dirname(__file__)
DEPLOY = os.path.join(HERE, "deploy")


def read(name: str) -> str:
    with open(os.path.join(DEPLOY, name)) as f:
        return f.read()


def env_example() -> dict[str, str]:
    out = {}
    for line in read("env.example").splitlines():
        if line.strip() and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_env_example_loads_as_a_valid_production_config():
    env = {**env_example(), "OWNER_SECRET_HASH": hash_secret("x")}
    cfg = load_config(env)
    assert cfg.auth_enabled and cfg.trust_proxy
    assert cfg.public_url == "https://anki.panzeilkamp.com"
    assert cfg.host == "127.0.0.1"


def test_env_example_as_is_is_refused_until_the_placeholder_hash_is_replaced():
    with pytest.raises(ConfigError):
        load_config(env_example())


def test_env_example_never_enables_the_insecure_mode_and_holds_no_secrets():
    env = env_example()
    assert "ALLOW_INSECURE_NO_AUTH" not in env
    assert env["ANKIWEB_USER"] == "" and env["ANKIWEB_PASS"] == ""
    assert "REPLACE_ME" in env["OWNER_SECRET_HASH"]


def test_env_example_leaves_other_people_off():
    assert "USERS_FILE" not in env_example()  # commented out: the owner-only server is the default
    assert "#USERS_FILE=/srv/anki-bridge/users.toml" in read("env.example")


def test_users_example_is_refused_as_is_and_loads_once_filled_in():
    from users import UsersFileError, load_users

    path = os.path.join(tempfile.mkdtemp(), "users.toml")
    with open(path, "w") as f:
        f.write(read("users.example.toml"))
    os.chmod(path, 0o600)
    with pytest.raises(UsersFileError):
        load_users(path)
    with open(path, "w") as f:
        f.write(read("users.example.toml").replace("REPLACE_ME", hash_secret("x")))
    assert list(load_users(path)) == ["alice"]


def test_service_and_nginx_use_the_port_from_the_env_file():
    port = env_example()["PORT"]
    assert port != "8000"  # the other site on the VPS
    assert f"proxy_pass http://127.0.0.1:{port};" in read("nginx-anki.conf")


def test_nginx_block_keeps_the_headers_the_server_relies_on():
    conf = read("nginx-anki.conf")
    for needle in (
        "proxy_set_header Host $host;",
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
        "proxy_set_header X-Forwarded-Proto $scheme;",
        "proxy_http_version 1.1;",
        "proxy_buffering off;",
        "server_name anki.panzeilkamp.com;",
    ):
        assert needle in conf, needle
    for name in ("nginx-anki.conf", "nginx-anki-http.conf"):
        assert "default_server" not in read(name), name


def test_the_unit_runs_unprivileged_and_hardened():
    unit = read("anki-bridge.service")
    assert re.search(r"^User=anki$", unit, re.M)
    assert re.search(r"^EnvironmentFile=/srv/anki-bridge/env$", unit, re.M)
    for needle in ("NoNewPrivileges=true", "ProtectSystem=strict", "ReadWritePaths=/srv/anki-bridge", "Restart=on-failure"):
        assert needle in unit, needle
    assert "ALLOW_INSECURE_NO_AUTH" not in unit


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_the_unit_passes_systemd_analyze():
    out = subprocess.run(
        ["systemd-analyze", "verify", os.path.join(DEPLOY, "anki-bridge.service")], capture_output=True, text=True
    )
    # The anki user and /usr/bin/nix exist only on the VPS.
    problems = [l for l in (out.stdout + out.stderr).splitlines() if l and "anki" not in l and "/usr/bin/nix" not in l]
    assert not problems, problems


def test_nothing_in_deploy_looks_like_a_secret():
    for name in os.listdir(DEPLOY):
        text = read(name)
        assert not re.search(r"scrypt\$[0-9a-f]{8,}", text), name
        assert not re.search(r"(?i)(pass|secret|token)\w*=[^\s#]+", text.replace("OWNER_SECRET_HASH=REPLACE_ME", "")), name


# ---- check.sh against a real local server ----------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(extra_env: dict[str, str], port: int):
    data = tempfile.mkdtemp()
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(port), "DATA_DIR": data, **extra_env}
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while time.time() < deadline and proc.poll() is None:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
        except urllib.error.HTTPError:
            return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    pytest.skip("server did not start")


def _check(url: str):
    return subprocess.run(["bash", os.path.join(DEPLOY, "check.sh"), url], capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
def test_check_script_passes_against_a_server_with_auth_and_fails_without():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = _serve({"PUBLIC_URL": url, "OWNER_SECRET_HASH": hash_secret("x")}, port)
    try:
        done = _check(url)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "all checks passed" in done.stdout
    finally:
        proc.kill()
        proc.wait()

    port = _free_port()
    proc = _serve({"ALLOW_INSECURE_NO_AUTH": "1"}, port)
    try:
        done = _check(f"http://127.0.0.1:{port}")
        assert done.returncode != 0 and "401" in done.stderr
    finally:
        proc.kill()
        proc.wait()
