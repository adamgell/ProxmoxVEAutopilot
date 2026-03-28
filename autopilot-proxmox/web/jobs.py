import asyncio
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone


class JobManager:
    """Manages Ansible playbook runs as subprocesses."""

    def __init__(self, jobs_dir="jobs"):
        self.jobs_dir = jobs_dir
        os.makedirs(jobs_dir, exist_ok=True)
        self._active = {}
        self._index = self._load_index()

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
        self._index.append(entry)
        self._save_index()

        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

        self._active[job_id] = {
            "process": proc,
            "log_file": log_file,
            "subscribers": [],
        }

        thread = threading.Thread(
            target=self._wait_for_completion,
            args=(job_id, proc, log_file),
            daemon=True,
        )
        thread.start()

        return entry

    def _wait_for_completion(self, job_id, proc, log_file):
        proc.wait()
        log_file.close()
        now = datetime.now(timezone.utc).isoformat()

        for entry in self._index:
            if entry["id"] == job_id:
                entry["status"] = "complete" if proc.returncode == 0 else "failed"
                entry["ended"] = now
                entry["exit_code"] = proc.returncode
                break
        self._save_index()

        if job_id in self._active:
            del self._active[job_id]

    def get_job(self, job_id):
        for entry in self._index:
            if entry["id"] == job_id:
                return entry
        return None

    def list_jobs(self):
        return list(reversed(self._index))

    def get_log(self, job_id):
        log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
        if os.path.exists(log_path):
            with open(log_path) as f:
                return f.read()
        return ""

    def is_running(self, job_id):
        return job_id in self._active

    def kill(self, job_id):
        if job_id not in self._active:
            return False
        proc = self._active[job_id]["process"]
        proc.kill()
        return True
