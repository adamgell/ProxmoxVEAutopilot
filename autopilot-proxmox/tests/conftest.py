"""Shared pytest fixtures for the autopilot-proxmox test suite.

These are opt-in helpers for new tests. Existing tests keep their own setup
blocks unchanged so they continue to pass as-is.

Fixtures exposed
----------------
- ``tmp_secrets_dir``: an isolated writable secrets dir pre-seeded with a
  fresh Fernet credential_key file.
- ``test_db``: a freshly initialised sequences.db (no default seeds).
- ``seeded_db``: a sequences.db initialised AND seeded with the three
  default sequences.
- ``web_client``: a FastAPI ``TestClient`` wired to ``test_db`` +
  ``tmp_secrets_dir``.
- ``seeded_web_client``: the same, but against the seeded DB.

The web fixtures ``monkeypatch`` ``web.app.SEQUENCES_DB``,
``web.app.SECRETS_DIR``, and ``web.app.CREDENTIAL_KEY`` so the real on-disk
paths are never touched, and reset the process-wide cipher cache
(``web.app._CIPHER``) so the isolated credential key is actually used.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def tmp_secrets_dir(tmp_path: Path) -> Path:
    """Writable secrets dir with a fresh Fernet ``credential_key``."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "credential_key").write_bytes(Fernet.generate_key())
    return secrets


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    """Fresh, initialised sequences DB (no default sequences seeded)."""
    from web import sequences_db

    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    return db


@pytest.fixture
def seeded_db(test_db: Path, tmp_secrets_dir: Path) -> Path:
    """Sequences DB initialised + seeded with the default sequences."""
    from web import sequences_db
    from web.crypto import Cipher

    sequences_db.seed_defaults(
        test_db,
        Cipher(tmp_secrets_dir / "credential_key"),
    )
    return test_db


def _patch_app_paths(monkeypatch, db: Path, secrets: Path, tmp_path: Path) -> None:
    """Redirect web.app's on-disk state at the module level."""
    import web.app as web_app

    # Reset the cached cipher so our isolated credential_key takes effect.
    web_app._CIPHER = None
    monkeypatch.setattr(web_app, "SEQUENCES_DB", db)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secrets)
    monkeypatch.setattr(web_app, "CREDENTIAL_KEY", secrets / "credential_key")
    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path / "hashes")


@pytest.fixture
def web_client(test_db, tmp_secrets_dir, tmp_path, monkeypatch):
    """FastAPI ``TestClient`` wired to an isolated, empty sequences DB."""
    from fastapi.testclient import TestClient

    _patch_app_paths(monkeypatch, test_db, tmp_secrets_dir, tmp_path)
    from web.app import app

    return TestClient(app)


@pytest.fixture
def seeded_web_client(seeded_db, tmp_secrets_dir, tmp_path, monkeypatch):
    """FastAPI ``TestClient`` wired to a DB pre-seeded with defaults."""
    from fastapi.testclient import TestClient

    _patch_app_paths(monkeypatch, seeded_db, tmp_secrets_dir, tmp_path)
    from web.app import app

    return TestClient(app)
