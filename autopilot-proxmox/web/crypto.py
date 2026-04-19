"""Fernet-based symmetric encryption for credential payloads.

The key lives in a file outside the Ansible vault so that rotating
the credential key doesn't touch unrelated secrets. Key file is
0600 on disk and auto-generated on first use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_generate_key(key_path: Path) -> bytes:
    """Return the Fernet key at ``key_path``, creating it if absent.

    The file is written with mode 0600.
    """
    key_path = Path(key_path)
    if key_path.exists():
        return key_path.read_bytes().strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write with restrictive permissions from the start — avoid the
    # race of "create open, chmod later".
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


class Cipher:
    """Thin wrapper over Fernet that also handles JSON payloads."""

    def __init__(self, key_path: Path) -> None:
        self._fernet = Fernet(load_or_generate_key(Path(key_path)))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        return self._fernet.decrypt(token)

    def encrypt_json(self, payload: dict) -> bytes:
        return self.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def decrypt_json(self, token: bytes) -> dict:
        return json.loads(self.decrypt(token).decode("utf-8"))
