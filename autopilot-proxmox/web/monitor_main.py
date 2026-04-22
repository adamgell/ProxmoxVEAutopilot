"""Monitor singleton: sweep loop, keytab refresher, orphan reaper.

Hard singleton via fcntl.flock. Second instance exits 0 (not a
failure — compose will tolerate scaling but only one wins).

Design: docs/specs/2026-04-21-microservice-split-design.md §3, §5
"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from web import jobs_db, service_health

_log = logging.getLogger("web.monitor_main")

# Thresholds / cadences (seconds). Task 15 will use these.
_SWEEP_INTERVAL_DEFAULT = 900       # 15 minutes, same as today
_REAPER_INTERVAL = 30               # poll for orphans twice a minute
_HEARTBEAT_INTERVAL = 10            # service_health cadence
_KEYTAB_CHECK_INTERVAL = 3600       # keytab health checked hourly


def _acquire_singleton_lock(path: Path) -> int | None:
    """Return an open FD holding an exclusive lock, or None if another
    process already holds it. Caller is responsible for closing the FD
    (or letting process exit do it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def run_monitor(*, lock_path: Path | str = "/app/output/monitor.lock",
                monitor_db_path: Path | str = "/app/output/device_monitor.db",
                jobs_db_path: Path | str = "/app/output/jobs.db",
                stop_event: threading.Event | None = None) -> None:
    lock_path = Path(lock_path)
    monitor_db_path = Path(monitor_db_path)
    jobs_db_path = Path(jobs_db_path)

    fd = _acquire_singleton_lock(lock_path)
    if fd is None:
        _log.warning(
            "monitor already running elsewhere (lock held on %s) — exiting 0",
            lock_path,
        )
        sys.exit(0)

    stop_event = stop_event or _install_signal_handlers()

    _log.info("monitor singleton acquired lock on %s", lock_path)
    try:
        _run_loops(stop_event=stop_event,
                   monitor_db_path=monitor_db_path,
                   jobs_db_path=jobs_db_path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _install_signal_handlers() -> threading.Event:
    stop = threading.Event()
    def _h(signum, frame):
        _log.info("caught signal %s; requesting stop", signum)
        stop.set()
    signal.signal(signal.SIGTERM, _h)
    signal.signal(signal.SIGINT, _h)
    return stop


def _run_loops(*, stop_event: threading.Event,
               monitor_db_path: Path, jobs_db_path: Path,
               sweep_interval_seconds: float = _SWEEP_INTERVAL_DEFAULT,
               reaper_interval_seconds: float = _REAPER_INTERVAL,
               heartbeat_interval_seconds: float = _HEARTBEAT_INTERVAL,
               keytab_interval_seconds: float = _KEYTAB_CHECK_INTERVAL) -> None:
    """The heart of the monitor. Four tickers, one process.

    Cadences (all independently overridable for tests):
      - heartbeat: every ``heartbeat_interval_seconds`` (default 10s)
      - reaper:    every ``reaper_interval_seconds``    (default 30s)
      - sweep:     every ``sweep_interval_seconds``     (default 900s)
      - keytab:    every ``keytab_interval_seconds``    (default 3600s)

    Each tick is wrapped in ``try/except`` so a transient failure in
    one (e.g., Proxmox API hiccup) never stops the others from firing.
    """
    service_health.init(monitor_db_path)
    version = _version_sha()

    last_sweep = 0.0
    last_reap = 0.0
    last_hb = 0.0
    last_keytab = 0.0

    _log.info(
        "monitor loops starting (sweep=%ss, reaper=%ss, keytab=%ss, heartbeat=%ss)",
        sweep_interval_seconds, reaper_interval_seconds,
        keytab_interval_seconds, heartbeat_interval_seconds,
    )

    while not stop_event.is_set():
        now = time.monotonic()

        if now - last_hb >= heartbeat_interval_seconds:
            try:
                service_health.heartbeat(
                    monitor_db_path, service_id="monitor",
                    service_type="monitor", version_sha=version,
                    detail="running",
                )
                service_health.prune_dead_workers(monitor_db_path)
            except Exception:
                _log.exception("heartbeat failed")
            last_hb = now

        if now - last_reap >= reaper_interval_seconds:
            try:
                n = jobs_db.reap_orphans(jobs_db_path)
                if n:
                    _log.warning("reaped %d orphaned jobs", n)
            except Exception:
                _log.exception("reaper failed")
            last_reap = now

        if now - last_sweep >= sweep_interval_seconds:
            try:
                _do_sweep_tick(monitor_db_path)
            except Exception:
                _log.exception("sweep tick failed")
            last_sweep = now

        if now - last_keytab >= keytab_interval_seconds:
            try:
                _do_keytab_tick(monitor_db_path)
            except Exception:
                _log.exception("keytab tick failed")
            last_keytab = now

        # Short wake interval so cadence rounding stays tight (a 0.1s
        # reaper_interval_seconds in tests needs sub-second wake-ups;
        # production tickers are all >=10s so a 1s tick is fine).
        if heartbeat_interval_seconds < 2:
            wake_seconds = min(1.0, heartbeat_interval_seconds / 2)
        else:
            wake_seconds = 1.0
        stop_event.wait(timeout=wake_seconds)

    _log.info("monitor loops stopping")


def _do_sweep_tick(monitor_db_path: Path) -> None:
    """One iteration of the device-monitor sweep.

    Mirrors the body of :func:`web.app._device_monitor_loop`:
      1. Read settings via ``device_history_db.get_settings`` (same
         source the /monitoring/settings UI writes to).
      2. Skip when ``settings.enabled`` is false.
      3. Build a live ``MonitorContext`` via ``web.app._build_live_monitor_context``
         (PVE API + AD + Graph wiring).
      4. Compute ``extra_in_scope_vmids`` from the sequences DB.
      5. Call ``device_monitor.sweep(ctx, extra_in_scope_vmids=extra)``.
    """
    from web import device_history_db, device_monitor
    try:
        settings = device_history_db.get_settings(monitor_db_path)
    except Exception:
        _log.exception("sweep: could not read settings")
        return
    if not getattr(settings, "enabled", True):
        return

    # Live wiring lives in web.app; import lazily so test-only calls
    # to monitor_main._run_loops don't drag FastAPI in.
    from web import app as web_app
    try:
        ctx = web_app._build_live_monitor_context()
        extra = web_app._vm_provisioning_vmids()
    except Exception:
        _log.exception("sweep: could not build live context")
        return
    device_monitor.sweep(ctx, extra_in_scope_vmids=extra)


def _do_keytab_tick(monitor_db_path: Path) -> None:
    """One iteration of the keytab probe/refresh logic.

    Delegates to ``web.app._run_keytab_checks`` which handles probe,
    record, and daily refresh against the configured gMSA. The helper
    already swallows and records its own errors — we still guard with
    try/except so an unexpected failure can't kill the loop thread.
    """
    from web import app as web_app
    try:
        web_app._run_keytab_checks()
    except Exception:
        _log.exception("keytab probe/refresh failed")


def _version_sha() -> str:
    """Best-effort running version SHA, matches web's footer build."""
    for candidate in (Path("/app/VERSION"),
                      Path(__file__).resolve().parent.parent / "VERSION"):
        try:
            if candidate.exists():
                return candidate.read_text().strip().splitlines()[0][:7] or "unknown"
        except Exception:
            continue
    return "unknown"
