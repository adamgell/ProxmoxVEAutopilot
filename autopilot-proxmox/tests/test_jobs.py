import asyncio
import json
import os
import tempfile

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
