"""Seed: Ubuntu sequences are inserted on empty DB alongside the Windows ones."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sequences.db"


@pytest.fixture
def key_path(tmp_path):
    from web import crypto
    key = tmp_path / "credential_key"
    crypto.load_or_generate_key(key)
    return key


def test_seed_inserts_ubuntu_sequences(db_path, key_path) -> None:
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    seqs = sequences_db.list_sequences(db_path)
    by_name = {s["name"]: s for s in seqs}

    assert "Ubuntu Intune + MDE (LinuxESP)" in by_name
    assert "Ubuntu Plain" in by_name

    ubu = by_name["Ubuntu Intune + MDE (LinuxESP)"]
    assert ubu["target_os"] == "ubuntu"
    assert ubu["produces_autopilot_hash"] is False

    full = sequences_db.get_sequence(db_path, ubu["id"])
    step_types = [s["step_type"] for s in full["steps"]]
    assert "install_ubuntu_core" in step_types
    assert "create_ubuntu_user" in step_types
    assert "install_apt_packages" in step_types
    assert "install_snap_packages" in step_types
    assert "install_intune_portal" in step_types
    assert "install_edge" in step_types
    assert "install_mde_linux" in step_types
    assert "remove_apt_packages" in step_types


def test_seed_is_idempotent(db_path, key_path) -> None:
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    first = len(sequences_db.list_sequences(db_path))
    sequences_db.seed_defaults(db_path, cipher)
    second = len(sequences_db.list_sequences(db_path))
    assert first == second
