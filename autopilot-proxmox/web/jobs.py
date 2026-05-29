"""Web-facing JobManager.

After the Task 13 microservice split, this module is enqueue-only:
  - start() inserts a pending row into Postgres and returns a dict.
  - The builder container claims and executes jobs.
  - list/get/log delegate to the queue + filesystem.

No subprocesses. No threads. No in-process callbacks.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from psycopg.errors import UniqueViolation

from web import jobs_pg as jobs_db


class JobManager:
    """Thin enqueue wrapper around the Postgres job queue."""

    def __init__(self, jobs_dir: str = "jobs", jobs_db_path: Path | None = None):
        self.jobs_dir = jobs_dir
        self.jobs_db_path = jobs_db_path
        os.makedirs(jobs_dir, exist_ok=True)

    def _generate_id(self) -> str:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        rand = os.urandom(4).hex()
        return f"{date}-{rand}"

    def start(self, playbook_name, command, args=None):
        """Enqueue a job. The builder container picks it up and runs it.

        Returns a dict shaped like the legacy return so call sites that
        read `entry["id"]` keep working.
        """
        row = None
        job_id = ""
        for _ in range(8):
            job_id = self._generate_id()
            # Pre-touch the log file so /jobs/<id> doesn't 500 trying to tail
            # a nonexistent file before the builder has written anything.
            log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
            open(log_path, "a").close()
            try:
                row = jobs_db.enqueue(
                    job_id=job_id,
                    job_type=playbook_name,
                    playbook=command[1] if len(command) > 1 else playbook_name,
                    cmd=list(command),
                    args=args or {},
                )
                break
            except UniqueViolation:
                logging.getLogger("web.jobs").warning(
                    "generated duplicate job id %s; retrying",
                    job_id,
                )
        if row is None:
            raise RuntimeError("failed to allocate a unique job id")

        return {
            "id": job_id,
            "playbook": playbook_name,
            "status": "pending",
            "started": row["created_at"],
            "ended": None,
            "exit_code": None,
            "args": args or {},
        }

    def list_jobs(self):
        return jobs_db.list_jobs()

    def get_job(self, job_id):
        return jobs_db.get_job(job_id)

    def get_log(self, job_id):
        path = os.path.join(self.jobs_dir, f"{job_id}.log")
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def is_running(self, job_id) -> bool:
        """Best-effort liveness check used by the log-tail websocket.

        Post-split we can't observe the subprocess directly — read the
        row status instead. Pending is treated as 'still active' so the
        tailer waits rather than closing before the builder claims.
        """
        job = self.get_job(job_id)
        if job is None:
            return False
        return job.get("status") in ("pending", "running")

    def set_arg(self, job_id: str, key: str, value) -> None:
        """Attach arbitrary key/value metadata to a job.

        Writes through to the Postgres jobs args_json column so the builder and
        downstream readers see it.
        """
        job = self.get_job(job_id)
        if job is None:
            return
        args = dict(job.get("args") or {})
        args[key] = value
        jobs_db.update_job_args(job_id, args)

    def add_on_complete(self, job_id: str, callback) -> None:
        """Deprecated: with the builder split, callbacks ran in the
        subprocess waiter thread which no longer exists. Kept as a
        no-op so any lingering callers don't crash, but the callback
        will never fire. TODO: if callbacks are still needed, plumb
        them through jobs_db as a post-finalize hook."""
        logging.getLogger("web.jobs").warning(
            "JobManager.add_on_complete is a no-op after the builder split"
        )
