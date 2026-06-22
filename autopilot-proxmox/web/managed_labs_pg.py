"""PostgreSQL store for managed lab profiles and reconciliation state."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


LAB_STATUSES = ("draft", "reserving", "validating", "fixing", "ready", "blocked", "archived")
OWNERSHIPS = ("attached", "adopting", "managed")
SOURCES = ("created", "adopted", "attached")
PROVIDERS = ("proxmox", "network", "ad", "entra", "intune", "deployment")

SCHEMA = """
CREATE TABLE IF NOT EXISTS labs (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    slug text NOT NULL UNIQUE,
    short_code text NOT NULL,
    group_tag text NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    network_cidr text NOT NULL DEFAULT '',
    gateway_ip text NOT NULL DEFAULT '',
    network_mode text NOT NULL DEFAULT 'sdn',
    sdn_zone text NOT NULL DEFAULT '',
    sdn_vnet text NOT NULL DEFAULT '',
    sdn_subnet text NOT NULL DEFAULT '',
    retry_count integer NOT NULL DEFAULT 0,
    last_reconcile_run_id uuid NULL,
    desired_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_boundaries (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    provider text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    ownership text NOT NULL,
    source text NOT NULL,
    provider_ids_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    desired_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    actual_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    baseline_snapshot_id uuid NULL,
    last_reconcile_status text NOT NULL DEFAULT 'unknown',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_boundaries_lab_provider ON lab_boundaries(lab_id, provider, kind);

CREATE TABLE IF NOT EXISTS lab_boundary_objects (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    boundary_id uuid NOT NULL REFERENCES lab_boundaries(id) ON DELETE CASCADE,
    provider text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    ownership text NOT NULL,
    source text NOT NULL,
    provider_ids_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    desired_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    actual_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    baseline_snapshot_id uuid NULL,
    last_reconcile_status text NOT NULL DEFAULT 'unknown',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_boundary_objects_lab_provider ON lab_boundary_objects(lab_id, provider, kind);

CREATE TABLE IF NOT EXISTS lab_reservations (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    reservation_type text NOT NULL,
    value text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE(reservation_type, value)
);

CREATE TABLE IF NOT EXISTS lab_reconcile_runs (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'running',
    attempt integer NOT NULL DEFAULT 1,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NULL,
    summary text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lab_reconcile_findings (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    reconcile_run_id uuid NULL REFERENCES lab_reconcile_runs(id) ON DELETE SET NULL,
    provider text NOT NULL,
    finding_type text NOT NULL,
    severity text NOT NULL,
    status text NOT NULL DEFAULT 'open',
    detail text NOT NULL DEFAULT '',
    object_ref_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    desired_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    actual_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_findings_lab_status ON lab_reconcile_findings(lab_id, status, severity);

CREATE TABLE IF NOT EXISTS lab_fix_actions (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    reconcile_run_id uuid NULL REFERENCES lab_reconcile_runs(id) ON DELETE SET NULL,
    provider text NOT NULL,
    action_type text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    priority integer NOT NULL DEFAULT 100,
    detail text NOT NULL DEFAULT '',
    request_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot_id uuid NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_fix_actions_lab_status ON lab_fix_actions(lab_id, status, priority);

CREATE TABLE IF NOT EXISTS lab_approval_requests (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    fix_action_id uuid NULL REFERENCES lab_fix_actions(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'not_required',
    reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_provider_snapshots (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    provider text NOT NULL,
    snapshot_type text NOT NULL,
    object_ref_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_events (
    id bigserial PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    actor text NOT NULL,
    detail text NOT NULL DEFAULT '',
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_events_lab_created ON lab_events(lab_id, created_at DESC);

CREATE TABLE IF NOT EXISTS lab_secret_refs (
    id uuid PRIMARY KEY,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    secret_type text NOT NULL,
    label text NOT NULL,
    provider_ref text NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS lab_secret_refs CASCADE;
DROP TABLE IF EXISTS lab_events CASCADE;
DROP TABLE IF EXISTS lab_provider_snapshots CASCADE;
DROP TABLE IF EXISTS lab_approval_requests CASCADE;
DROP TABLE IF EXISTS lab_fix_actions CASCADE;
DROP TABLE IF EXISTS lab_reconcile_findings CASCADE;
DROP TABLE IF EXISTS lab_reconcile_runs CASCADE;
DROP TABLE IF EXISTS lab_reservations CASCADE;
DROP TABLE IF EXISTS lab_boundary_objects CASCADE;
DROP TABLE IF EXISTS lab_boundaries CASCADE;
DROP TABLE IF EXISTS labs CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or f"lab-{uuid.uuid4().hex[:8]}"


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


def _map_json_fields(row: dict | None, *fields: str) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in (
        "id",
        "lab_id",
        "boundary_id",
        "reconcile_run_id",
        "fix_action_id",
        "snapshot_id",
        "baseline_snapshot_id",
        "last_reconcile_run_id",
    ):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at", "started_at", "finished_at", "completed_at"):
        if key in out:
            out[key] = _iso(out.get(key))
    for key in fields:
        json_key = f"{key}_json"
        if json_key in out:
            out[key] = _json_value(out.pop(json_key)) or {}
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


def create_lab(
    conn: Connection,
    *,
    name: str,
    short_code: str,
    group_tag: str,
    network_cidr: str,
    gateway_ip: str = "",
    network_mode: str = "sdn",
    sdn_zone: str = "",
    sdn_vnet: str = "",
    sdn_subnet: str = "",
) -> dict:
    now = _now()
    lab_id = _new_id()
    desired_state = {
        "naming_policy": "{lab_short}-{role}-{index}",
        "network": {
            "mode": network_mode,
            "cidr": network_cidr,
            "gateway_ip": gateway_ip,
            "sdn_zone": sdn_zone,
            "sdn_vnet": sdn_vnet,
            "sdn_subnet": sdn_subnet or network_cidr,
        },
        "identity": {
            "m365_mode": "modeled",
            "ad_mode": "modeled",
        },
    }
    row = conn.execute(
        """
        INSERT INTO labs (
            id, name, slug, short_code, group_tag, network_cidr, gateway_ip,
            network_mode, sdn_zone, sdn_vnet, sdn_subnet, desired_state_json,
            created_at, updated_at
        )
        VALUES (
            %(id)s, %(name)s, %(slug)s, %(short_code)s, %(group_tag)s,
            %(network_cidr)s, %(gateway_ip)s, %(network_mode)s, %(sdn_zone)s,
            %(sdn_vnet)s, %(sdn_subnet)s, %(desired_state)s, %(created_at)s, %(updated_at)s
        )
        RETURNING *
        """,
        {
            "id": lab_id,
            "name": name.strip(),
            "slug": _slug(name),
            "short_code": short_code.strip().lower(),
            "group_tag": group_tag.strip(),
            "network_cidr": network_cidr.strip(),
            "gateway_ip": gateway_ip.strip(),
            "network_mode": network_mode.strip() or "sdn",
            "sdn_zone": sdn_zone.strip(),
            "sdn_vnet": sdn_vnet.strip(),
            "sdn_subnet": (sdn_subnet or network_cidr).strip(),
            "desired_state": Jsonb(desired_state),
            "created_at": now,
            "updated_at": now,
        },
    ).fetchone()
    record_event(conn, lab_id=lab_id, event_type="lab_created", actor="system", detail=f"Created lab {name}")
    conn.commit()
    mapped = _map_json_fields(row, "desired_state")
    assert mapped is not None
    return mapped


def get_lab(conn: Connection, lab_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM labs WHERE id = %s", (lab_id,)).fetchone()
    return _map_json_fields(row, "desired_state")


def list_labs(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM labs ORDER BY created_at DESC, name ASC").fetchall()
    return [mapped for row in rows if (mapped := _map_json_fields(row, "desired_state")) is not None]


def create_boundary(
    conn: Connection,
    *,
    lab_id: str,
    provider: str,
    kind: str,
    name: str,
    ownership: str,
    source: str,
    desired_state: dict,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_boundaries (
            id, lab_id, provider, kind, name, ownership, source,
            desired_state_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (_new_id(), lab_id, provider, kind, name, ownership, source, Jsonb(desired_state), now, now),
    ).fetchone()
    conn.commit()
    mapped = _map_json_fields(row, "provider_ids", "desired_state", "actual_state")
    assert mapped is not None
    return mapped


def create_boundary_object(
    conn: Connection,
    *,
    lab_id: str,
    boundary_id: str,
    provider: str,
    kind: str,
    name: str,
    ownership: str,
    source: str,
    desired_state: dict,
    provider_ids: dict | None = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_boundary_objects (
            id, lab_id, boundary_id, provider, kind, name, ownership, source,
            provider_ids_json, desired_state_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            lab_id,
            boundary_id,
            provider,
            kind,
            name,
            ownership,
            source,
            Jsonb(provider_ids or {}),
            Jsonb(desired_state),
            now,
            now,
        ),
    ).fetchone()
    conn.commit()
    mapped = _map_json_fields(row, "provider_ids", "desired_state", "actual_state")
    assert mapped is not None
    return mapped


def record_event(
    conn: Connection,
    *,
    lab_id: str,
    event_type: str,
    actor: str,
    detail: str = "",
    payload: dict | None = None,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO lab_events (lab_id, event_type, actor, detail, payload_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (lab_id, event_type, actor, detail, Jsonb(payload or {}), _now()),
    ).fetchone()
    mapped = _map_json_fields(row, "payload")
    assert mapped is not None
    return mapped


def _recent_events(conn: Connection, lab_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lab_events WHERE lab_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
        (lab_id, limit),
    ).fetchall()
    return [mapped for row in rows if (mapped := _map_json_fields(row, "payload")) is not None]


def page_payload(conn: Connection, selected_lab_id: str | None = None) -> dict:
    labs = list_labs(conn)
    selected = get_lab(conn, selected_lab_id) if selected_lab_id else (labs[0] if labs else None)
    lab_id = selected["id"] if selected else ""
    return {
        "labs": labs,
        "selected_lab": selected,
        "boundaries": [],
        "boundary_objects": [],
        "reservations": [],
        "reconcile_runs": [],
        "findings": [],
        "fix_actions": [],
        "events": _recent_events(conn, lab_id) if lab_id else [],
    }
