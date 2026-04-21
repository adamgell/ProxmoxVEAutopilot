"""SQLite-backed job queue + per-type concurrency caps.

Design: docs/specs/2026-04-21-microservice-split-design.md §2

Layout mirrors web/sequences_db.py — module-level SCHEMA string,
init() via executescript, context-managed connections.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
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
