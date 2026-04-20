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


def test_delete_credential_blocked_if_referenced(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher, name="a", type="domain_join",
        payload={"username": "x", "password": "y", "domain_fqdn": "z"},
    )
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "join_ad_domain",
         "params": {"credential_id": cred_id, "ou_path": "OU=X"},
         "enabled": True},
    ])
    with pytest.raises(sequences_db.CredentialInUse) as exc:
        sequences_db.delete_credential(db_path, cred_id)
    assert seq_id in exc.value.sequence_ids


def test_create_sequence(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="Entra Join", description="default flow",
        is_default=True, produces_autopilot_hash=True,
    )
    assert seq_id > 0
    seq = sequences_db.get_sequence(db_path, seq_id)
    assert seq["name"] == "Entra Join"
    assert seq["is_default"] is True
    assert seq["produces_autopilot_hash"] is True
    assert seq["steps"] == []


def test_only_one_default_sequence(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    a = sequences_db.create_sequence(db_path, name="A", description="",
                                     is_default=True)
    b = sequences_db.create_sequence(db_path, name="B", description="",
                                     is_default=True)
    seq_a = sequences_db.get_sequence(db_path, a)
    seq_b = sequences_db.get_sequence(db_path, b)
    assert seq_a["is_default"] is False
    assert seq_b["is_default"] is True


def test_set_sequence_steps_replaces(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "dell-latitude-5540"}, "enabled": True},
        {"step_type": "local_admin",
         "params": {"credential_id": 1}, "enabled": True},
    ])
    seq = sequences_db.get_sequence(db_path, seq_id)
    assert [s["step_type"] for s in seq["steps"]] == [
        "set_oem_hardware", "local_admin"]
    assert seq["steps"][0]["order_index"] == 0
    assert seq["steps"][1]["order_index"] == 1

    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])
    seq = sequences_db.get_sequence(db_path, seq_id)
    assert [s["step_type"] for s in seq["steps"]] == ["autopilot_entra"]


def test_list_sequences_summary(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    sequences_db.create_sequence(db_path, name="A", description="")
    sequences_db.create_sequence(db_path, name="B", description="")
    out = sequences_db.list_sequences(db_path)
    assert [s["name"] for s in out] == ["A", "B"]
    assert "steps" not in out[0]
    assert "step_count" in out[0]


def test_delete_sequence_cascade_steps(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])
    sequences_db.delete_sequence(db_path, seq_id)
    import sqlite3
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM task_sequence_steps").fetchone()[0]
    assert n == 0


def test_delete_sequence_blocked_if_referenced_by_vm(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.record_vm_provisioning(db_path, vmid=101, sequence_id=seq_id)
    with pytest.raises(sequences_db.SequenceInUse) as exc:
        sequences_db.delete_sequence(db_path, seq_id)
    assert 101 in exc.value.vmids


def test_duplicate_sequence_copies_steps(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "set_oem_hardware", "params": {"oem_profile": "x"},
         "enabled": True},
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])
    new_id = sequences_db.duplicate_sequence(db_path, seq_id, new_name="S (copy)")
    new_seq = sequences_db.get_sequence(db_path, new_id)
    assert new_seq["name"] == "S (copy)"
    assert [s["step_type"] for s in new_seq["steps"]] == [
        "set_oem_hardware", "autopilot_entra"]
    assert new_seq["is_default"] is False


def test_record_vm_provisioning_upsert(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.record_vm_provisioning(db_path, vmid=101, sequence_id=seq_id)
    sequences_db.record_vm_provisioning(db_path, vmid=101, sequence_id=seq_id)
    assert sequences_db.get_vm_sequence_id(db_path, 101) == seq_id


def test_get_default_sequence(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    a = sequences_db.create_sequence(db_path, name="A", description="")
    b = sequences_db.create_sequence(db_path, name="B", description="",
                                     is_default=True)
    assert sequences_db.get_default_sequence_id(db_path) == b
    sequences_db.update_sequence(db_path, b, is_default=False)
    assert sequences_db.get_default_sequence_id(db_path) is None


def test_update_default_preserved_on_unique_conflict(db_path):
    """If update_sequence(is_default=True) fails due to a UNIQUE name
    collision, the existing default must still be the default afterward
    — otherwise the DB ends up with zero defaults."""
    import sqlite3
    from web import sequences_db
    sequences_db.init(db_path)
    a = sequences_db.create_sequence(db_path, name="A", description="",
                                     is_default=True)
    b = sequences_db.create_sequence(db_path, name="B", description="")
    with pytest.raises(sqlite3.IntegrityError):
        # Renaming B to "A" violates UNIQUE(name); the is_default=True
        # request must not demote A's default status if this fails.
        sequences_db.update_sequence(db_path, b, name="A", is_default=True)
    assert sequences_db.get_default_sequence_id(db_path) == a


def test_seed_defaults_inserts_three_on_empty(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    names = [s["name"] for s in sequences_db.list_sequences(db_path)]
    assert "Entra Join (default)" in names
    assert "AD Domain Join — Local Admin" in names
    assert "Hybrid Autopilot (stub)" in names


def test_seed_defaults_idempotent(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    sequences_db.seed_defaults(db_path, cipher)  # second call no-op
    assert len(sequences_db.list_sequences(db_path)) == 3


def test_seed_creates_default_credential(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    creds = sequences_db.list_credentials(db_path, type="local_admin")
    assert any(c["name"] == "default-local-admin" for c in creds)


def test_seed_entra_sequence_is_default_and_produces_hash(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    for s in sequences_db.list_sequences(db_path):
        if s["name"] == "Entra Join (default)":
            assert s["is_default"] is True
            assert s["produces_autopilot_hash"] is True
            break
    else:
        pytest.fail("Entra Join (default) not found")


def test_seed_default_sequences_all_compile_cleanly(db_path, key_path):
    """After B.2a, all three seeded sequences must compile without error
    (except the Hybrid stub which is expected to raise StepNotImplemented).
    The AD sequence compiles clean BUT emits a runonce step with
    credential_id=0 which would fail at render time — that's by design;
    the operator wires up a real credential before provisioning."""
    from web import crypto, sequences_db, sequence_compiler
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    for s in sequences_db.list_sequences(db_path):
        seq = sequences_db.get_sequence(db_path, s["id"])
        if s["name"] == "Hybrid Autopilot (stub)":
            with pytest.raises(sequence_compiler.StepNotImplemented):
                sequence_compiler.compile(seq)
        elif s["name"] == "AD Domain Join — Local Admin":
            # Compiles (join_ad_domain has credential_id=0 which is truthy
            # to the compiler — the renderer rejects later).
            compiled = sequence_compiler.compile(seq)
            # Should have the RunOnce steps present
            types = [x["step_type"] for x in compiled.runonce_steps]
            assert "join_ad_domain" in types
            assert "rename_computer" in types
        else:
            compiled = sequence_compiler.compile(seq)
