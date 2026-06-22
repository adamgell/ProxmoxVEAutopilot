"""PostgreSQL store for managed lab profiles and reconciliation state."""
from __future__ import annotations

import ipaddress
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

CREATE TABLE IF NOT EXISTS lab_boundary_object_provider_identities (
    boundary_object_id uuid NOT NULL REFERENCES lab_boundary_objects(id) ON DELETE CASCADE,
    lab_id uuid NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    provider text NOT NULL,
    kind text NOT NULL,
    identity_key text NOT NULL,
    identity_value text NOT NULL,
    PRIMARY KEY (boundary_object_id, identity_key),
    UNIQUE (provider, kind, identity_key, identity_value)
);
CREATE INDEX IF NOT EXISTS idx_lab_boundary_object_provider_identities_lab
    ON lab_boundary_object_provider_identities(lab_id, provider, kind);

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
DROP TABLE IF EXISTS lab_boundary_object_provider_identities CASCADE;
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


def _normalize_provider_identity_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bool, int, float)):
        return str(value).strip()
    return ""


def _provider_identity_rows(
    *,
    boundary_object_id: str,
    lab_id: str,
    provider: str,
    kind: str,
    provider_ids: dict | None,
) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for raw_key, raw_value in (provider_ids or {}).items():
        identity_key = str(raw_key).strip()
        identity_value = _normalize_provider_identity_value(raw_value)
        if not identity_key or not identity_value:
            continue
        rows.append((boundary_object_id, lab_id, provider, kind, identity_key, identity_value))
    return rows


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


def _list_lab_rows(
    conn: Connection,
    *,
    table: str,
    lab_id: str,
    order_by: str,
    json_fields: tuple[str, ...] = (),
) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE lab_id = %s ORDER BY {order_by}",
        (lab_id,),
    ).fetchall()
    return [mapped for row in rows if (mapped := _map_json_fields(row, *json_fields)) is not None]


def reserve_value(
    conn: Connection,
    *,
    lab_id: str,
    reservation_type: str,
    value: str,
    metadata: dict | None = None,
) -> dict:
    normalized_value = value.strip()
    existing_row = conn.execute(
        "SELECT * FROM lab_reservations WHERE reservation_type = %s AND value = %s",
        (reservation_type, normalized_value),
    ).fetchone()
    existing = _map_json_fields(existing_row, "metadata")
    if existing is not None:
        if existing["lab_id"] != lab_id:
            raise ValueError(
                f"reservation {reservation_type}:{normalized_value} is already reserved by another lab"
            )
        now = _now()
        row = conn.execute(
            """
            UPDATE lab_reservations
            SET updated_at = %s
            WHERE id = %s
            RETURNING *
            """,
            (now, existing["id"]),
        ).fetchone()
    else:
        now = _now()
        row = conn.execute(
            """
            INSERT INTO lab_reservations (id, lab_id, reservation_type, value, metadata_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (_new_id(), lab_id, reservation_type, normalized_value, Jsonb(metadata or {}), now, now),
        ).fetchone()
    conn.commit()
    mapped = _map_json_fields(row, "metadata")
    assert mapped is not None
    return mapped


def reserve_default_names(
    conn: Connection,
    *,
    lab_id: str,
    short_code: str,
    role: str,
    count: int,
) -> list[dict]:
    rows: list[dict] = []
    prefix = f"{short_code.strip().lower()}-{role.strip().lower()}"
    for index in range(1, count + 1):
        value = f"{prefix}-{index:03d}"
        if len(value) > 15:
            raise ValueError(f"generated Windows hostname exceeds 15 characters: {value}")
        rows.append(
            reserve_value(
                conn,
                lab_id=lab_id,
                reservation_type="hostname",
                value=value,
                metadata={"role": role, "index": index},
            )
        )
    return rows


def find_overlapping_cidr_reservations(
    conn: Connection,
    cidr: str,
    exclude_lab_id: str | None = None,
) -> list[dict]:
    requested = ipaddress.ip_network(cidr, strict=False)
    rows = conn.execute(
        """
        SELECT * FROM lab_reservations
        WHERE reservation_type = 'cidr' AND status = 'active'
        ORDER BY created_at ASC
        """
    ).fetchall()
    overlaps: list[dict] = []
    for row in rows:
        mapped = _map_json_fields(row, "metadata")
        if mapped is None:
            continue
        if exclude_lab_id and mapped["lab_id"] == exclude_lab_id:
            continue
        existing = ipaddress.ip_network(mapped["value"], strict=False)
        if requested.overlaps(existing):
            overlaps.append(mapped)
    return overlaps


def start_reconcile_run(conn: Connection, *, lab_id: str, attempt: int) -> dict:
    now = _now()
    run_id = _new_id()
    row = conn.execute(
        """
        INSERT INTO lab_reconcile_runs (id, lab_id, status, attempt, started_at)
        VALUES (%s, %s, 'running', %s, %s)
        RETURNING *
        """,
        (run_id, lab_id, attempt, now),
    ).fetchone()
    conn.execute(
        "UPDATE labs SET status = 'validating', last_reconcile_run_id = %s, updated_at = %s WHERE id = %s",
        (run_id, now, lab_id),
    )
    record_event(
        conn,
        lab_id=lab_id,
        event_type="reconcile_started",
        actor="reconciler",
        payload={"run_id": run_id, "attempt": attempt},
    )
    conn.commit()
    mapped = _map_json_fields(row)
    assert mapped is not None
    return mapped


def _lab_status_from_reconcile_status(status: str) -> str:
    if status == "failed":
        return "blocked"
    if status in LAB_STATUSES:
        return status
    return "validating"



def finish_reconcile_run(conn: Connection, *, run_id: str, status: str, summary: str = "") -> dict:
    now = _now()
    row = conn.execute(
        """
        UPDATE lab_reconcile_runs
        SET status = %s, summary = %s, finished_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (status, summary, now, run_id),
    ).fetchone()
    mapped = _map_json_fields(row)
    assert mapped is not None
    conn.execute(
        """
        UPDATE labs
        SET status = %s,
            retry_count = %s,
            last_reconcile_run_id = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (
            _lab_status_from_reconcile_status(status),
            mapped["attempt"],
            mapped["id"],
            now,
            mapped["lab_id"],
        ),
    )
    record_event(
        conn,
        lab_id=mapped["lab_id"],
        event_type="reconcile_finished",
        actor="reconciler",
        detail=summary,
        payload={"run_id": run_id, "status": status},
    )
    conn.commit()
    return mapped


def record_finding(
    conn: Connection,
    *,
    lab_id: str,
    reconcile_run_id: str | None,
    provider: str,
    finding_type: str,
    severity: str,
    detail: str,
    object_ref: dict | None = None,
    desired_state: dict | None = None,
    actual_state: dict | None = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_reconcile_findings (
            id, lab_id, reconcile_run_id, provider, finding_type, severity, detail,
            object_ref_json, desired_state_json, actual_state_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            lab_id,
            reconcile_run_id,
            provider,
            finding_type,
            severity,
            detail,
            Jsonb(object_ref or {}),
            Jsonb(desired_state or {}),
            Jsonb(actual_state or {}),
            now,
            now,
        ),
    ).fetchone()
    record_event(
        conn,
        lab_id=lab_id,
        event_type="finding_recorded",
        actor="reconciler",
        detail=detail,
        payload={"finding_type": finding_type, "severity": severity},
    )
    conn.commit()
    mapped = _map_json_fields(row, "object_ref", "desired_state", "actual_state")
    assert mapped is not None
    return mapped


def create_fix_action(
    conn: Connection,
    *,
    lab_id: str,
    reconcile_run_id: str | None,
    provider: str,
    action_type: str,
    priority: int,
    detail: str,
    request: dict,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_fix_actions (
            id, lab_id, reconcile_run_id, provider, action_type, priority, detail, request_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (_new_id(), lab_id, reconcile_run_id, provider, action_type, priority, detail, Jsonb(request), now, now),
    ).fetchone()
    record_event(
        conn,
        lab_id=lab_id,
        event_type="fix_action_created",
        actor="reconciler",
        detail=detail,
        payload={"action_type": action_type},
    )
    conn.commit()
    mapped = _map_json_fields(row, "request", "result")
    assert mapped is not None
    return mapped


def get_fix_action(conn: Connection, fix_action_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM lab_fix_actions WHERE id = %s",
        (fix_action_id,),
    ).fetchone()
    return _map_json_fields(row, "request", "result")


def update_fix_action(
    conn: Connection,
    fix_action_id: str,
    *,
    status: str,
    result: dict | None = None,
    snapshot_id: str | None = None,
) -> dict:
    existing = get_fix_action(conn, fix_action_id)
    if existing is None:
        raise ValueError(f"fix action not found: {fix_action_id}")

    terminal_statuses = {"fixed", "failed", "blocked"}
    effective_snapshot_id = snapshot_id or existing.get("snapshot_id")
    if status in terminal_statuses and not effective_snapshot_id:
        raise ValueError("snapshot_id is required for terminal status")

    now = _now()
    row = conn.execute(
        """
        UPDATE lab_fix_actions
        SET status = %s,
            result_json = COALESCE(%s, result_json),
            snapshot_id = COALESCE(%s, snapshot_id),
            updated_at = %s,
            completed_at = CASE WHEN %s IN ('fixed', 'failed', 'blocked') THEN %s ELSE completed_at END
        WHERE id = %s
        RETURNING *
        """,
        (status, Jsonb(result) if result is not None else None, snapshot_id, now, status, now, fix_action_id),
    ).fetchone()
    mapped = _map_json_fields(row, "request", "result")
    assert mapped is not None
    record_event(
        conn,
        lab_id=mapped["lab_id"],
        event_type="fix_action_updated",
        actor="reconciler",
        detail=str(mapped["detail"]),
        payload={"action_type": mapped["action_type"], "status": status},
    )
    conn.commit()
    return mapped


def record_provider_snapshot(
    conn: Connection,
    *,
    lab_id: str,
    provider: str,
    snapshot_type: str,
    object_ref: dict,
    snapshot: dict,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO lab_provider_snapshots (id, lab_id, provider, snapshot_type, object_ref_json, snapshot_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (_new_id(), lab_id, provider, snapshot_type, Jsonb(object_ref), Jsonb(snapshot), _now()),
    ).fetchone()
    conn.commit()
    mapped = _map_json_fields(row, "object_ref", "snapshot")
    assert mapped is not None
    return mapped


def list_open_findings(conn: Connection, lab_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lab_reconcile_findings WHERE lab_id = %s AND status = 'open' ORDER BY created_at DESC",
        (lab_id,),
    ).fetchall()
    return [
        mapped
        for row in rows
        if (mapped := _map_json_fields(row, "object_ref", "desired_state", "actual_state")) is not None
    ]


def list_pending_fix_actions(conn: Connection, lab_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM lab_fix_actions
        WHERE lab_id = %s AND status = 'pending'
        ORDER BY priority ASC, created_at ASC
        """,
        (lab_id,),
    ).fetchall()
    return [
        mapped
        for row in rows
        if (mapped := _map_json_fields(row, "request", "result")) is not None
    ]


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
    object_id = _new_id()
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
            object_id,
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
    identity_rows = _provider_identity_rows(
        boundary_object_id=object_id,
        lab_id=lab_id,
        provider=provider,
        kind=kind,
        provider_ids=provider_ids,
    )
    for identity_row in identity_rows:
        conn.execute(
            """
            INSERT INTO lab_boundary_object_provider_identities (
                boundary_object_id, lab_id, provider, kind, identity_key, identity_value
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            identity_row,
        )
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
    conn.commit()
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
    boundaries = _list_lab_rows(
        conn,
        table="lab_boundaries",
        lab_id=lab_id,
        order_by="created_at DESC, name ASC",
        json_fields=("provider_ids", "desired_state", "actual_state"),
    ) if lab_id else []
    boundary_objects = _list_lab_rows(
        conn,
        table="lab_boundary_objects",
        lab_id=lab_id,
        order_by="created_at DESC, name ASC",
        json_fields=("provider_ids", "desired_state", "actual_state"),
    ) if lab_id else []
    reservations = _list_lab_rows(
        conn,
        table="lab_reservations",
        lab_id=lab_id,
        order_by="created_at DESC, reservation_type ASC, value ASC",
        json_fields=("metadata",),
    ) if lab_id else []
    reconcile_runs = _list_lab_rows(
        conn,
        table="lab_reconcile_runs",
        lab_id=lab_id,
        order_by="started_at DESC, id DESC",
    ) if lab_id else []
    findings = list_open_findings(conn, lab_id) if lab_id else []
    fix_actions = _list_lab_rows(
        conn,
        table="lab_fix_actions",
        lab_id=lab_id,
        order_by="priority ASC, created_at ASC",
        json_fields=("request", "result"),
    ) if lab_id else []
    return {
        "labs": labs,
        "selected_lab": selected,
        "boundaries": boundaries,
        "boundary_objects": boundary_objects,
        "reservations": reservations,
        "reconcile_runs": reconcile_runs,
        "findings": findings,
        "fix_actions": fix_actions,
        "events": _recent_events(conn, lab_id) if lab_id else [],
    }
