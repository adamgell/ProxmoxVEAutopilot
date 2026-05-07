import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_env(pg_conn):
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d) / "jobs"
        jobs_dir.mkdir()
        output_dir = Path(d) / "output"
        output_dir.mkdir()
        yield jobs_dir, output_dir


def test_builder_runs_one_job_and_exits_on_stop(tmp_env):
    """Happy path: enqueue a job, builder claims + spawns + finalizes."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["echo", "ok"],
        args={},
    )

    stop = threading.Event()

    def _fake_run(row, log_path, db_path, worker_id, stop_event):
        jobs_db.finalize_job(row["id"], exit_code=0)

    with patch("web.builder._run_one_job", side_effect=_fake_run):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={
                "jobs_dir": jobs_dir,
                "db_path": output_dir / "jobs.db",
                "monitor_db_path": output_dir / "device_monitor.db",
                "worker_id": "test-worker",
                "stop_event": stop,
                "poll_interval_seconds": 0.1,
            },
            daemon=True,
        )
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

    assert jobs_db.get_job("j1")["status"] == "complete"


def test_builder_run_one_job_kills_on_kill_requested(tmp_env):
    """With kill_requested=1, the heartbeat tick should terminate the subprocess."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "30"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="test-worker")

    proc = MagicMock()
    proc.poll.side_effect = [None, 0, 0]
    proc.terminate = MagicMock()

    row = jobs_db.get_job("j1")
    jobs_db.request_kill("j1")

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()

    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="test-worker",
            stop_event=stop,
            heartbeat_seconds=0.05,
        )

    proc.terminate.assert_called_once()
    assert jobs_db.get_job("j1")["exit_code"] == 0


def test_builder_idle_sleeps_when_no_jobs(tmp_env):
    """When claim returns None, the loop sleeps and retries without busy-looping."""
    from web import builder

    jobs_dir, output_dir = tmp_env
    stop = threading.Event()
    claims = []

    def _mock_claim(*args, **kwargs):
        claims.append(time.monotonic())
        return None

    with patch("web.builder.jobs_db.claim_next_job", side_effect=_mock_claim):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={
                "jobs_dir": jobs_dir,
                "db_path": output_dir / "jobs.db",
                "monitor_db_path": output_dir / "device_monitor.db",
                "worker_id": "t",
                "stop_event": stop,
                "poll_interval_seconds": 0.1,
            },
            daemon=True,
        )
        t.start()
        time.sleep(0.35)
        stop.set()
        t.join(timeout=1)

    assert 2 <= len(claims) <= 6


def test_builder_run_one_job_exits_cleanly_on_reap(tmp_env, pg_conn):
    """A reaped row terminates the subprocess and stays orphaned."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "30"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="w")

    proc = MagicMock()
    proc.poll.return_value = None
    proc.terminate = MagicMock()

    row = jobs_db.get_job("j1")
    pg_conn.execute("UPDATE jobs SET status='orphaned' WHERE id=%s", ("j1",))
    pg_conn.commit()

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()
    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="w",
            stop_event=stop,
            heartbeat_seconds=0.01,
        )
    assert proc.terminate.called
    assert jobs_db.get_job("j1")["status"] == "orphaned"


def test_builder_swallows_transient_db_errors(tmp_env):
    """Transient heartbeat DB errors are logged and retried."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "30"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="w")

    proc = MagicMock()
    proc.poll.side_effect = [None, 0, 0]

    row = jobs_db.get_job("j1")
    call_count = {"n": 0}
    real_touch = jobs_db.touch_heartbeat

    def flaky_touch(jid, worker_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("database is locked")
        return real_touch(jid, worker_id)

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()
    with patch("web.builder.subprocess.Popen", return_value=proc), patch(
        "web.builder.jobs_db.touch_heartbeat", side_effect=flaky_touch
    ):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="w",
            stop_event=stop,
            heartbeat_seconds=0.01,
        )
    assert jobs_db.get_job("j1")["status"] == "complete"
