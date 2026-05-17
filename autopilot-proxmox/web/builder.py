"""Builder worker loop. Claims Ansible jobs from Postgres and runs them.

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
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from web import jobs_pg as jobs_db
from web import service_health_pg as service_health
from web.paths import JOBS_DIR as _JOBS_DIR, OUTPUT_DIR as _OUTPUT_DIR, REPO_ROOT

_log = logging.getLogger("web.builder")

_OSDEPLOY_VMID_PATTERNS = [
    re.compile(r"Clone template to new VM\s+(\d+)", re.IGNORECASE),
    re.compile(r"\bVMID[:=]\s*(\d+)\b", re.IGNORECASE),
]
_SECRET_REDACTIONS = [
    (
        re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE),
        r"\1[redacted]",
    ),
    (
        re.compile(
            r"((?:token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+",
            re.IGNORECASE,
        ),
        r"\1[redacted]",
    ),
]


def _worker_id(output_dir: Path | None = None) -> str:
    """Persist a uuid under <output_dir>/worker-id.<hostname> so compose
    restarts preserve identity for the health UI. Uses O_CREAT|O_EXCL
    to survive any (pathological) simultaneous-start race.

    `output_dir` defaults to ``OUTPUT_DIR`` (repo-relative on macOS,
    ``/app/output`` inside Docker) so callers that omit the argument
    work correctly in both environments.
    """
    hostname = os.uname().nodename
    base = Path(output_dir) if output_dir is not None else _OUTPUT_DIR
    path = base / f"worker-id.{hostname}"
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
    # REPO_ROOT/VERSION works for both Docker (/app/VERSION) and native macOS.
    try:
        candidate = REPO_ROOT / "VERSION"
        if candidate.exists():
            return candidate.read_text().strip()[:7] or "unknown"
    except Exception:
        pass
    return "unknown"


def _redact_log_text(text: str) -> str:
    redacted = text.replace("\x00", "")
    for pattern, replacement in _SECRET_REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _read_log_tail(path: Path, *, max_bytes: int = 8192, max_chars: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    except Exception:
        _log.exception("failed to read job log tail from %s", path)
        return ""
    text = data[-max_bytes:].decode("utf-8", errors="replace").strip()
    return _redact_log_text(text)[-max_chars:]


def _extract_vmid_from_log(text: str) -> int | None:
    matches: list[int] = []
    for pattern in _OSDEPLOY_VMID_PATTERNS:
        for match in pattern.finditer(text or ""):
            try:
                matches.append(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return matches[-1] if matches else None


def _extract_failure_message(text: str, *, job_id: str, exit_code: int | None) -> str:
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            "fatal:" in lowered
            or "failed!" in lowered
            or "http error" in lowered
            or '"msg"' in lowered
        ):
            return stripped[-1000:]
    return f"OSDeploy provision job {job_id} failed with exit code {exit_code}"


def _finalize_related_deployment(row: dict, *, exit_code: int | None, log_path: Path) -> bool:
    if exit_code == 0 or row.get("job_type") != "provision_osdeploy":
        return False
    args = row.get("args") or {}
    run_id = args.get("osdeploy_run_id")
    if not run_id:
        return False

    log_tail = _read_log_tail(log_path)
    vmid = _extract_vmid_from_log(log_tail)
    message = _extract_failure_message(log_tail, job_id=row["id"], exit_code=exit_code)
    try:
        from web import db_pg, osdeploy_pg

        with db_pg.connection() as conn:
            run = osdeploy_pg.mark_failed_from_job(
                conn,
                run_id=run_id,
                job_id=row["id"],
                exit_code=exit_code,
                message=message,
                vmid=vmid,
                log_tail=log_tail,
            )
        if run:
            _log.warning(
                "marked OSDeploy run %s failed from job %s (vmid=%s exit_code=%s)",
                run_id,
                row["id"],
                vmid,
                exit_code,
            )
            return True
    except Exception:
        _log.exception("failed to finalize related OSDeploy run for job %s", row["id"])
    return False


def reconcile_failed_related_deployments(
    *,
    jobs_dir: Path | str = _JOBS_DIR,
    limit: int = 500,
) -> int:
    jobs_dir = Path(jobs_dir)
    reconciled = 0
    try:
        rows = jobs_db.list_jobs(limit=limit)
    except Exception:
        _log.exception("failed to list jobs for related deployment reconciliation")
        return 0
    for row in rows:
        if row.get("job_type") != "provision_osdeploy":
            continue
        if row.get("status") not in {"failed", "orphaned"}:
            continue
        log_path = jobs_dir / f"{row['id']}.log"
        if _finalize_related_deployment(
            row,
            exit_code=row.get("exit_code") if row.get("exit_code") is not None else -1,
            log_path=log_path,
        ):
            reconciled += 1
    return reconciled


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
                jobs_db.finalize_job(row["id"], exit_code=-1)
                _finalize_related_deployment(row, exit_code=-1, log_path=log_path)
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
                n = jobs_db.touch_heartbeat(row["id"], worker_id)
                if n == 0:
                    _log.warning(
                        "job %s no longer in 'running' state — "
                        "reaped or finalized externally; terminating",
                        row["id"],
                    )
                    reaped = True
                    break
                current = jobs_db.get_job(row["id"])
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
                n = jobs_db.finalize_job(row["id"], exit_code=exit_code)
                if n == 0:
                    _log.warning(
                        "finalize_job for %s found row already terminal "
                        "(likely reaped mid-run)", row["id"],
                    )
                _finalize_related_deployment(row, exit_code=exit_code, log_path=log_path)
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


def run_builder(*, jobs_dir: Path | str = _JOBS_DIR,
                db_path: Path | str = _OUTPUT_DIR / "jobs.db",
                monitor_db_path: Path | str = _OUTPUT_DIR / "device_monitor.db",
                worker_id: str | None = None,
                stop_event: threading.Event | None = None,
                poll_interval_seconds: float = 2.0,
                heartbeat_seconds: float = 5.0) -> None:
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(db_path)
    monitor_db_path = Path(monitor_db_path)

    if worker_id is None:
        worker_id = _worker_id(output_dir=db_path.parent)
    stop_event = stop_event or _install_signal_handlers()
    version = _version_sha()

    _log.info("builder %s starting (poll=%ss heartbeat=%ss)",
              worker_id, poll_interval_seconds, heartbeat_seconds)

    service_health.init()
    service_health.heartbeat(
        service_id=worker_id, service_type="builder",
        version_sha=version, detail="starting",
    )
    reconciled = reconcile_failed_related_deployments(jobs_dir=jobs_dir)
    if reconciled:
        _log.warning("reconciled %s failed related deployment job(s)", reconciled)

    last_service_heartbeat = 0.0
    while not stop_event.is_set():
        row = jobs_db.claim_next_job(worker_id=worker_id)
        now = time.monotonic()
        if now - last_service_heartbeat >= 10.0:
            detail = f"running {row['id']}" if row else "idle"
            service_health.heartbeat(
                service_id=worker_id,
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
            service_id=worker_id,
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
