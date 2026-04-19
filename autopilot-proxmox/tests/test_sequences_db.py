"""Tests for web.sequences_db — schema, credentials, sequences, steps, vm_provisioning."""
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


def test_init_creates_all_tables(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"task_sequences", "task_sequence_steps", "credentials",
            "vm_provisioning"} <= tables


def test_init_is_idempotent(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    sequences_db.init(db_path)  # must not raise


def test_create_credential_encrypts_payload(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher,
        name="acme-svc", type="domain_join",
        payload={"username": "acme\\svc", "password": "p@ss",
                 "domain_fqdn": "acme.local"},
    )
    assert cred_id > 0

    # Raw row must NOT contain the password in plaintext
    import sqlite3
    with sqlite3.connect(db_path) as c:
        row = c.execute("SELECT encrypted_blob FROM credentials WHERE id=?",
                        (cred_id,)).fetchone()
    assert b"p@ss" not in row[0]


def test_get_credential_decrypts(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher,
        name="acme-svc", type="domain_join",
        payload={"username": "acme\\svc", "password": "p@ss",
                 "domain_fqdn": "acme.local"},
    )
    out = sequences_db.get_credential(db_path, cipher, cred_id)
    assert out["name"] == "acme-svc"
    assert out["type"] == "domain_join"
    assert out["payload"]["password"] == "p@ss"


def test_list_credentials_omits_payload(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.create_credential(
        db_path, cipher, name="a", type="local_admin",
        payload={"username": "Administrator", "password": "x"},
    )
    rows = sequences_db.list_credentials(db_path)
    assert len(rows) == 1
    assert "payload" not in rows[0]
    assert "encrypted_blob" not in rows[0]
    assert rows[0]["name"] == "a"


def test_update_credential_replaces_payload(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher, name="a", type="local_admin",
        payload={"username": "Administrator", "password": "old"},
    )
    sequences_db.update_credential(
        db_path, cipher, cred_id,
        name="a", payload={"username": "Administrator", "password": "new"},
    )
    out = sequences_db.get_credential(db_path, cipher, cred_id)
    assert out["payload"]["password"] == "new"


def test_delete_credential_succeeds_when_unreferenced(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher, name="a", type="local_admin",
        payload={"username": "x", "password": "y"},
    )
    sequences_db.delete_credential(db_path, cred_id)
    assert sequences_db.list_credentials(db_path) == []
