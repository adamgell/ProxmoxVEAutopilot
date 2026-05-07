"""PostgreSQL-backed per-service heartbeat table for /monitoring."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import Connection

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS service_health (
    service_id text PRIMARY KEY,
    service_type text NOT NULL,
    version_sha text NOT NULL,
    started_at timestamptz NOT NULL,
    last_heartbeat timestamptz NOT NULL,
    detail text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS service_health_by_type ON service_health(service_type);
"""


OK_THRESHOLD = 20
DEGRADED_THRESHOLD = 50


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def heartbeat(
    *,
    service_id: str,
    service_type: str,
    version_sha: str,
    detail: str = "",
) -> None:
    now = _now()
    with db_pg.connection() as conn:
        conn.execute(
            """
            INSERT INTO service_health
                (service_id, service_type, version_sha, started_at, last_heartbeat, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (service_id) DO UPDATE SET
                service_type = EXCLUDED.service_type,
                version_sha = EXCLUDED.version_sha,
                last_heartbeat = EXCLUDED.last_heartbeat,
                detail = EXCLUDED.detail
            """,
            (service_id, service_type, version_sha, now, now, detail),
        )
        conn.commit()


def _coerce_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _classify(last_heartbeat: datetime, now: datetime) -> str:
    age = (now - _coerce_utc(last_heartbeat)).total_seconds()
    if age <= OK_THRESHOLD:
        return "ok"
    if age <= DEGRADED_THRESHOLD:
        return "degraded"
    return "dead"


def list_services() -> list[dict]:
    now = _now()
    with db_pg.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM service_health ORDER BY service_type, service_id"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        last_heartbeat = _coerce_utc(item["last_heartbeat"])
        item["status"] = _classify(last_heartbeat, now)
        item["age_seconds"] = int((now - last_heartbeat).total_seconds())
        for key in ("started_at", "last_heartbeat"):
            item[key] = _coerce_utc(item[key]).isoformat()
        out.append(item)
    return out


def prune_dead_workers(*, max_age_seconds: int = 600) -> int:
    cutoff = _now() - timedelta(seconds=max_age_seconds)
    with db_pg.connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM service_health
            WHERE service_type = 'builder'
              AND last_heartbeat < %s
            """,
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
