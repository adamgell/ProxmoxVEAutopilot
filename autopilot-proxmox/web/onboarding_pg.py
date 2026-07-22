"""PostgreSQL state store for the operator onboarding wizard."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from web import db_pg


VALID_STATUSES = {"pending", "in_progress", "launched", "complete", "aborted"}
VALID_PERSONAS = {"lab", "msp", "corp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS onboarding_state (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_sub       text NOT NULL UNIQUE,
    status          text NOT NULL DEFAULT 'in_progress',
    current_step    text NOT NULL DEFAULT 'welcome',
    persona         text,
    answers         jsonb NOT NULL DEFAULT '{}'::jsonb,
    launched_run_id text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
"""

DROP_SCHEMA_FOR_TESTS = "DROP TABLE IF EXISTS onboarding_state CASCADE;"


def init(conn: Connection | None = None) -> None:
    """Create the onboarding_state table if missing. Idempotent."""
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


class StaleEtag(Exception):
    """Raised when If-Match does not match the current row's etag."""


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    if isinstance(out.get("answers"), str):
        import json
        out["answers"] = json.loads(out["answers"])
    out["etag"] = _etag_for(out["updated_at"])
    return out


def _etag_for(updated_at: datetime) -> str:
    return f'W/"{updated_at.isoformat()}"'


def get_state(conn: Connection, owner_sub: str) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        "SELECT * FROM onboarding_state WHERE owner_sub = %s",
        (owner_sub,),
    ).fetchone()
    return _row_to_dict(row)


def put_state(
    conn: Connection,
    *,
    owner_sub: str,
    if_match: str | None,
    patch: dict[str, Any],
) -> dict:
    """Insert or update the row for `owner_sub`. Returns the updated row.

    If the row exists and `if_match` does not equal its current etag, raises
    StaleEtag. If the row does not exist, `if_match` must be None.
    """
    # Validate inputs.
    if "status" in patch and patch["status"] not in VALID_STATUSES:
        raise ValueError(f"invalid status: {patch['status']}")
    if "persona" in patch and patch["persona"] is not None and patch["persona"] not in VALID_PERSONAS:
        raise ValueError(f"invalid persona: {patch['persona']}")

    cur = conn.cursor(row_factory=dict_row)
    existing = cur.execute(
        "SELECT * FROM onboarding_state WHERE owner_sub = %s FOR UPDATE",
        (owner_sub,),
    ).fetchone()

    if existing is None:
        if if_match is not None:
            raise StaleEtag("row does not exist; if_match must be None")
        # Insert.
        merged_answers = patch.get("answers", {})
        row = cur.execute(
            """
            INSERT INTO onboarding_state
                (owner_sub, status, current_step, persona, answers, launched_run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                owner_sub,
                patch.get("status", "in_progress"),
                patch.get("current_step", "welcome"),
                patch.get("persona"),
                Jsonb(merged_answers),
                patch.get("launched_run_id"),
            ),
        ).fetchone()
    else:
        current_etag = _etag_for(existing["updated_at"])
        if if_match is not None and if_match != current_etag:
            raise StaleEtag(f"if_match={if_match!r} but current={current_etag!r}")
        # Merge answers shallowly.
        new_answers = dict(existing["answers"] or {})
        if "answers" in patch:
            new_answers.update(patch["answers"])
        row = cur.execute(
            """
            UPDATE onboarding_state SET
                status         = COALESCE(%s, status),
                current_step   = COALESCE(%s, current_step),
                persona        = COALESCE(%s, persona),
                answers        = %s,
                launched_run_id = COALESCE(%s, launched_run_id),
                updated_at     = now()
            WHERE owner_sub = %s
            RETURNING *
            """,
            (
                patch.get("status"),
                patch.get("current_step"),
                patch.get("persona"),
                Jsonb(new_answers),
                patch.get("launched_run_id"),
                owner_sub,
            ),
        ).fetchone()

    conn.commit()
    return _row_to_dict(row)


def delete_state(conn: Connection, owner_sub: str) -> None:
    conn.execute(
        "DELETE FROM onboarding_state WHERE owner_sub = %s",
        (owner_sub,),
    )
    conn.commit()


def set_launched_run(conn: Connection, owner_sub: str, *, run_id: str) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        """
        UPDATE onboarding_state
        SET launched_run_id = %s, status = 'launched', updated_at = now()
        WHERE owner_sub = %s
        RETURNING *
        """,
        (run_id, owner_sub),
    ).fetchone()
    if row is None:
        raise KeyError(owner_sub)
    conn.commit()
    return _row_to_dict(row)
