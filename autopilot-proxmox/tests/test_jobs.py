import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from web.jobs import JobManager


@pytest.fixture
def tmp_jobs_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def manager(tmp_jobs_dir):
    mgr = JobManager(jobs_dir=tmp_jobs_dir)
    yield mgr
    # Wait for any active jobs to finish before teardown removes the temp dir
    import time
    for _ in range(50):
        if not mgr._active:
            break
        time.sleep(0.1)


def test_start_job_returns_id(manager):
    job = manager.start("test-playbook", ["echo", "hello"])
    assert job["id"]
    assert job["status"] == "running"
    assert job["playbook"] == "test-playbook"


def test_list_jobs_includes_started_job(manager):
    job = manager.start("test-playbook", ["echo", "hello"])
    jobs = manager.list_jobs()
    assert len(jobs) >= 1
    assert any(j["id"] == job["id"] for j in jobs)


def test_job_completes_with_exit_code(manager):
    job = manager.start("test", ["echo", "hello"])
    import time
    for _ in range(50):
        info = manager.get_job(job["id"])
        if info["status"] != "running":
            break
        time.sleep(0.1)
    info = manager.get_job(job["id"])
    assert info["status"] == "complete"
    assert info["exit_code"] == 0


def test_failed_job_has_failed_status(manager):
    job = manager.start("test", ["false"])
    import time
    for _ in range(50):
        info = manager.get_job(job["id"])
        if info["status"] != "running":
            break
        time.sleep(0.1)
    info = manager.get_job(job["id"])
    assert info["status"] == "failed"
    assert info["exit_code"] != 0


def test_job_log_written_to_disk(manager):
    job = manager.start("test", ["echo", "test-output-line"])
    import time
    for _ in range(50):
        info = manager.get_job(job["id"])
        if info["status"] != "running":
            break
        time.sleep(0.1)
    log_path = os.path.join(manager.jobs_dir, f"{job['id']}.log")
    assert os.path.exists(log_path)
    content = open(log_path).read()
    assert "test-output-line" in content


def test_get_nonexistent_job_returns_none(manager):
    assert manager.get_job("fake-id") is None


def test_index_persisted_to_disk(manager):
    job = manager.start("test", ["echo", "hi"])
    import time
    for _ in range(50):
        info = manager.get_job(job["id"])
        if info["status"] != "running":
            break
        time.sleep(0.1)
    index_path = os.path.join(manager.jobs_dir, "index.json")
    assert os.path.exists(index_path)
    data = json.loads(open(index_path).read())
    assert len(data) >= 1
    assert data[0]["id"] == job["id"]


def test_job_manager_start_inserts_row_in_jobs_db():
    """JobManager.start writes to jobs.db AND still spawns subprocess
    (Phase 0 — web is still the runner). Popen mocked so no real
    subprocess runs."""
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)

        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        with patch("web.jobs.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            mock_popen.return_value.wait.return_value = 0
            entry = mgr.start(
                "build_template", ["ansible-playbook", "x.yml"],
                args={"profile": "p"},
            )
        assert entry["id"]
        row = jobs_db.get_job(db_path, entry["id"])
        assert row is not None
        assert row["job_type"] == "build_template"
        # Row should be running or complete (web claimed immediately).
        assert row["status"] in ("running", "complete")


def test_job_manager_start_without_jobs_db_path_still_works():
    """Backwards compat: if jobs_db_path isn't provided, JobManager
    should still work (no new behavior)."""
    from web import jobs
    with tempfile.TemporaryDirectory() as d:
        mgr = jobs.JobManager(jobs_dir=str(d))  # no jobs_db_path
        with patch("web.jobs.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            mock_popen.return_value.wait.return_value = 0
            entry = mgr.start("test_playbook", ["true"], args={})
        assert entry["id"]
        # No jobs.db was set, so no crash on the absent attribute.
