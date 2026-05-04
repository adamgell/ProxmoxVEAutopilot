"""Tests for provisioning_runs + provisioning_run_steps schema."""
import sqlite3
import pytest


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sequences.db"


def test_init_creates_provisioning_runs_table(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "provisioning_runs" in tables
    assert "provisioning_run_steps" in tables


def test_provisioning_runs_columns(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {r[1]: r for r in conn.execute(
            "PRAGMA table_info(provisioning_runs)"
        )}
    assert "id" in cols and "vmid" in cols and "vm_uuid" in cols
    assert "provision_path" in cols and "state" in cols
    # vmid must be NULLABLE because Ansible owns /cluster/nextid allocation
    assert cols["vmid"][3] == 0, "vmid must be NULL-able"


def test_provision_path_check_constraint(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        # Need a sequence to satisfy FK
        conn.execute(
            "INSERT INTO task_sequences (name,description,created_at,updated_at)"
            " VALUES ('x','',datetime('now'),datetime('now'))"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO provisioning_runs "
                "(sequence_id, provision_path, state, started_at) "
                "VALUES (1, 'pxe', 'queued', datetime('now'))"
            )


def test_provisioning_run_steps_cascade_delete(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO task_sequences (name,description,created_at,updated_at)"
            " VALUES ('x','',datetime('now'),datetime('now'))"
        )
        conn.execute(
            "INSERT INTO provisioning_runs "
            "(sequence_id, provision_path, state, started_at) "
            "VALUES (1, 'winpe', 'queued', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO provisioning_run_steps "
            "(run_id, order_index, phase, kind, state) "
            "VALUES (1, 0, 'winpe', 'apply_wim', 'pending')"
        )
        conn.execute("DELETE FROM provisioning_runs WHERE id=1")
        n = conn.execute(
            "SELECT COUNT(*) FROM provisioning_run_steps"
        ).fetchone()[0]
    assert n == 0


def test_uuid_state_index_present(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
    assert "idx_provisioning_runs_vm_uuid_state" in idx
