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
             patch("web.monitor_main._do_keytab_tick", return_value=None), \
             patch("web.monitor_main._do_cloudosd_readiness_tick", return_value={"watched": 0}), \
             patch("web.monitor_main._do_cloudosd_domain_join_tick", return_value={"candidates": 0}), \
             patch("web.monitor_main._do_screenshot_capture_tick", return_value={"captured": 0}):
            t = threading.Thread(
                target=monitor_main._run_loops,
                kwargs={"stop_event": stop,
                        "monitor_db_path": monitor_db,
                        "reaper_interval_seconds": 0.1,
                        "heartbeat_interval_seconds": 0.1,
                        "sweep_interval_seconds": 10,
                        "keytab_interval_seconds": 10,
                        "readiness_interval_seconds": 10,
                        "screenshot_interval_seconds": 10,
                        "domain_join_interval_seconds": 10},
                daemon=True,
            )
            t.start()
            time.sleep(0.35)
            stop.set()
            t.join(timeout=2)
        assert jobs_pg.get_job("stale")["status"] == "orphaned"


def _run_loops_with_reconcile(pg_conn, *, interval, auto_apply):
    """Spin _run_loops briefly with every tick but lab-reconcile stubbed out,
    and return the MagicMock standing in for the reconcile tick."""
    from web import monitor_main, service_health_pg as service_health
    import tempfile, threading, time
    from unittest.mock import patch, MagicMock
    from pathlib import Path

    service_health.init(pg_conn)
    reconcile = MagicMock(return_value={"counts": {"fixing": 1}, "applied_count": 0})
    with tempfile.TemporaryDirectory() as d:
        monitor_db = Path(d) / "device_monitor.db"
        stop = threading.Event()
        with patch("web.monitor_main._do_sweep_tick", return_value=None), \
             patch("web.monitor_main._do_keytab_tick", return_value=None), \
             patch("web.monitor_main._do_cloudosd_readiness_tick", return_value={"watched": 0}), \
             patch("web.monitor_main._do_cloudosd_domain_join_tick", return_value={"candidates": 0}), \
             patch("web.monitor_main._do_screenshot_capture_tick", return_value={"captured": 0}), \
             patch("web.monitor_main._do_lab_reconcile_tick", reconcile):
            t = threading.Thread(
                target=monitor_main._run_loops,
                kwargs={"stop_event": stop,
                        "monitor_db_path": monitor_db,
                        "reaper_interval_seconds": 10,
                        "heartbeat_interval_seconds": 0.1,
                        "sweep_interval_seconds": 10,
                        "keytab_interval_seconds": 10,
                        "readiness_interval_seconds": 10,
                        "screenshot_interval_seconds": 10,
                        "domain_join_interval_seconds": 0,
                        "lab_reconcile_interval_seconds": interval,
                        "lab_reconcile_auto_apply": auto_apply},
                daemon=True,
            )
            t.start()
            time.sleep(0.35)
            stop.set()
            t.join(timeout=2)
    return reconcile


def test_run_loops_runs_lab_reconcile_when_enabled(pg_conn):
    reconcile = _run_loops_with_reconcile(pg_conn, interval=0.1, auto_apply=False)
    assert reconcile.called
    reconcile.assert_called_with(auto_apply=False)


def test_run_loops_passes_auto_apply_flag_through(pg_conn):
    reconcile = _run_loops_with_reconcile(pg_conn, interval=0.1, auto_apply=True)
    reconcile.assert_called_with(auto_apply=True)


def test_run_loops_skips_lab_reconcile_when_interval_zero(pg_conn):
    """Default cadence (0) keeps the fleet reconcile off: no Proxmox mutation
    happens unless an operator explicitly enables it."""
    reconcile = _run_loops_with_reconcile(pg_conn, interval=0, auto_apply=False)
    assert not reconcile.called


def test_lab_reconcile_tick_skips_on_utm(monkeypatch):
    """On a UTM backend there is no Proxmox SDN fleet, so the tick returns
    before building inventory or opening a DB connection."""
    from web import app as app_module, monitor_main, managed_labs_reconciler

    monkeypatch.setattr(app_module, "_load_vars", lambda: {"hypervisor_type": "utm"})
    called = []
    monkeypatch.setattr(
        managed_labs_reconciler,
        "reconcile_all_labs",
        lambda *a, **k: called.append(k) or {},
    )

    result = monitor_main._do_lab_reconcile_tick(auto_apply=True)

    assert result == {"skipped": "utm"}
    assert called == []


def _run_loops_with_domain_join(pg_conn, *, interval):
    """Spin _run_loops briefly with every tick stubbed and return the MagicMock
    standing in for the CloudOSD domain-join tick."""
    from web import monitor_main, service_health_pg as service_health
    import tempfile, threading, time
    from unittest.mock import patch, MagicMock
    from pathlib import Path

    service_health.init(pg_conn)
    domain_join = MagicMock(return_value={"candidates": 1, "joined": 1})
    with tempfile.TemporaryDirectory() as d:
        monitor_db = Path(d) / "device_monitor.db"
        stop = threading.Event()
        with patch("web.monitor_main._do_sweep_tick", return_value=None), \
             patch("web.monitor_main._do_keytab_tick", return_value=None), \
             patch("web.monitor_main._do_cloudosd_readiness_tick", return_value={"watched": 0}), \
             patch("web.monitor_main._do_screenshot_capture_tick", return_value={"captured": 0}), \
             patch("web.monitor_main._do_cloudosd_domain_join_tick", domain_join):
            t = threading.Thread(
                target=monitor_main._run_loops,
                kwargs={"stop_event": stop,
                        "monitor_db_path": monitor_db,
                        "reaper_interval_seconds": 10,
                        "heartbeat_interval_seconds": 0.1,
                        "sweep_interval_seconds": 10,
                        "keytab_interval_seconds": 10,
                        "readiness_interval_seconds": 10,
                        "screenshot_interval_seconds": 10,
                        "lab_reconcile_interval_seconds": 0,
                        "domain_join_interval_seconds": interval},
                daemon=True,
            )
            t.start()
            time.sleep(0.35)
            stop.set()
            t.join(timeout=2)
    return domain_join


def test_run_loops_runs_domain_join_when_enabled(pg_conn):
    domain_join = _run_loops_with_domain_join(pg_conn, interval=0.1)
    assert domain_join.called


def test_run_loops_skips_domain_join_when_interval_zero(pg_conn):
    domain_join = _run_loops_with_domain_join(pg_conn, interval=0)
    assert not domain_join.called


def test_domain_join_tick_skips_on_utm(monkeypatch):
    """On a UTM backend there is no Proxmox guest-exec transport, so the tick
    returns before resolving credentials or opening a DB connection."""
    from web import app as app_module, monitor_main, cloudosd_domain_join

    monkeypatch.setattr(app_module, "_load_vars", lambda: {"hypervisor_type": "utm"})
    called = []
    monkeypatch.setattr(
        cloudosd_domain_join, "run_pending_joins",
        lambda *a, **k: called.append(k) or {},
    )

    result = monitor_main._do_cloudosd_domain_join_tick()

    assert result == {"skipped": "utm"}
    assert called == []


def test_screenshot_capture_tick_collects_running_vms(monkeypatch, tmp_path):
    from web import app as app_module, monitor_main

    monkeypatch.setattr(app_module, "SCREENSHOT_STORE_DIR", tmp_path / "screenshots")
    monkeypatch.setattr(
        app_module,
        "_running_vmids_for_screenshot_capture",
        lambda: [105, 116],
    )
    monkeypatch.setattr(
        app_module,
        "_capture_vm_screenshot_png",
        lambda vmid: b"\x89PNG\r\n\x1a\n" + str(vmid).encode(),
    )

    result = monitor_main._do_screenshot_capture_tick()

    assert result == {"enabled": True, "running": 2, "captured": 2, "failed": 0}
    assert app_module._latest_vm_screenshot(105)["source"] == "collector"
    assert app_module._latest_vm_screenshot(116)["source"] == "collector"


def test_run_monitor_passes_monitor_db_path_to_loops(monkeypatch):
    from web import monitor_main

    stop = threading.Event()
    monkeypatch.setattr(monitor_main, "_install_signal_handlers", lambda: stop)
    with tempfile.TemporaryDirectory() as d:
        lock_path = Path(d) / "monitor.lock"
        monitor_db = Path(d) / "device_monitor.db"
        with patch("web.monitor_main._run_loops") as loops:
            monitor_main.run_monitor(lock_path=lock_path, monitor_db_path=monitor_db)
        loops.assert_called_once()
        assert loops.call_args.kwargs["monitor_db_path"] == monitor_db
