from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _database_url() -> str:
    return os.environ.get("AUTOPILOT_TS_ENGINE_DATABASE_URL", "")


def _connection():
    from web import db_pg

    dsn = _database_url()
    if not dsn:
        return None
    return db_pg.connection(dsn)


def init() -> None:
    cm = _connection()
    if cm is None:
        return
    with cm as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_call_audit (
              id BIGSERIAL PRIMARY KEY,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              correlation_id TEXT,
              tool_name TEXT,
              caller TEXT,
              arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
              result JSONB NOT NULL DEFAULT '{}'::jsonb,
              error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_action_approvals (
              id BIGSERIAL PRIMARY KEY,
              approval_id TEXT NOT NULL UNIQUE,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 minutes',
              tool_name TEXT NOT NULL,
              target_summary TEXT NOT NULL DEFAULT '',
              risk_label TEXT NOT NULL DEFAULT 'sensitive',
              arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
              status TEXT NOT NULL DEFAULT 'pending',
              result JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mcp_action_approvals_status_time
              ON mcp_action_approvals(status, created_at DESC)
            """
        )


def reset_for_tests(conn: Any) -> None:
    conn.execute("DROP TABLE IF EXISTS mcp_call_audit CASCADE")
    conn.execute("DROP TABLE IF EXISTS mcp_action_approvals CASCADE")
    conn.commit()


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


def audit_call(
    *,
    tool_name: str | None,
    arguments: dict[str, Any] | None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    correlation_id: str | None = None,
    caller: str | None = None,
) -> None:
    try:
        cm = _connection()
        if cm is None:
            return
        with cm as conn:
            conn.execute(
                """
                INSERT INTO mcp_call_audit
                  (correlation_id, tool_name, caller, arguments, result, error)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                (
                    correlation_id,
                    tool_name,
                    caller,
                    json.dumps(arguments or {}, default=str),
                    json.dumps(result or {}, default=str),
                    error,
                ),
            )
    except Exception:
        return


def create_approval(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    target_summary: str = "",
    risk_label: str = "sensitive",
) -> dict[str, Any]:
    init()
    cm = _connection()
    approval_id = str(uuid4())
    if cm is None:
        return {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "target_summary": target_summary,
            "risk_label": risk_label,
            "arguments": arguments,
            "status": "pending",
            "ephemeral": True,
        }
    with cm as conn:
        row = conn.execute(
            """
            INSERT INTO mcp_action_approvals (
              approval_id, tool_name, target_summary, risk_label, arguments
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
            (
                approval_id,
                tool_name,
                target_summary,
                risk_label,
                json.dumps(arguments or {}, default=str),
            ),
        ).fetchone()
    return _row_dict(row)


def list_approvals(*, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init()
    cm = _connection()
    if cm is None:
        return []
    limit = max(1, min(int(limit or 100), 500))
    clauses = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with cm as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM mcp_action_approvals
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (*params, limit),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def get_approval(approval_id: str) -> dict[str, Any] | None:
    init()
    cm = _connection()
    if cm is None:
        return None
    with cm as conn:
        row = conn.execute(
            "SELECT * FROM mcp_action_approvals WHERE approval_id = %s",
            (approval_id,),
        ).fetchone()
    return _row_dict(row) if row else None


def _approval_expired(row: dict[str, Any]) -> bool:
    raw = row.get("expires_at")
    if not raw:
        return False
    if isinstance(raw, str):
        try:
            expires_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
    else:
        expires_at = raw
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def update_approval_status(
    approval_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    init()
    cm = _connection()
    if cm is None:
        return None
    with cm as conn:
        row = conn.execute(
            """
            UPDATE mcp_action_approvals
            SET status = %s,
                result = %s::jsonb
            WHERE approval_id = %s
            RETURNING *
            """,
            (
                status,
                json.dumps(result or {}, default=str),
                approval_id,
            ),
        ).fetchone()
    return _row_dict(row) if row else None


def reject_approval(approval_id: str, *, reason: str = "") -> dict[str, Any] | None:
    row = get_approval(approval_id)
    if not row:
        return None
    if row.get("status") != "pending":
        return row
    return update_approval_status(
        approval_id,
        status="rejected",
        result={"rejected": True, "reason": reason},
    )


def approve_approval(
    approval_id: str,
    *,
    executors: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    row = get_approval(approval_id)
    if not row:
        return None
    if row.get("status") != "pending":
        return row
    if _approval_expired(row):
        return update_approval_status(
            approval_id,
            status="expired",
            result={"executed": False, "reason": "approval_expired"},
        )
    executor = (executors or {}).get(str(row.get("tool_name") or ""))
    if executor is None:
        return update_approval_status(
            approval_id,
            status="failed",
            result={
                "executed": False,
                "reason": "executor_not_registered",
                "tool_name": row.get("tool_name"),
            },
        )
    try:
        result = executor(dict(row.get("arguments") or {}))
    except Exception as exc:
        return update_approval_status(
            approval_id,
            status="failed",
            result={"executed": False, "error": str(exc), "type": exc.__class__.__name__},
        )
    return update_approval_status(
        approval_id,
        status="executed",
        result={"executed": True, "result": result},
    )
