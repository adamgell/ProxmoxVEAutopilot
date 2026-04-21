import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_env():
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d) / "jobs"; jobs_dir.mkdir()
        db_path = Path(d) / "jobs.db"
        from web import jobs_db
        jobs_db.init(db_path)
        yield jobs_dir, db_path


def test_builder_runs_one_job_and_exits_on_stop(tmp_env):
    """Happy path: enqueue a job, builder claims + spawns + finalizes."""
    from web import builder, jobs_db
    jobs_dir, db_path = tmp_env

    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=["echo", "ok"], args={})

    stop = threading.Event()

    def _fake_run(row, log_path, db_path, worker_id, stop_event):
        jobs_db.finalize_job(db_path, row["id"], exit_code=0)

    with patch("web.builder._run_one_job", side_effect=_fake_run):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={"jobs_dir": jobs_dir, "db_path": db_path,
                    "monitor_db_path": db_path.parent / "device_monitor.db",
                    "worker_id": "test-worker", "stop_event": stop,
                    "poll_interval_seconds": 0.1},
            daemon=True,
        )
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

    assert jobs_db.get_job(db_path, "j1")["status"] == "complete"


def test_builder_run_one_job_kills_on_kill_requested(tmp_env):
    """With kill_requested=1, the heartbeat tick should terminate the
    subprocess."""
    from web import builder, jobs_db
    jobs_dir, db_path = tmp_env

    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=["sleep", "30"], args={})
    jobs_db.claim_next_job(db_path, worker_id="test-worker")

    proc = MagicMock()
    # First .poll() returns None (running), second returns 0 (after terminate)
    proc.poll.side_effect = [None, 0]
    proc.terminate = MagicMock()

    row = jobs_db.get_job(db_path, "j1")
    jobs_db.request_kill(db_path, "j1")

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()

    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row, log_path=log_path, db_path=db_path,
            worker_id="test-worker", stop_event=stop,
            heartbeat_seconds=0.05,
        )

    proc.terminate.assert_called_once()
    # Since poll returned 0 after terminate, finalize should mark complete.
    assert jobs_db.get_job(db_path, "j1")["exit_code"] == 0


def test_builder_idle_sleeps_when_no_jobs(tmp_env):
    """When claim returns None, the loop sleeps and retries without
    busy-looping."""
    from web import builder
    jobs_dir, db_path = tmp_env
    stop = threading.Event()
    claims = []

    def _mock_claim(*args, **kwargs):
        claims.append(time.monotonic())
        return None

    with patch("web.builder.jobs_db.claim_next_job", side_effect=_mock_claim):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={"jobs_dir": jobs_dir, "db_path": db_path,
                    "monitor_db_path": db_path.parent / "device_monitor.db",
                    "worker_id": "t", "stop_event": stop,
                    "poll_interval_seconds": 0.1},
            daemon=True,
        )
        t.start()
        time.sleep(0.35)
        stop.set()
        t.join(timeout=1)

    # Should have polled ~3 times in 0.35s with 0.1s interval, not 3500.
    assert 2 <= len(claims) <= 6
