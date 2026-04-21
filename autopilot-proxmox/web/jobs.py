import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path


class JobManager:
    """Manages Ansible playbook runs as subprocesses. Thread-safe."""

    def __init__(self, jobs_dir="jobs", jobs_db_path: Path | None = None):
        self.jobs_dir = jobs_dir
        self.jobs_db_path = jobs_db_path
        os.makedirs(jobs_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._active = {}
        self._index = self._load_index()
        self._on_complete: dict[str, list] = {}
        self._cleanup_orphans()

    def _cleanup_orphans(self):
        """Mark any jobs stuck as 'running' from a previous crash as failed."""
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        for entry in self._index:
            if entry["status"] == "running":
                entry["status"] = "failed"
                entry["ended"] = now
                entry["exit_code"] = -1
                changed = True
        if changed:
            self._save_index()

    def _load_index(self):
        path = os.path.join(self.jobs_dir, "index.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return []

    def _save_index(self):
        path = os.path.join(self.jobs_dir, "index.json")
        with open(path, "w") as f:
            json.dump(self._index, f, indent=2)

    def _generate_id(self):
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        rand = os.urandom(2).hex()
        return f"{date}-{rand}"

    def start(self, playbook_name, command, args=None):
        job_id = self._generate_id()
        log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
        now = datetime.now(timezone.utc).isoformat()

        entry = {
            "id": job_id,
            "playbook": playbook_name,
            "status": "running",
            "started": now,
            "ended": None,
            "exit_code": None,
            "args": args or {},
        }

        # Mirror into jobs.db (Phase 0 bridge — Task 13 will strip the
        # subprocess spawn so web becomes enqueue-only). Web claims the
        # row in-process so status transitions pending→running; the real
        # subprocess is what's actually driving the work today.
        if self.jobs_db_path is not None:
            from web import jobs_db
            jobs_db.enqueue(
                self.jobs_db_path,
                job_id=job_id,
                job_type=playbook_name,
                playbook=command[1] if len(command) > 1 else playbook_name,
                cmd=list(command),
                args=args or {},
            )
            jobs_db.claim_next_job(self.jobs_db_path, worker_id="web-inproc")

        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        with self._lock:
            self._index.append(entry)
            self._save_index()
            self._active[job_id] = {
                "process": proc,
                "log_file": log_file,
            }

        thread = threading.Thread(
            target=self._wait_for_completion,
            args=(job_id, proc, log_file),
            daemon=True,
        )
        thread.start()

        return entry

    def add_on_complete(self, job_id: str, callback) -> None:
        """Register a callback(job_dict) to run when the job finishes.

        Callbacks run in the job-runner thread. Exceptions are logged and
        swallowed — a bad callback must not poison job status.
        """
        with self._lock:
            self._on_complete.setdefault(job_id, []).append(callback)

    def _wait_for_completion(self, job_id, proc, log_file):
        proc.wait()
        log_file.close()
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            job = None
            for entry in self._index:
                if entry["id"] == job_id:
                    entry["status"] = "complete" if proc.returncode == 0 else "failed"
                    entry["ended"] = now
                    entry["exit_code"] = proc.returncode
                    job = dict(entry)
                    break
            self._save_index()
            self._active.pop(job_id, None)
            callbacks = self._on_complete.pop(job_id, [])

        if job is not None:
            for cb in callbacks:
                try:
                    cb(job)
                except Exception as e:
                    try:
                        log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
                        with open(log_path, "a") as f:
                            f.write(f"[on_complete] callback error: {e}\n")
                    except Exception:
                        pass

    def get_job(self, job_id):
        with self._lock:
            for entry in self._index:
                if entry["id"] == job_id:
                    return dict(entry)
        return None

    def list_jobs(self):
        with self._lock:
            return list(reversed(self._index))

    def get_log(self, job_id):
        log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
        if os.path.exists(log_path):
            with open(log_path) as f:
                return f.read()
        return ""

    def is_running(self, job_id):
        with self._lock:
            return job_id in self._active

    def kill(self, job_id):
        with self._lock:
            if job_id not in self._active:
                return False
            proc = self._active[job_id]["process"]
        proc.kill()
        return True

    def set_arg(self, job_id: str, key: str, value) -> None:
        """Attach arbitrary key/value metadata to a job (used by Phase B)."""
        with self._lock:
            for entry in self._index:
                if entry["id"] == job_id:
                    args = entry.get("args") or {}
                    args[key] = value
                    entry["args"] = args
                    break
            self._save_index()
