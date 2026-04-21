"""Web-facing JobManager.

After the Task 13 microservice split, this module is enqueue-only:
  - start() inserts a pending row into jobs.db and returns a dict.
  - The builder container claims and executes jobs.
  - list/get/log delegate to jobs.db + filesystem.

No subprocesses. No threads. No in-process callbacks.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path


class JobManager:
    """Thin enqueue wrapper around jobs_db. The builder owns execution."""

    def __init__(self, jobs_dir: str = "jobs", jobs_db_path: Path | None = None):
        self.jobs_dir = jobs_dir
        self.jobs_db_path = jobs_db_path
        os.makedirs(jobs_dir, exist_ok=True)

    def _generate_id(self) -> str:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        rand = os.urandom(2).hex()
        return f"{date}-{rand}"

    def start(self, playbook_name, command, args=None):
        """Enqueue a job. The builder container picks it up and runs it.

        Returns a dict shaped like the legacy return so call sites that
        read `entry["id"]` keep working.
        """
        if self.jobs_db_path is None:
            raise RuntimeError(
                "JobManager.start requires jobs_db_path — the builder "
                "split made jobs.db the only queue."
            )
        job_id = self._generate_id()
        # Pre-touch the log file so /jobs/<id> doesn't 500 trying to tail
        # a nonexistent file before the builder has written anything.
        log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
        open(log_path, "a").close()

        from web import jobs_db
        row = jobs_db.enqueue(
            self.jobs_db_path,
            job_id=job_id,
            job_type=playbook_name,
            playbook=command[1] if len(command) > 1 else playbook_name,
            cmd=list(command),
            args=args or {},
        )

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
        from web import jobs_db
        return jobs_db.list_jobs(self.jobs_db_path)

    def get_job(self, job_id):
        from web import jobs_db
        return jobs_db.get_job(self.jobs_db_path, job_id)

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

        Writes through to jobs.db's args_json column so the builder and
        downstream readers see it.
        """
        if self.jobs_db_path is None:
            return
        import json
        import sqlite3
        # Inline update to avoid plumbing a new jobs_db helper for a
        # single call site (sequence tracking).
        conn = sqlite3.connect(self.jobs_db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute(
                "SELECT args_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return
            args = json.loads(row[0]) if row[0] else {}
            args[key] = value
            conn.execute(
                "UPDATE jobs SET args_json=? WHERE id=?",
                (json.dumps(args), job_id),
            )
        finally:
            conn.close()

    def add_on_complete(self, job_id: str, callback) -> None:
        """Deprecated: with the builder split, callbacks ran in the
        subprocess waiter thread which no longer exists. Kept as a
        no-op so any lingering callers don't crash, but the callback
        will never fire. TODO: if callbacks are still needed, plumb
        them through jobs_db as a post-finalize hook."""
        logging.getLogger("web.jobs").warning(
            "JobManager.add_on_complete is a no-op after the builder split"
        )
