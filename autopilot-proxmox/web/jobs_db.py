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


# Job-type names MUST match the strings passed to job_manager.start()
# in web/app.py — the claim query LEFT JOINs on this table to enforce
# caps, so any job whose type is missing here falls back to the
# DEFAULT_CAP below. Previous versions misspelled some of these
# (`capture_hash` vs `hash_capture`, `hash_upload` vs
# `upload_after_capture`), which made the INNER-JOIN claim query
# silently skip those jobs forever. The fallback in claim_next_job
# now makes such misspellings degrade gracefully instead.
_DEFAULT_LIMITS = [
    ("build_template", 1),
    ("provision_clone", 3),
    ("hash_capture", 5),
    ("upload_after_capture", 5),
    ("retry_inject_hash", 3),
]

# Cap applied to any job_type NOT in job_type_limits. Conservative
# default — 1 serial job at a time — so an unknown type can still
# complete rather than block forever, but also can't accidentally
# thunder-herd the builder if an operator introduces a new type.
_DEFAULT_CAP = 1


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
    """Create tables if absent; seed default concurrency caps.

    Also migrates misspelled job_type names from earlier versions of
    the seed (capture_hash → hash_capture, hash_upload →
    upload_after_capture) so existing deploys that ran the old seed
    get their jobs_db.db corrected on next boot without manual SQL.
    """
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Rename legacy misspellings — keep whatever max_concurrent
        # the operator may have tuned; only the type name changes.
        for old, new in [
            ("capture_hash", "hash_capture"),
            ("hash_upload", "upload_after_capture"),
        ]:
            conn.execute(
                "UPDATE job_type_limits SET job_type = ? "
                "WHERE job_type = ? "
                "AND NOT EXISTS (SELECT 1 FROM job_type_limits WHERE job_type = ?)",
                (new, old, new),
            )
            # If both old and new rows exist (operator already added the
            # correct name manually), drop the old one rather than leave
            # a stale entry.
            conn.execute(
                "DELETE FROM job_type_limits WHERE job_type = ?",
                (old,),
            )
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
    """Deserialize the cmd_json / args_json columns back to Python.

    Also emit ``started`` / ``ended`` aliases alongside the new
    ``created_at`` / ``ended_at`` columns so the pre-split templates
    (``jobs.html``, ``job_detail.html``) and the duration-computing
    code in ``app.py`` keep working without edits. New code should use
    the canonical names; these aliases are kept indefinitely for the
    simple reason that there's no real cost to carrying them.
    """
    d = dict(row)
    d["cmd"] = json.loads(d.pop("cmd_json"))
    d["args"] = json.loads(d.pop("args_json"))
    d["started"] = d.get("created_at")
    d["ended"] = d.get("ended_at")
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
            # LEFT JOIN + COALESCE so a job_type with no row in
            # job_type_limits (operator added a new type, or we missed
            # seeding one) falls back to DEFAULT_CAP (1) instead of
            # being ignored by an INNER JOIN. The INNER-JOIN form was
            # why `hash_capture` / `upload_after_capture` jobs hung as
            # pending forever when the seed table was misspelled
            # `capture_hash` / `hash_upload`.
            row = conn.execute(f"""
                SELECT j.id
                FROM jobs j
                LEFT JOIN job_type_limits l ON l.job_type = j.job_type
                WHERE j.status = 'pending'
                  AND (SELECT COUNT(*) FROM jobs
                       WHERE job_type = j.job_type AND status = 'running')
                      < COALESCE(l.max_concurrent, {_DEFAULT_CAP})
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


def touch_heartbeat(db_path: Path, job_id: str) -> int:
    """UPDATE last_heartbeat. Returns rowcount — 0 means the row was
    reaped or finalized elsewhere; caller should stop touching it.
    Guarded on status='running' so a reaped row doesn't get silently
    revived."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET last_heartbeat=? "
            "WHERE id=? AND status='running'",
            (_now(), job_id),
        )
    return cur.rowcount


def finalize_job(db_path: Path, job_id: str, *, exit_code: int) -> int:
    """Terminal-state write. Guarded on status='running' so a reaped
    row isn't flipped back to complete/failed, which would mask the
    reap in the audit trail. Returns rowcount — 0 means the row was
    already reaped; caller should log + skip. Also clears
    kill_requested so the terminal row is a clean record."""
    status = "complete" if exit_code == 0 else "failed"
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, ended_at=?, "
            "kill_requested=0 WHERE id=? AND status='running'",
            (status, exit_code, _now(), job_id),
        )
    return cur.rowcount


def request_kill(db_path: Path, job_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET kill_requested=1 WHERE id=?",
            (job_id,),
        )


def reap_orphans(db_path: Path, *, stale_threshold_seconds: int = 300) -> int:
    """Mark running jobs with stale heartbeats as orphaned. 300s =
    60x the 5s heartbeat cadence, generous under DB-write contention
    while still catching truly dead builders within reasonable time.

    Emits a WARNING for any row whose heartbeat was newer than
    threshold * 0.5 at reap time — that's the smoking gun for a
    'reaped while likely alive' bug and shouldn't happen in normal
    operation. Monitor this in the logs.
    """
    import logging
    from datetime import datetime, timezone, timedelta
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(seconds=stale_threshold_seconds)
              ).isoformat(timespec="seconds")
    warn_cutoff = (now_dt - timedelta(seconds=stale_threshold_seconds / 2)
                   ).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        candidates = conn.execute(
            "SELECT id, last_heartbeat FROM jobs "
            "WHERE status='running' AND last_heartbeat < ?",
            (cutoff,),
        ).fetchall()
        if not candidates:
            return 0
        now_iso = now_dt.isoformat(timespec="seconds")
        for row in candidates:
            if row["last_heartbeat"] and row["last_heartbeat"] > warn_cutoff:
                logging.getLogger("web.jobs_db").warning(
                    "reap_orphans: flipping job %s to orphaned but "
                    "heartbeat %s is newer than %s (possibly alive) — "
                    "investigate DB-write contention or reaper threshold",
                    row["id"], row["last_heartbeat"], warn_cutoff,
                )
        cur = conn.execute(
            "UPDATE jobs SET status='orphaned', ended_at=? "
            "WHERE status='running' AND last_heartbeat < ?",
            (now_iso, cutoff),
        )
    return cur.rowcount


def _insert_migrated(db_path: Path, *, job_id: str, job_type: str,
                     playbook: str, args: dict, status: str,
                     started_at: str, ended_at: str | None,
                     exit_code: int | None) -> None:
    """Internal helper for jobs_migration. Inserts a row with specific
    status (complete/failed/orphaned) — bypasses normal enqueue flow."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, job_type, playbook, cmd_json, args_json, status, "
            " created_at, claimed_at, ended_at, exit_code) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)",
            (job_id, job_type, playbook, json.dumps(args),
             status, started_at, started_at, ended_at, exit_code),
        )
