"""
Each user's own API keys, encrypted at rest.

Keys are stored in auth/user-keys.json (or DATA_DIR/auth/ on a host), one record
per user and key name. Each value is sealed with AES-256-GCM under a master key,
with a fresh random nonce, and with the owner's username and the key's name bound
in as associated data, so a record copied into another user's slot, or renamed,
fails to decrypt instead of quietly working.

The master key comes from the SECRETS_KEY setting (any long random string; on
Render, let it generate one). Without it, a random key is created once in
auth/secret.key, readable only by the account running the dashboard. Lose the
master key and the stored API keys can't be recovered: users simply enter them
again.

Plain values never leave this module except to the code that calls an AI
provider. The browser only ever sees a hint: the last four characters.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import auth

STORE = auth.DIR / "user-keys.json"
KEY_FILE = auth.DIR / "secret.key"
MAX_VALUE = 1000
_lock = threading.Lock()
_master = None


def _master_key():
    global _master
    if _master:
        return _master
    secret = os.environ.get("SECRETS_KEY", "").strip()
    if not secret:
        if not KEY_FILE.exists():
            auth.DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        secret = KEY_FILE.read_text().strip()
    if len(secret) < 16:
        raise RuntimeError("SECRETS_KEY is too short; use a long random value")
    _master = hashlib.sha256(b"nightly-sweep vault v1\0" + secret.encode()).digest()
    return _master


def _aad(username, name):
    return f"{username}\0{name}".encode()


def _load():
    return auth._read(STORE)


def _save(data):
    auth._write(STORE, data)


def set_key(username, name, value):
    value = (value or "").strip()
    if not value or len(value) > MAX_VALUE:
        raise ValueError("Paste the whole key (it can't be empty).")
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_master_key()).encrypt(nonce, value.encode(), _aad(username, name))
    record = {"v": 1, "nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(sealed).decode(),
              "hint": value[-4:] if len(value) >= 12 else "", "updated": time.time()}
    with _lock:
        data = _load()
        data.setdefault(username, {})[name] = record
        _save(data)


def delete_key(username, name):
    with _lock:
        data = _load()
        if data.get(username, {}).pop(name, None) is not None:
            if not data[username]:
                del data[username]
            _save(data)
            return True
    return False


def delete_user(username):
    with _lock:
        data = _load()
        if data.pop(username, None) is not None:
            _save(data)


def list_keys(username):
    """Names, hints and dates only. Never the values."""
    return {name: {"hint": r.get("hint", ""), "updated": r.get("updated")}
            for name, r in _load().get(username, {}).items()}


def get_keys(username):
    """Decrypted keys for one user, for the code that calls AI providers. Unreadable records are skipped."""
    out = {}
    for name, r in _load().get(username, {}).items():
        try:
            out[name] = AESGCM(_master_key()).decrypt(
                base64.b64decode(r["nonce"]), base64.b64decode(r["ct"]), _aad(username, name)).decode()
        except (InvalidTag, KeyError, ValueError):
            continue     # wrong master key, or a record that was moved or tampered with
    return out


def unreadable(username):
    """Names whose records can't be decrypted (e.g. the master key changed)."""
    readable = get_keys(username)
    return sorted(n for n in _load().get(username, {}) if n not in readable)
