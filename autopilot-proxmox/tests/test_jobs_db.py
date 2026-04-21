import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "jobs.db"


def test_init_creates_tables(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "jobs" in names
    assert "job_type_limits" in names


def test_init_is_idempotent(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.init(db_path)  # should not raise


def test_init_seeds_default_concurrency_caps(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    caps = jobs_db.list_job_type_limits(db_path)
    by_type = {row["job_type"]: row["max_concurrent"] for row in caps}
    # Per spec §2 "Per-type concurrency caps"
    assert by_type["build_template"] == 1
    assert by_type["provision_clone"] == 3
    assert by_type["capture_hash"] == 5
    assert by_type["hash_upload"] == 5
    assert by_type["retry_inject_hash"] == 3


def test_wal_mode_enabled(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_init_preserves_operator_tuned_concurrency_caps(db_path):
    """INSERT OR IGNORE in init() is what lets operators tune caps via
    /settings without having the next container restart overwrite the
    value. Break this and operator config silently reverts."""
    from web import jobs_db
    jobs_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE job_type_limits SET max_concurrent = 7 "
            "WHERE job_type = 'provision_clone'"
        )
        conn.commit()
    # Re-init should NOT clobber the tuned value.
    jobs_db.init(db_path)
    caps = {r["job_type"]: r["max_concurrent"]
            for r in jobs_db.list_job_type_limits(db_path)}
    assert caps["provision_clone"] == 7
    # Other defaults still seeded as expected.
    assert caps["build_template"] == 1


def test_enqueue_creates_pending_job(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    job = jobs_db.enqueue(
        db_path,
        job_id="20260421-abcd",
        job_type="provision_clone",
        playbook="/app/playbooks/provision_clone.yml",
        cmd=["ansible-playbook", "/app/playbooks/provision_clone.yml", "-e", "vm_count=1"],
        args={"vm_count": 1, "hostname_pattern": "autopilot-{serial}"},
    )
    assert job["id"] == "20260421-abcd"
    assert job["status"] == "pending"
    assert job["worker_id"] is None
    assert job["kill_requested"] == 0
    assert job["created_at"]
    assert job["claimed_at"] is None

    got = jobs_db.get_job(db_path, "20260421-abcd")
    assert got["id"] == "20260421-abcd"
    assert got["args"]["vm_count"] == 1
    assert got["cmd"][0] == "ansible-playbook"


def test_list_jobs_orders_newest_first(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="older", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.enqueue(db_path, job_id="newer", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    rows = jobs_db.list_jobs(db_path)
    assert [r["id"] for r in rows] == ["newer", "older"]


def test_get_job_returns_none_for_missing(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    assert jobs_db.get_job(db_path, "does-not-exist") is None
