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
               monitor_db_path: Path, jobs_db_path: Path) -> None:
    """Placeholder — filled in by Task 15 (sweep/reaper/keytab/heartbeat tickers)."""
    while not stop_event.is_set():
        stop_event.wait(timeout=1)
