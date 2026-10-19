"""Encryption for the AnkiWeb logins the server keeps. No Anki or MCP code here.

What is kept is AnkiWeb's sync key (never the password), encrypted with AES-GCM under a random
data key per person. The data key itself is stored only wrapped:

- under a key derived from the person's login secret (scrypt), unwrapped when they log in;
- under each of their live authorization codes and tokens, unwrapped when Claude presents one.

So the server's files alone (database, users file, env, backups) cannot decrypt a login: that
takes the person's secret or one of their live tokens, which only their Claude connector holds.
The running server does see the plaintext while it uses it; nothing at rest can protect that.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_NONCE = 12
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}


def new_key() -> bytes:
    return os.urandom(32)


def secret_key(secret: str, salt: bytes) -> bytes:
    """Key from a login secret. The salt is the vault's own, never the login hash's: the stored
    login hash must not double as the key."""
    n, r, p = _SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"]
    return hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * n * r * 2, dklen=32)


def token_key(token: str) -> bytes:
    """Key from a code or token (256 random bits, so no slow KDF needed). Independent of the
    SHA-256 the database stores to look the token up."""
    return hmac.new(token.encode(), b"anki-bridge/token-wrap/v1", hashlib.sha256).digest()


def seal(key: bytes, plaintext: bytes, context: str) -> bytes:
    """`context` (who and what) is authenticated, so a blob cannot be moved to another person."""
    nonce = os.urandom(_NONCE)
    return _VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, context.encode())


def unseal(key: bytes, blob: bytes | None, context: str) -> bytes | None:
    """None if the blob is missing, not ours, tampered with, or the key is wrong."""
    if not blob or blob[:1] != _VERSION:
        return None
    nonce, body = blob[1 : 1 + _NONCE], blob[1 + _NONCE :]
    try:
        return AESGCM(key).decrypt(nonce, body, context.encode())
    except (InvalidTag, ValueError):
        return None
