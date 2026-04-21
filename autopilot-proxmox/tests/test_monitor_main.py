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


def test_run_loops_runs_reaper_on_cadence(monkeypatch):
    """_run_loops calls reap_orphans periodically."""
    from web import monitor_main
    import tempfile, threading, time
    from unittest.mock import patch
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        monitor_db = Path(d) / "device_monitor.db"
        jobs_db_path = Path(d) / "jobs.db"
        from web import jobs_db, service_health
        jobs_db.init(jobs_db_path)
        service_health.init(monitor_db)

        reaps = []
        def _mock_reap(*a, **kw):
            reaps.append(time.monotonic())
            return 0

        stop = threading.Event()
        with patch("web.monitor_main._do_sweep_tick", return_value=None), \
             patch("web.monitor_main._do_keytab_tick", return_value=None), \
             patch("web.monitor_main.jobs_db.reap_orphans", side_effect=_mock_reap):
            t = threading.Thread(
                target=monitor_main._run_loops,
                kwargs={"stop_event": stop,
                        "monitor_db_path": monitor_db,
                        "jobs_db_path": jobs_db_path,
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
        assert 2 <= len(reaps) <= 6
