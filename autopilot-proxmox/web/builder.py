"""Builder worker loop. Claims Ansible jobs from jobs.db and runs them.

Lifecycle:
    1. Poll claim_next_job every `poll_interval_seconds` (default 2s).
    2. When a job is claimed, spawn `ansible-playbook` as subprocess.
    3. Heartbeat every `heartbeat_seconds` (default 5s) while running.
    4. On each heartbeat, check kill_requested; terminate if set.
    5. On process exit, finalize_job with the exit code.

Design: docs/specs/2026-04-21-microservice-split-design.md §2 + §7
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from web import jobs_db, service_health

_log = logging.getLogger("web.builder")


def _worker_id() -> str:
    """Persist a uuid under /app/output/worker-id.<hostname> so compose
    restarts preserve identity for the health UI. Uses O_CREAT|O_EXCL
    to survive any (pathological) simultaneous-start race."""
    hostname = os.uname().nodename
    path = Path("/app/output") / f"worker-id.{hostname}"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        pass
    new_id = f"builder-{uuid.uuid4().hex[:8]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # Someone beat us; re-read.
        return path.read_text().strip()
    try:
        os.write(fd, new_id.encode())
    finally:
        os.close(fd)
    return new_id


def _version_sha() -> str:
    path = Path("/app/VERSION")
    return path.read_text().strip()[:7] if path.exists() else "unknown"


def _run_one_job(row: dict, *, log_path: Path, db_path: Path,
                 worker_id: str, stop_event: threading.Event,
                 heartbeat_seconds: float = 5.0) -> None:
    """Spawn, heartbeat, react to kill, finalize — defensively.

    - DB failures (transient sqlite.OperationalError) are logged and
      swallowed so the heartbeat loop keeps running.
    - If touch_heartbeat returns 0 rows updated, another writer (the
      reaper, or the /kill endpoint via a race) has taken us out of
      'running' state — terminate the subprocess and exit without
      calling finalize_job (the status is already terminal).
    - Any exit path (normal, kill, reap, exception) terminates the
      subprocess if it's still alive, so we never leak processes.
    """
    _log.info("starting job %s (type=%s) on %s",
              row["id"], row["job_type"], worker_id)
    log_file = open(log_path, "a")
    proc = None
    try:
        try:
            proc = subprocess.Popen(
                row["cmd"], stdout=log_file, stderr=subprocess.STDOUT, text=True,
            )
        except Exception:
            _log.exception("failed to spawn subprocess for job %s", row["id"])
            try:
                jobs_db.finalize_job(db_path, row["id"], exit_code=-1)
            except Exception:
                _log.exception("finalize_job failed for %s", row["id"])
            return

        reaped = False
        exit_code = None
        while True:
            exit_code = proc.poll()
            if exit_code is not None:
                break
            try:
                n = jobs_db.touch_heartbeat(db_path, row["id"])
                if n == 0:
                    _log.warning(
                        "job %s no longer in 'running' state — "
                        "reaped or finalized externally; terminating",
                        row["id"],
                    )
                    reaped = True
                    break
                current = jobs_db.get_job(db_path, row["id"])
                if current and current.get("kill_requested"):
                    _log.info("kill_requested on %s — terminating",
                              row["id"])
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            except Exception:
                _log.exception("db error during heartbeat for %s — "
                               "swallowing, will retry next tick",
                               row["id"])
            if stop_event.is_set():
                _log.info("stop_event set — terminating job %s", row["id"])
                try:
                    proc.terminate()
                except Exception:
                    pass
            time.sleep(heartbeat_seconds)

        if not reaped:
            try:
                n = jobs_db.finalize_job(db_path, row["id"], exit_code=exit_code)
                if n == 0:
                    _log.warning(
                        "finalize_job for %s found row already terminal "
                        "(likely reaped mid-run)", row["id"],
                    )
            except Exception:
                _log.exception("finalize_job failed for %s", row["id"])
            _log.info("job %s finished with exit_code=%s",
                      row["id"], exit_code)
    finally:
        # Never leak a subprocess. If we're leaving this function and
        # the proc is still alive (reap, exception, stop_event escape,
        # anything), SIGTERM it.
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        log_file.close()


def run_builder(*, jobs_dir: Path | str = "/app/jobs",
                db_path: Path | str = "/app/output/jobs.db",
                monitor_db_path: Path | str = "/app/output/device_monitor.db",
                worker_id: str | None = None,
                stop_event: threading.Event | None = None,
                poll_interval_seconds: float = 2.0,
                heartbeat_seconds: float = 5.0) -> None:
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(db_path)
    monitor_db_path = Path(monitor_db_path)

    if worker_id is None:
        worker_id = _worker_id()
    stop_event = stop_event or _install_signal_handlers()
    version = _version_sha()

    _log.info("builder %s starting (poll=%ss heartbeat=%ss)",
              worker_id, poll_interval_seconds, heartbeat_seconds)

    service_health.init(monitor_db_path)
    service_health.heartbeat(monitor_db_path,
                             service_id=worker_id, service_type="builder",
                             version_sha=version, detail="starting")

    last_service_heartbeat = 0.0
    while not stop_event.is_set():
        row = jobs_db.claim_next_job(db_path, worker_id=worker_id)
        now = time.monotonic()
        if now - last_service_heartbeat >= 10.0:
            detail = f"running {row['id']}" if row else "idle"
            service_health.heartbeat(
                monitor_db_path, service_id=worker_id,
                service_type="builder", version_sha=version, detail=detail,
            )
            last_service_heartbeat = now

        if row is None:
            if stop_event.wait(timeout=poll_interval_seconds):
                break
            continue

        log_path = jobs_dir / f"{row['id']}.log"
        _run_one_job(row, log_path=log_path, db_path=db_path,
                     worker_id=worker_id, stop_event=stop_event)
        service_health.heartbeat(
            monitor_db_path, service_id=worker_id,
            service_type="builder", version_sha=version, detail="idle",
        )

    _log.info("builder %s stopping", worker_id)


def _install_signal_handlers() -> threading.Event:
    """SIGTERM/SIGINT → set the stop event so the loop exits cleanly."""
    stop = threading.Event()
    def _handler(signum, frame):
        _log.info("caught signal %s; requesting stop", signum)
        stop.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    return stop
