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


def test_create_run_returns_id_with_null_vmid(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    assert run_id == 1
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["vmid"] is None
    assert run["vm_uuid"] is None
    assert run["state"] == "queued"
    assert run["provision_path"] == "winpe"


def test_set_run_identity_populates_vmid_and_uuid(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1234,
        vm_uuid="00000000-0000-0000-0000-aabbccddeeff",
    )
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["vmid"] == 1234
    assert run["vm_uuid"] == "00000000-0000-0000-0000-aabbccddeeff"
    assert run["state"] == "awaiting_winpe"


def test_get_run_by_uuid_state_finds_match(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1234, vm_uuid="abc",
    )
    found = sequences_db.find_run_by_uuid_state(
        db_path, vm_uuid="abc", state="awaiting_winpe",
    )
    assert found["id"] == run_id


def test_get_run_by_uuid_state_returns_none_when_state_wrong(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1234, vm_uuid="abc",
    )
    found = sequences_db.find_run_by_uuid_state(
        db_path, vm_uuid="abc", state="firstlogon",
    )
    assert found is None


def test_append_step_assigns_order_index_and_pending_state(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    s1 = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="partition_disk",
        params={"layout": "default"},
    )
    s2 = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    assert s1["order_index"] == 0
    assert s2["order_index"] == 1
    assert s1["state"] == "pending"
    assert s1["params_json"] == '{"layout": "default"}'


def test_update_step_state_records_timestamps(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    s = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="running",
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="ok",
    )
    steps = sequences_db.list_run_steps(db_path, run_id=run_id)
    assert steps[0]["state"] == "ok"
    assert steps[0]["started_at"] is not None
    assert steps[0]["finished_at"] is not None


def test_sweep_stale_runs_marks_run_failed_after_ttl(db_path, monkeypatch):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="x", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1, vm_uuid="u",
    )
    s = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="running",
    )
    # Force the step's started_at into the distant past so it looks stale.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE provisioning_run_steps "
            "SET started_at = '2000-01-01T00:00:00+00:00' WHERE id=?",
            (s["id"],),
        )

    n = sequences_db.sweep_stale_runs(db_path, ttl_seconds=600)
    assert n == 1
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["state"] == "failed"
    assert "stale" in (run["last_error"] or "")


def test_sweep_stale_runs_leaves_active_runs_alone(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="y", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1, vm_uuid="u",
    )
    s = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="running",
    )
    n = sequences_db.sweep_stale_runs(db_path, ttl_seconds=3600)
    assert n == 0
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["state"] == "awaiting_winpe"


def test_sweep_stale_runs_skips_runs_in_terminal_state(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="z", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1, vm_uuid="u",
    )
    sequences_db.update_provisioning_run_state(
        db_path, run_id=run_id, state="done",
    )
    n = sequences_db.sweep_stale_runs(db_path, ttl_seconds=1)
    assert n == 0
