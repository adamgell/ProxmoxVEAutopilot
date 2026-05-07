import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch


def test_singleton_guard_second_instance_exits_zero():
    from web import monitor_main
    with tempfile.TemporaryDirectory() as d:
        lock_path = Path(d) / "monitor.lock"
        import fcntl
        holder = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            acquired = monitor_main._acquire_singleton_lock(lock_path)
            assert acquired is None
        finally:
            os.close(holder)


def test_singleton_guard_first_instance_gets_lock():
    from web import monitor_main
    with tempfile.TemporaryDirectory() as d:
        lock_path = Path(d) / "monitor.lock"
        fd = monitor_main._acquire_singleton_lock(lock_path)
        assert fd is not None
        import os as _os
        _os.close(fd)


def test_run_loops_runs_reaper_on_cadence(monkeypatch, pg_conn):
    """_run_loops reaps stale Postgres jobs periodically."""
    from web import monitor_main
    import tempfile, threading, time
    from unittest.mock import patch
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        monitor_db = Path(d) / "device_monitor.db"
        from web import jobs_pg, service_health_pg as service_health

        jobs_pg.enqueue(
            job_id="stale",
            job_type="hash_capture",
            playbook="capture.yml",
            cmd=["true"],
            args={},
        )
        jobs_pg.claim_next_job(worker_id="builder-1")
        pg_conn.execute(
            "UPDATE jobs SET last_heartbeat = now() - interval '10 minutes' "
            "WHERE id = %s",
            ("stale",),
        )
        pg_conn.commit()
        service_health.init(pg_conn)

        stop = threading.Event()
        with patch("web.monitor_main._do_sweep_tick", return_value=None), \
             patch("web.monitor_main._do_keytab_tick", return_value=None):
            t = threading.Thread(
                target=monitor_main._run_loops,
                kwargs={"stop_event": stop,
                        "monitor_db_path": monitor_db,
                        "reaper_interval_seconds": 0.1,
                        "heartbeat_interval_seconds": 0.1,
                        "sweep_interval_seconds": 10,
                        "keytab_interval_seconds": 10},
                daemon=True,
            )
            t.start()
            time.sleep(0.35)
            stop.set()
            t.join(timeout=2)
        assert jobs_pg.get_job("stale")["status"] == "orphaned"
