"""Per-service heartbeat table. Read by the /monitoring health strip.

Design: docs/specs/2026-04-21-microservice-split-design.md §6
Lives in device_monitor.db alongside device state (same DB so the
/monitoring page is one connection, not two).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_health (
    service_id      TEXT PRIMARY KEY,
    service_type    TEXT NOT NULL,
    version_sha     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    last_heartbeat  TEXT NOT NULL,
    detail          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS service_health_by_type ON service_health(service_type);
"""


_OK_THRESHOLD       = 20    # up to 2× heartbeat interval (10s) = ok
_DEGRADED_THRESHOLD = 50    # 2–5× = degraded; beyond = dead


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def init(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def heartbeat(db_path: Path, *, service_id: str, service_type: str,
              version_sha: str, detail: str = "") -> None:
    """UPSERT one row. Called every 10s by each container."""
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO service_health "
            "  (service_id, service_type, version_sha, started_at, last_heartbeat, detail) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(service_id) DO UPDATE SET "
            "  service_type = excluded.service_type, "
            "  version_sha  = excluded.version_sha, "
            "  last_heartbeat = excluded.last_heartbeat, "
            "  detail       = excluded.detail",
            (service_id, service_type, version_sha, now, now, detail),
        )


def _parse_utc(iso: str) -> datetime:
    """Parse an ISO timestamp and guarantee a tz-aware UTC datetime.

    All heartbeats written by this module include the ``+00:00`` offset,
    but a stray naive row (e.g., hand-inserted for testing, or an older
    migration path that dropped the tz) would otherwise raise TypeError
    when subtracted from a tz-aware value.
    """
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _classify(last_heartbeat_iso: str, now_iso: str) -> str:
    last = _parse_utc(last_heartbeat_iso)
    now = _parse_utc(now_iso)
    age = (now - last).total_seconds()
    if age <= _OK_THRESHOLD:
        return "ok"
    if age <= _DEGRADED_THRESHOLD:
        return "degraded"
    return "dead"


def list_services(db_path: Path) -> list[dict]:
    """Render-ready rows with a computed `status` + `age_seconds` column."""
    now_iso = _now()
    now_dt = _parse_utc(now_iso)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM service_health ORDER BY service_type, service_id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _classify(d["last_heartbeat"], now_iso)
        last = _parse_utc(d["last_heartbeat"])
        d["age_seconds"] = int((now_dt - last).total_seconds())
        out.append(d)
    return out


def prune_dead_workers(db_path: Path, *, max_age_seconds: int = 600) -> int:
    """Drop builder rows whose heartbeat is older than max_age_seconds."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=max_age_seconds)
              ).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM service_health "
            "WHERE service_type='builder' AND last_heartbeat < ?",
            (cutoff,),
        )
    return cur.rowcount
