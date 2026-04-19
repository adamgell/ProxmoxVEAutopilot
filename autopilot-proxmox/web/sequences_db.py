"""SQLite-backed store for task sequences, steps, and credentials.

Mirrors web.devices_db: module-level SCHEMA string, init() via executescript,
context-managed connections with row_factory=sqlite3.Row.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    produces_autopilot_hash INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_sequence_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (sequence_id, order_index)
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    encrypted_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vm_provisioning (
    vmid INTEGER PRIMARY KEY,
    sequence_id INTEGER REFERENCES task_sequences(id) ON DELETE SET NULL,
    provisioned_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: Path) -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


class CredentialInUse(Exception):
    """Raised when attempting to delete a credential referenced by a step."""
    def __init__(self, cred_id: int, sequence_ids: list[int]):
        super().__init__(
            f"Credential {cred_id} is referenced by sequences {sequence_ids}"
        )
        self.cred_id = cred_id
        self.sequence_ids = sequence_ids


class SequenceInUse(Exception):
    """Raised when attempting to delete a sequence referenced by vm_provisioning."""
    def __init__(self, sequence_id: int, vmids: list[int]):
        super().__init__(
            f"Sequence {sequence_id} is referenced by VMs {vmids}"
        )
        self.sequence_id = sequence_id
        self.vmids = vmids


def _row_to_credential_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_credential(db_path, cipher, *, name: str, type: str, payload: dict) -> int:
    now = _now()
    blob = cipher.encrypt_json(payload)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO credentials (name, type, encrypted_blob, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, type, blob, now, now),
        )
        return cur.lastrowid


def update_credential(db_path, cipher, cred_id: int, *,
                      name: Optional[str] = None,
                      payload: Optional[dict] = None) -> None:
    now = _now()
    with _connect(db_path) as conn:
        updates, args = [], []
        if name is not None:
            updates.append("name = ?")
            args.append(name)
        if payload is not None:
            updates.append("encrypted_blob = ?")
            args.append(cipher.encrypt_json(payload))
        if not updates:
            return
        updates.append("updated_at = ?")
        args.append(now)
        args.append(cred_id)
        conn.execute(
            f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?", args
        )


def get_credential(db_path, cipher, cred_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, type, encrypted_blob, created_at, updated_at "
            "FROM credentials WHERE id = ?",
            (cred_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        **_row_to_credential_summary(row),
        "payload": cipher.decrypt_json(row["encrypted_blob"]),
    }


def list_credentials(db_path, type: Optional[str] = None) -> list[dict]:
    query = "SELECT id, name, type, created_at, updated_at FROM credentials"
    args: tuple = ()
    if type is not None:
        query += " WHERE type = ?"
        args = (type,)
    query += " ORDER BY name ASC"
    with _connect(db_path) as conn:
        return [_row_to_credential_summary(r) for r in conn.execute(query, args)]


def delete_credential(db_path, cred_id: int) -> None:
    """Delete a credential. Raises CredentialInUse if referenced by any step."""
    with _connect(db_path) as conn:
        referencing = _sequences_referencing_credential(conn, cred_id)
        if referencing:
            raise CredentialInUse(cred_id, referencing)
        conn.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))


def _sequences_referencing_credential(conn: sqlite3.Connection, cred_id: int) -> list[int]:
    """Scan task_sequence_steps.params_json for credential_id references.

    Uses json_extract (SQLite 3.38+). Returns distinct sequence IDs.
    """
    rows = conn.execute(
        "SELECT DISTINCT sequence_id FROM task_sequence_steps "
        "WHERE json_extract(params_json, '$.credential_id') = ?",
        (cred_id,),
    ).fetchall()
    return sorted(r["sequence_id"] for r in rows)
