"""PostgreSQL-backed job queue + per-type concurrency caps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    job_type text NOT NULL,
    playbook text NOT NULL,
    cmd_json jsonb NOT NULL,
    args_json jsonb NOT NULL,
    status text NOT NULL,
    worker_id text NULL,
    kill_requested boolean NOT NULL DEFAULT false,
    exit_code integer NULL,
    created_at timestamptz NOT NULL,
    claimed_at timestamptz NULL,
    last_heartbeat timestamptz NULL,
    ended_at timestamptz NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_type_limits (
    job_type text PRIMARY KEY,
    max_concurrent integer NOT NULL CHECK (max_concurrent > 0)
);
"""


DEFAULT_LIMITS = [
    ("build_template", 1),
    ("provision_clone", 3),
    ("cloudosd_build_iso", 1),
    ("provision_cloudosd", 4),
    ("hash_capture", 5),
    ("upload_hash", 5),
    ("upload_after_capture", 5),
    ("retry_inject_hash", 3),
]
DEFAULT_CAP = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        for job_type, cap in DEFAULT_LIMITS:
            if job_type == "provision_cloudosd":
                conn.execute(
                    """
                    INSERT INTO job_type_limits (job_type, max_concurrent)
                    VALUES (%s, %s)
                    ON CONFLICT (job_type) DO UPDATE
                    SET max_concurrent = EXCLUDED.max_concurrent
                    """,
                    (job_type, cap),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO job_type_limits (job_type, max_concurrent)
                    VALUES (%s, %s)
                    ON CONFLICT (job_type) DO NOTHING
                    """,
                    (job_type, cap),
                )
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    conn.execute("DROP TABLE IF EXISTS job_type_limits CASCADE")
    conn.commit()


def _json_value(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value.obj
    return value


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["cmd"] = _json_value(out.pop("cmd_json"))
    out["args"] = _json_value(out.pop("args_json"))
    out["started"] = _iso(out.get("created_at"))
    out["ended"] = _iso(out.get("ended_at"))
    for key in ("created_at", "claimed_at", "last_heartbeat", "ended_at"):
        out[key] = _iso(out.get(key))
    return out


def enqueue(
    *,
    job_id: str,
    job_type: str,
    playbook: str,
    cmd: list,
    args: dict,
) -> dict:
    now = _now()
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO jobs
                (id, job_type, playbook, cmd_json, args_json, status, created_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', %s)
            RETURNING *
            """,
            (job_id, job_type, playbook, Jsonb(cmd), Jsonb(args), now),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)


def get_job(job_id: str) -> dict | None:
    with db_pg.connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        return _row_to_dict(row)


def list_jobs(*, limit: int = 200) -> list[dict]:
    with db_pg.connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM jobs
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]


def claim_next_job(*, worker_id: str) -> dict | None:
    """Atomically claim the oldest pending job whose type is under cap."""
    now = _now()
    with db_pg.connection() as conn:
        with conn.transaction():
            candidate_types = conn.execute(
                """
                SELECT job_type, min(created_at) AS first_created, min(id) AS first_id
                FROM jobs
                WHERE status = 'pending'
                GROUP BY job_type
                ORDER BY first_created ASC, first_id ASC
                """
            ).fetchall()
            for candidate in candidate_types:
                job_type = candidate["job_type"]
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (job_type,),
                )
                limit_row = conn.execute(
                    """
                    SELECT max_concurrent
                    FROM job_type_limits
                    WHERE job_type = %s
                    """,
                    (job_type,),
                ).fetchone()
                cap = limit_row["max_concurrent"] if limit_row else DEFAULT_CAP
                running = conn.execute(
                    """
                    SELECT count(*) AS count
                    FROM jobs
                    WHERE job_type = %s AND status = 'running'
                    """,
                    (job_type,),
                ).fetchone()["count"]
                if running >= cap:
                    continue

                pending = conn.execute(
                    """
                    SELECT id
                    FROM jobs
                    WHERE job_type = %s AND status = 'pending'
                    ORDER BY created_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (job_type,),
                ).fetchone()
                if not pending:
                    continue
                row = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'running',
                        worker_id = %s,
                        claimed_at = %s,
                        last_heartbeat = %s
                    WHERE id = %s AND status = 'pending'
                    RETURNING *
                    """,
                    (worker_id, now, now, pending["id"]),
                ).fetchone()
                if row:
                    return _row_to_dict(row)
        return None


def touch_heartbeat(job_id: str, worker_id: str) -> int:
    with db_pg.connection() as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET last_heartbeat = %s
            WHERE id = %s AND worker_id = %s AND status = 'running'
            """,
            (_now(), job_id, worker_id),
        )
        conn.commit()
        return cur.rowcount


def finalize_job(job_id: str, *, exit_code: int) -> int:
    status = "complete" if exit_code == 0 else "failed"
    with db_pg.connection() as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = %s,
                exit_code = %s,
                ended_at = %s,
                kill_requested = false
            WHERE id = %s AND status = 'running'
            """,
            (status, exit_code, _now(), job_id),
        )
        conn.commit()
        return cur.rowcount


def request_kill(job_id: str) -> None:
    with db_pg.connection() as conn:
        conn.execute(
            "UPDATE jobs SET kill_requested = true WHERE id = %s",
            (job_id,),
        )
        conn.commit()


def reap_stale_running_jobs(*, older_than_seconds: int = 900) -> int:
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    with db_pg.connection() as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = 'orphaned',
                ended_at = %s
            WHERE status = 'running'
              AND last_heartbeat < %s
            """,
            (_now(), cutoff),
        )
        conn.commit()
        return cur.rowcount


def reap_orphans(*, stale_threshold_seconds: int = 300) -> int:
    return reap_stale_running_jobs(older_than_seconds=stale_threshold_seconds)


def list_job_type_limits() -> list[dict]:
    with db_pg.connection() as conn:
        return conn.execute(
            """
            SELECT job_type, max_concurrent
            FROM job_type_limits
            ORDER BY job_type
            """
        ).fetchall()


def update_job_type_limit(job_type: str, max_concurrent: int) -> dict:
    cap = int(max_concurrent)
    if cap <= 0:
        raise ValueError("max_concurrent must be a positive integer")
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO job_type_limits (job_type, max_concurrent)
            VALUES (%s, %s)
            ON CONFLICT (job_type)
            DO UPDATE SET max_concurrent = EXCLUDED.max_concurrent
            RETURNING job_type, max_concurrent
            """,
            (job_type, cap),
        ).fetchone()
        conn.commit()
        return row


def update_job_args(job_id: str, args: dict) -> int:
    with db_pg.connection() as conn:
        cur = conn.execute(
            "UPDATE jobs SET args_json = %s WHERE id = %s",
            (Jsonb(args), job_id),
        )
        conn.commit()
        return cur.rowcount


def complete_interrupted_provision_winpe_jobs_for_run(*, run_id: int) -> int:
    with db_pg.connection() as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = 'complete',
                exit_code = 0,
                ended_at = %s,
                kill_requested = false
            WHERE job_type IN ('provision_winpe', 'provision_clone')
              AND status IN ('failed', 'orphaned')
              AND args_json->>'run_id' = %s
              AND (status = 'orphaned' OR exit_code IN (-15) OR exit_code IS NULL)
            """,
            (_now(), str(run_id)),
        )
        conn.commit()
        return cur.rowcount
