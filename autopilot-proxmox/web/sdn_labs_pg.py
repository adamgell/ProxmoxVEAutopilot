from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


EGRESS_POLICIES = {"open", "restricted", "dark"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_sdn_bindings (
    bubble_id uuid PRIMARY KEY REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    zone text NOT NULL,
    vnet text NOT NULL,
    subnet text NOT NULL DEFAULT '',
    egress_policy text NOT NULL DEFAULT 'open',
    snat_enabled boolean NOT NULL DEFAULT true,
    firewall_profile text NOT NULL DEFAULT 'isolated_open_egress',
    last_apply_state text NOT NULL DEFAULT 'not_applied',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_sdn_audit_events (
    id bigserial PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    actor text NOT NULL,
    action text NOT NULL,
    old_values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    new_values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_sdn_audit_bubble_created
    ON lab_sdn_audit_events(bubble_id, created_at DESC, id DESC);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS lab_sdn_audit_events CASCADE;
DROP TABLE IF EXISTS lab_sdn_bindings CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _binding_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("bubble_id") is not None:
        out["bubble_id"] = str(out["bubble_id"])
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _audit_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    if out.get("bubble_id") is not None:
        out["bubble_id"] = str(out["bubble_id"])
    out["old_values"] = _json_value(out.pop("old_values_json")) or {}
    out["new_values"] = _json_value(out.pop("new_values_json")) or {}
    out["created_at"] = _iso(out.get("created_at"))
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


def get_binding(conn: Connection, bubble_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM lab_sdn_bindings WHERE bubble_id = %s",
        (bubble_id,),
    ).fetchone()
    return _binding_row(row)


def _record_audit(
    conn: Connection,
    *,
    bubble_id: str,
    actor: str,
    action: str,
    old_values: dict | None,
    new_values: dict | None,
) -> None:
    conn.execute(
        """
        INSERT INTO lab_sdn_audit_events (
            bubble_id, actor, action, old_values_json, new_values_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            bubble_id,
            (actor or "operator").strip() or "operator",
            action,
            Jsonb(old_values or {}),
            Jsonb(new_values or {}),
            _now(),
        ),
    )


def upsert_binding(
    conn: Connection,
    *,
    bubble_id: str,
    zone: str,
    vnet: str,
    subnet: str = "",
    egress_policy: str = "open",
    snat_enabled: bool = True,
    firewall_profile: str = "isolated_open_egress",
    actor: str = "operator",
) -> dict:
    egress_policy = (egress_policy or "open").strip().lower()
    if egress_policy not in EGRESS_POLICIES:
        raise ValueError(f"Unsupported SDN lab egress policy: {egress_policy}")
    old = get_binding(conn, bubble_id)
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_sdn_bindings (
            bubble_id, zone, vnet, subnet, egress_policy, snat_enabled,
            firewall_profile, last_apply_state, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'not_applied', %s, %s)
        ON CONFLICT (bubble_id) DO UPDATE SET
            zone = EXCLUDED.zone,
            vnet = EXCLUDED.vnet,
            subnet = EXCLUDED.subnet,
            egress_policy = EXCLUDED.egress_policy,
            snat_enabled = EXCLUDED.snat_enabled,
            firewall_profile = EXCLUDED.firewall_profile,
            updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (
            bubble_id,
            zone.strip(),
            vnet.strip(),
            subnet.strip(),
            egress_policy,
            bool(snat_enabled),
            (firewall_profile or "isolated_open_egress").strip() or "isolated_open_egress",
            now,
            now,
        ),
    ).fetchone()
    binding = _binding_row(row)
    _record_audit(
        conn,
        bubble_id=bubble_id,
        actor=actor,
        action="sdn_binding_upserted",
        old_values=old,
        new_values=binding,
    )
    conn.commit()
    return binding


def delete_binding(conn: Connection, bubble_id: str, *, actor: str = "operator") -> bool:
    old = get_binding(conn, bubble_id)
    if not old:
        return False
    conn.execute("DELETE FROM lab_sdn_bindings WHERE bubble_id = %s", (bubble_id,))
    _record_audit(
        conn,
        bubble_id=bubble_id,
        actor=actor,
        action="sdn_binding_deleted",
        old_values=old,
        new_values={},
    )
    conn.commit()
    return True


def list_audit_events(conn: Connection, bubble_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM lab_sdn_audit_events
        WHERE bubble_id = %s
        ORDER BY created_at DESC, id DESC
        """,
        (bubble_id,),
    ).fetchall()
    return [_audit_row(row) for row in rows]
