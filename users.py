"""People besides the owner, from the users file (USERS_FILE). No Anki or MCP code here.

The owner is not in this file: they stay configured by the env vars. A name in this file is
that person's username and subject. The file holds only login secret hashes: each person enters
their AnkiWeb login on the login page, and it is stored encrypted (vault.py), never here.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass

from auth import OWNER_SUBJECT, USERNAME_OK

_ANKIWEB_KEYS = {"ankiweb_user", "ankiweb_pass", "ankiweb_endpoint"}


class UsersFileError(Exception):
    """The users file is unsafe or malformed. The server refuses to start."""


@dataclass(frozen=True)
class User:
    name: str
    secret_hash: str


def load_users(path: str) -> dict[str, User]:
    try:
        mode = os.stat(path).st_mode
    except OSError as e:
        raise UsersFileError(f"cannot read the users file {path}: {e.strerror}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UsersFileError(f"{path} must be readable by its owner only (chmod 600)")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise UsersFileError(f"{path} is not valid TOML: {e}") from None

    tables = data.get("users", {})
    if set(data) - {"users"} or not isinstance(tables, dict):
        raise UsersFileError(f"{path}: only [users.<name>] tables are allowed")
    users: dict[str, User] = {}
    for name, entry in tables.items():
        where = f"{path}: [users.{name}]"
        if not isinstance(entry, dict):
            raise UsersFileError(f"{where} must be a table")
        if name == OWNER_SUBJECT or not USERNAME_OK.match(name):
            raise UsersFileError(f"{where}: a name is lowercase letters, digits, - or _ (at most 32), and not {OWNER_SUBJECT!r}")
        if set(entry) & _ANKIWEB_KEYS:
            raise UsersFileError(
                f"{where}: AnkiWeb logins do not go in this file. Remove them; the person enters theirs "
                "on the login page, where it is stored encrypted."
            )
        unknown = sorted(set(entry) - {"secret_hash"})
        if unknown:
            raise UsersFileError(f"{where}: unknown key(s) {unknown}")
        secret_hash = entry.get("secret_hash", "")
        if not isinstance(secret_hash, str) or not secret_hash.startswith("scrypt$"):
            raise UsersFileError(f"{where}: secret_hash must come from `python auth.py hash {name}`")
        users[name] = User(name, secret_hash)
    return users
