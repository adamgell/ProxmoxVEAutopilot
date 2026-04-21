"""SQLite-backed job queue + per-type concurrency caps.

Design: docs/specs/2026-04-21-microservice-split-design.md §2

Layout mirrors web/sequences_db.py — module-level SCHEMA string,
init() via executescript, context-managed connections.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    job_type       TEXT NOT NULL,
    playbook       TEXT NOT NULL,
    cmd_json       TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    status         TEXT NOT NULL,
    worker_id      TEXT,
    kill_requested INTEGER NOT NULL DEFAULT 0,
    exit_code      INTEGER,
    created_at     TEXT NOT NULL,
    claimed_at     TEXT,
    last_heartbeat TEXT,
    ended_at       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_type_limits (
    job_type       TEXT PRIMARY KEY,
    max_concurrent INTEGER NOT NULL
);
"""


_DEFAULT_LIMITS = [
    ("build_template", 1),
    ("provision_clone", 3),
    ("capture_hash", 5),
    ("hash_upload", 5),
    ("retry_inject_hash", 3),
]


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL mode so readers (web tailing status) don't block the
    # builder's claim/update writes. Set every connection — it's
    # persisted in the file header but setting it is cheap and
    # defensive against tools that reset it.
    conn.execute("PRAGMA journal_mode=WAL")
    # With multiple processes writing (web + N builders + monitor),
    # writer/writer contention is inevitable. WAL only removes
    # reader/writer contention — busy_timeout is what keeps a second
    # writer waiting for the lock instead of raising OperationalError
    # immediately. 5s is generous; our write burst is tiny.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init(db_path: Path) -> None:
    """Create tables if absent; seed default concurrency caps."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # INSERT OR IGNORE so operator-tuned values survive re-init.
        for job_type, cap in _DEFAULT_LIMITS:
            conn.execute(
                "INSERT OR IGNORE INTO job_type_limits (job_type, max_concurrent) "
                "VALUES (?, ?)",
                (job_type, cap),
            )


def list_job_type_limits(db_path: Path) -> list[dict]:
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT job_type, max_concurrent FROM job_type_limits "
            "ORDER BY job_type"
        )]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Deserialize the cmd_json / args_json columns back to Python."""
    d = dict(row)
    d["cmd"] = json.loads(d.pop("cmd_json"))
    d["args"] = json.loads(d.pop("args_json"))
    return d


def enqueue(db_path: Path, *, job_id: str, job_type: str,
            playbook: str, cmd: list, args: dict) -> dict:
    """Insert a new pending job. Returns the row as a dict (with cmd + args
    already JSON-decoded for callers).
    """
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs "
            "(id, job_type, playbook, cmd_json, args_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (job_id, job_type, playbook, json.dumps(cmd), json.dumps(args), now),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(row)


def get_job(db_path: Path, job_id: str) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(db_path: Path, *, limit: int = 200) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def claim_next_job(db_path: Path, *, worker_id: str) -> dict | None:
    """Atomically claim the oldest pending job whose type is under its cap.

    BEGIN IMMEDIATE transaction, SELECT candidate respecting per-type cap,
    conditional UPDATE with status='pending' guard. Returns claimed row
    or None if nothing claimable.
    """
    now = _now()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("""
                SELECT j.id
                FROM jobs j
                JOIN job_type_limits l ON l.job_type = j.job_type
                WHERE j.status = 'pending'
                  AND (SELECT COUNT(*) FROM jobs
                       WHERE job_type = j.job_type AND status = 'running')
                      < l.max_concurrent
                ORDER BY j.created_at ASC, j.rowid ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            n = conn.execute(
                "UPDATE jobs SET status='running', worker_id=?, "
                "claimed_at=?, last_heartbeat=? "
                "WHERE id=? AND status='pending'",
                (worker_id, now, now, row["id"]),
            ).rowcount
            if n != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _row_to_dict(claimed)


def touch_heartbeat(db_path: Path, job_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET last_heartbeat=? WHERE id=?",
            (_now(), job_id),
        )


def finalize_job(db_path: Path, job_id: str, *, exit_code: int) -> None:
    status = "complete" if exit_code == 0 else "failed"
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, ended_at=? WHERE id=?",
            (status, exit_code, _now(), job_id),
        )


def request_kill(db_path: Path, job_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET kill_requested=1 WHERE id=?",
            (job_id,),
        )


def reap_orphans(db_path: Path, *, stale_threshold_seconds: int = 120) -> int:
    """Mark running jobs with stale heartbeats as orphaned. Returns row count.

    Called by the monitor container on a 30-second ticker (spec §5).
    Threshold 120s = 24x the 5s builder heartbeat cadence.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=stale_threshold_seconds)
              ).isoformat(timespec="seconds")
    now = _now()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='orphaned', ended_at=? "
            "WHERE status='running' AND last_heartbeat < ?",
            (now, cutoff),
        )
    return cur.rowcount
