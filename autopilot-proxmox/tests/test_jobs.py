import os
import tempfile
from pathlib import Path

import pytest

from web.jobs import JobManager


def test_job_manager_start_enqueues_pending_row():
    """Task 13: JobManager is enqueue-only. start() writes a pending
    row to jobs.db and returns a legacy-shaped dict — no subprocess,
    no thread."""
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)
        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        entry = mgr.start(
            "build_template",
            ["ansible-playbook", "x.yml"],
            args={"profile": "p"},
        )
        row = jobs_db.get_job(db_path, entry["id"])
    assert row is not None
    assert row["status"] == "pending"
    assert row["job_type"] == "build_template"
    assert row["playbook"] == "x.yml"
    assert row["args"] == {"profile": "p"}
    assert entry["id"]
    assert entry["status"] == "pending"
    assert entry["playbook"] == "build_template"
    assert entry["ended"] is None
    assert entry["exit_code"] is None


def test_job_manager_start_pretouches_log_file():
    """The log file must exist after start(), so /jobs/<id> tailing
    doesn't 500 before the builder writes anything."""
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)
        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        entry = mgr.start("build_template", ["true"], args={})
        log_path = jobs_dir / f"{entry['id']}.log"
        assert log_path.exists()


def test_job_manager_get_log_returns_empty_for_missing():
    from web import jobs
    with tempfile.TemporaryDirectory() as d:
        mgr = jobs.JobManager(jobs_dir=d, jobs_db_path=Path(d) / "jobs.db")
        assert mgr.get_log("missing") == ""


def test_job_manager_get_log_reads_from_disk():
    from web import jobs
    with tempfile.TemporaryDirectory() as d:
        mgr = jobs.JobManager(jobs_dir=d, jobs_db_path=Path(d) / "jobs.db")
        log_path = Path(d) / "abc.log"
        log_path.write_text("hello\nworld\n")
        assert mgr.get_log("abc") == "hello\nworld\n"


def test_job_manager_list_and_get_delegate_to_jobs_db():
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)
        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        e1 = mgr.start("build_template", ["true"], args={})
        e2 = mgr.start("provision_clone", ["true"], args={})

        listed = mgr.list_jobs()
        listed_ids = {j["id"] for j in listed}
        assert e1["id"] in listed_ids
        assert e2["id"] in listed_ids

        got = mgr.get_job(e1["id"])
        assert got is not None
        assert got["id"] == e1["id"]
        assert got["status"] == "pending"

        assert mgr.get_job("does-not-exist") is None


def test_job_manager_get_nonexistent_job_returns_none():
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)
        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        assert mgr.get_job("fake-id") is None


def test_job_manager_add_on_complete_is_noop(caplog):
    """add_on_complete is kept as a no-op so lingering callers don't
    crash, but it logs a warning — the callback cannot fire after the
    builder split."""
    import logging
    from web import jobs
    with tempfile.TemporaryDirectory() as d:
        mgr = jobs.JobManager(jobs_dir=d, jobs_db_path=Path(d) / "jobs.db")
        with caplog.at_level(logging.WARNING, logger="web.jobs"):
            mgr.add_on_complete("some-id", lambda job: None)
        assert any(
            "add_on_complete is a no-op" in r.message for r in caplog.records
        )


def test_job_manager_no_longer_has_subprocess_attrs():
    """Sanity check: the subprocess-era attributes are gone."""
    from web import jobs
    with tempfile.TemporaryDirectory() as d:
        mgr = jobs.JobManager(jobs_dir=d, jobs_db_path=Path(d) / "jobs.db")
        # These should not exist post-Task-13.
        assert not hasattr(mgr, "_active")
        assert not hasattr(mgr, "_wait_for_completion")
        assert not hasattr(mgr, "_cleanup_orphans")
        assert not hasattr(mgr, "_load_index")
        assert not hasattr(mgr, "_save_index")
