"""PostgreSQL store for deployment phase timing telemetry."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


PHASE_STATES = {"pending", "running", "done", "failed", "skipped", "stale"}
TERMINAL_STATES = {"done", "failed", "skipped", "stale"}
SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "download_url",
    "url",
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS deployment_phase_timings (
    deployment_key text NOT NULL,
    deployment_type text NOT NULL,
    source text NOT NULL,
    source_id text NOT NULL,
    phase_key text NOT NULL,
    phase_label text NOT NULL,
    state text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NULL,
    last_progress_at timestamptz NOT NULL,
    duration_seconds integer NULL,
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (deployment_key, phase_key),
    CHECK (state IN ('pending', 'running', 'done', 'failed', 'skipped', 'stale'))
);
CREATE INDEX IF NOT EXISTS idx_deployment_phase_timings_type_phase
    ON deployment_phase_timings(deployment_type, phase_key, state, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployment_phase_timings_progress
    ON deployment_phase_timings(state, last_progress_at DESC);

CREATE TABLE IF NOT EXISTS deployment_phase_baselines (
    deployment_type text NOT NULL,
    phase_key text NOT NULL,
    sample_count integer NOT NULL,
    p50_seconds integer NULL,
    p90_seconds integer NULL,
    p95_seconds integer NULL,
    failure_rate double precision NOT NULL DEFAULT 0,
    health text NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (deployment_type, phase_key)
);
"""


DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS deployment_phase_baselines CASCADE;
DROP TABLE IF EXISTS deployment_phase_timings CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(value: Any) -> str | None:
    dt = _coerce_dt(value)
    return dt.isoformat() if dt else None


def _duration(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    if not (started_at and ended_at):
        return None
    return max(0, int((ended_at - started_at).total_seconds()))


def _sanitize_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                continue
            out[str(key)] = _sanitize_evidence(child)
        return out
    if isinstance(value, list):
        return [_sanitize_evidence(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _merge_state(existing: str | None, incoming: str) -> str:
    if incoming not in PHASE_STATES:
        raise ValueError(f"unsupported phase state: {incoming!r}")
    if incoming == "failed":
        return "failed"
    if existing == "failed" and incoming in {"done", "skipped"}:
        return incoming
    if existing == "failed" and incoming != "done":
        return "failed"
    if existing in TERMINAL_STATES and incoming in {"pending", "running"}:
        return existing
    return incoming


def _row_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["started_at"] = _iso(out.get("started_at"))
    out["ended_at"] = _iso(out.get("ended_at"))
    out["last_progress_at"] = _iso(out.get("last_progress_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    out["evidence"] = out.pop("evidence_json") or {}
    return out


def _baseline_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["sample_count"] = int(out["sample_count"])
    for key in ("p50_seconds", "p90_seconds", "p95_seconds"):
        out[key] = int(out[key]) if out[key] is not None else None
    out["failure_rate"] = float(out.get("failure_rate") or 0)
    out["updated_at"] = _iso(out.get("updated_at"))
    return out


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()


def record_phase(
    conn: Connection,
    *,
    deployment_key: str,
    deployment_type: str,
    source: str,
    source_id: str,
    phase_key: str,
    phase_label: str,
    state: str,
    started_at: Any,
    ended_at: Any = None,
    last_progress_at: Any = None,
    evidence: dict | None = None,
    error: str | None = None,
    commit: bool = True,
    init_schema: bool = True,
) -> dict:
    if init_schema:
        init(conn)
    started = _coerce_dt(started_at) or _now()
    ended = _coerce_dt(ended_at)
    progress = (
        _coerce_dt(last_progress_at)
        or ended
        or started
    )
    incoming_evidence = _sanitize_evidence(evidence or {})
    existing = conn.execute(
        """
        SELECT *
        FROM deployment_phase_timings
        WHERE deployment_key = %s AND phase_key = %s
        """,
        (deployment_key, phase_key),
    ).fetchone()
    if existing:
        current = dict(existing)
        prior_started = _coerce_dt(current.get("started_at"))
        prior_ended = _coerce_dt(current.get("ended_at"))
        prior_progress = _coerce_dt(current.get("last_progress_at"))
        merged_started = min(
            [dt for dt in (prior_started, started) if dt is not None],
        )
        merged_ended = ended or prior_ended
        merged_progress = max(
            [dt for dt in (prior_progress, progress, merged_ended, merged_started) if dt is not None],
        )
        merged_evidence = {
            **(current.get("evidence_json") or {}),
            **incoming_evidence,
        }
        row = conn.execute(
            """
            UPDATE deployment_phase_timings
            SET deployment_type = %s,
                source = %s,
                source_id = %s,
                phase_label = %s,
                state = %s,
                started_at = %s,
                ended_at = %s,
                last_progress_at = %s,
                duration_seconds = %s,
                evidence_json = %s,
                error = CASE
                    WHEN %s IN ('done', 'skipped') THEN %s
                    ELSE COALESCE(%s, error)
                END,
                updated_at = %s
            WHERE deployment_key = %s AND phase_key = %s
            RETURNING *
            """,
            (
                deployment_type,
                source,
                source_id,
                phase_label,
                _merge_state(current.get("state"), state),
                merged_started,
                merged_ended,
                merged_progress,
                _duration(merged_started, merged_ended),
                Jsonb(merged_evidence),
                state,
                error,
                error,
                _now(),
                deployment_key,
                phase_key,
            ),
        ).fetchone()
    else:
        row = conn.execute(
            """
            INSERT INTO deployment_phase_timings (
                deployment_key, deployment_type, source, source_id,
                phase_key, phase_label, state, started_at, ended_at,
                last_progress_at, duration_seconds, evidence_json, error, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                deployment_key,
                deployment_type,
                source,
                source_id,
                phase_key,
                phase_label,
                state,
                started,
                ended,
                progress,
                _duration(started, ended),
                Jsonb(incoming_evidence),
                error,
                _now(),
            ),
        ).fetchone()
    if commit:
        conn.commit()
    return _row_dict(row)


def start_phase(conn: Connection, **kwargs) -> dict:
    return record_phase(conn, state="running", **kwargs)


def progress_phase(
    conn: Connection,
    *,
    deployment_key: str,
    phase_key: str,
    last_progress_at: Any = None,
    evidence: dict | None = None,
) -> dict | None:
    existing = conn.execute(
        """
        SELECT *
        FROM deployment_phase_timings
        WHERE deployment_key = %s AND phase_key = %s
        """,
        (deployment_key, phase_key),
    ).fetchone()
    if not existing:
        return None
    data = dict(existing)
    return record_phase(
        conn,
        deployment_key=deployment_key,
        deployment_type=data["deployment_type"],
        source=data["source"],
        source_id=data["source_id"],
        phase_key=phase_key,
        phase_label=data["phase_label"],
        state=data["state"],
        started_at=data["started_at"],
        ended_at=data["ended_at"],
        last_progress_at=last_progress_at or _now(),
        evidence=evidence,
        error=data.get("error"),
    )


def end_phase(
    conn: Connection,
    *,
    deployment_key: str,
    phase_key: str,
    ended_at: Any = None,
    evidence: dict | None = None,
) -> dict | None:
    existing = conn.execute(
        """
        SELECT *
        FROM deployment_phase_timings
        WHERE deployment_key = %s AND phase_key = %s
        """,
        (deployment_key, phase_key),
    ).fetchone()
    if not existing:
        return None
    data = dict(existing)
    return record_phase(
        conn,
        deployment_key=deployment_key,
        deployment_type=data["deployment_type"],
        source=data["source"],
        source_id=data["source_id"],
        phase_key=phase_key,
        phase_label=data["phase_label"],
        state="done",
        started_at=data["started_at"],
        ended_at=ended_at or _now(),
        evidence=evidence,
    )


def fail_phase(
    conn: Connection,
    *,
    deployment_key: str,
    phase_key: str,
    ended_at: Any = None,
    error: str = "",
    evidence: dict | None = None,
) -> dict | None:
    existing = conn.execute(
        """
        SELECT *
        FROM deployment_phase_timings
        WHERE deployment_key = %s AND phase_key = %s
        """,
        (deployment_key, phase_key),
    ).fetchone()
    if not existing:
        return None
    data = dict(existing)
    return record_phase(
        conn,
        deployment_key=deployment_key,
        deployment_type=data["deployment_type"],
        source=data["source"],
        source_id=data["source_id"],
        phase_key=phase_key,
        phase_label=data["phase_label"],
        state="failed",
        started_at=data["started_at"],
        ended_at=ended_at or _now(),
        evidence=evidence,
        error=error,
    )


def list_phases(conn: Connection, deployment_key: str) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM deployment_phase_timings
        WHERE deployment_key = %s
        ORDER BY started_at ASC, phase_key ASC
        """,
        (deployment_key,),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def list_all_phases(conn: Connection, *, limit: int = 1000) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM deployment_phase_timings
        ORDER BY started_at DESC, deployment_key ASC, phase_key ASC
        LIMIT %s
        """,
        (max(1, min(int(limit or 1000), 5000)),),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _percentile_nearest(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile / 100 * len(ordered)) - 1))
    return int(ordered[index])


def recompute_baselines(conn: Connection) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT deployment_type, phase_key, state, duration_seconds, started_at
        FROM deployment_phase_timings
        WHERE state IN ('done', 'failed')
        ORDER BY deployment_type, phase_key, started_at DESC
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["deployment_type"], row["phase_key"]), []).append(dict(row))
    out: list[dict] = []
    now = _now()
    for (deployment_type, phase_key), items in grouped.items():
        successful = [
            int(item["duration_seconds"])
            for item in items
            if item["state"] == "done" and item.get("duration_seconds") is not None
        ][:50]
        failure_count = sum(1 for item in items[:50] if item["state"] == "failed")
        denominator = max(1, min(50, len(items)))
        sample_count = len(successful)
        health = "learning" if sample_count < 5 else "healthy"
        row = conn.execute(
            """
            INSERT INTO deployment_phase_baselines (
                deployment_type, phase_key, sample_count, p50_seconds,
                p90_seconds, p95_seconds, failure_rate, health, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (deployment_type, phase_key)
            DO UPDATE SET
                sample_count = EXCLUDED.sample_count,
                p50_seconds = EXCLUDED.p50_seconds,
                p90_seconds = EXCLUDED.p90_seconds,
                p95_seconds = EXCLUDED.p95_seconds,
                failure_rate = EXCLUDED.failure_rate,
                health = EXCLUDED.health,
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            (
                deployment_type,
                phase_key,
                sample_count,
                _percentile_nearest(successful, 50),
                _percentile_nearest(successful, 90),
                _percentile_nearest(successful, 95),
                float(failure_count / denominator),
                health,
                now,
            ),
        ).fetchone()
        out.append(_baseline_row(row))
    conn.commit()
    return sorted(out, key=lambda row: (row["deployment_type"], row["phase_key"]))


def list_baselines(conn: Connection) -> list[dict]:
    init(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM deployment_phase_baselines
        ORDER BY deployment_type, phase_key
        """
    ).fetchall()
    return [_baseline_row(row) for row in rows]
