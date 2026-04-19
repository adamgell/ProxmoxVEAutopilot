"""Migration test for target_os column on task_sequences."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from web import sequences_db


def test_fresh_init_has_target_os_column(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    sequences_db.init(db)
    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(task_sequences)"
        )}
    assert "target_os" in cols


def test_migration_backfills_existing_rows_to_windows(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    # Simulate a pre-migration DB by creating the old schema without target_os.
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE task_sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                produces_autopilot_hash INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO task_sequences (name, created_at, updated_at) "
            "VALUES ('legacy', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z')"
        )
    # Run init() on the pre-migration DB.
    sequences_db.init(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT target_os FROM task_sequences WHERE name='legacy'"
        ).fetchone()
    assert row[0] == "windows"


def test_target_os_accepts_ubuntu(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    sequences_db.init(db)
    seq_id = sequences_db.create_sequence(
        db,
        name="test-ubuntu",
        description="",
        is_default=False,
        produces_autopilot_hash=False,
        target_os="ubuntu",
        steps=[],
    )
    seq = sequences_db.get_sequence(db, seq_id)
    assert seq["target_os"] == "ubuntu"
