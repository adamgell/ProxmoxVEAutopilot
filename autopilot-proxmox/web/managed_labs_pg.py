"""PostgreSQL store for managed lab profiles and reconciliation state."""
from __future__ import annotations

import ipaddress
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


LAB_STATUSES = ("draft", "reserving", "validating", "fixing", "ready", "blocked", "archived")
OWNERSHIPS = ("attached", "adopting", "managed")
SOURCES = ("created", "adopted", "attached")
PROVIDERS = ("proxmox", "network", "ad", "entra", "intune", "deployment")

LAB_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "standard-hybrid-lab",
        "name": "Standard hybrid lab",
        "summary": "Desktop and server lab with managed Proxmox network, AD, Entra, and Intune intent.",
        "defaults": {
            "name": "Standard Hybrid Lab",
            "short_code": "lab01",
            "group_tag": "LAB01-Managed",
            "network_cidr": "10.50.20.0/24",
            "gateway_ip": "10.50.20.1",
            "sdn_zone": "lab-lab01",
            "sdn_vnet": "lab01-vnet",
            "desktop_count": 2,
            "server_count": 1,
            "naming_policy": "{lab_short}-{role}-{index}",
        },
        "intent": {
            "deployment_model": "hybrid",
            "identity": {"m365_mode": "managed", "ad_mode": "managed"},
            "intune": {"enrollment_status_profile": "managed", "app_assignments": "managed"},
            "ad": {"ou": "managed", "gpo": "managed", "users_day0": "managed"},
        },
    },
    {
        "id": "cloud-desktop-lab",
        "name": "Cloud desktop lab",
        "summary": "Cloud-first desktop lab with Proxmox networking and managed Entra/Intune intent.",
        "defaults": {
            "name": "Cloud Desktop Lab",
            "short_code": "cld01",
            "group_tag": "CLD01-Managed",
            "network_cidr": "10.50.30.0/24",
            "gateway_ip": "10.50.30.1",
            "sdn_zone": "lab-cld01",
            "sdn_vnet": "cld01-vnet",
            "desktop_count": 3,
            "server_count": 0,
            "naming_policy": "{lab_short}-{role}-{index}",
        },
        "intent": {
            "deployment_model": "cloud_only",
            "identity": {"m365_mode": "managed", "ad_mode": "attached"},
            "intune": {"enrollment_status_profile": "managed", "app_assignments": "managed"},
            "ad": {"ou": "left_alone", "gpo": "left_alone", "users_day0": "managed"},
        },
    },
    {
        "id": "server-validation-lab",
        "name": "Server validation lab",
        "summary": "Mixed server and workstation lab for OSDeploy validation with managed network intent.",
        "defaults": {
            "name": "Server Validation Lab",
            "short_code": "srv01",
            "group_tag": "SRV01-Managed",
            "network_cidr": "10.50.40.0/24",
            "gateway_ip": "10.50.40.1",
            "sdn_zone": "lab-srv01",
            "sdn_vnet": "srv01-vnet",
            "desktop_count": 1,
            "server_count": 2,
            "naming_policy": "{lab_short}-{role}-{index}",
        },
        "intent": {
            "deployment_model": "hybrid_server",
            "identity": {"m365_mode": "managed", "ad_mode": "managed"},
            "intune": {"enrollment_status_profile": "managed", "app_assignments": "managed"},
            "ad": {"ou": "managed", "gpo": "managed", "users_day0": "managed"},
        },
    },
)


def list_lab_templates() -> list[dict[str, Any]]:
    return deepcopy(list(LAB_TEMPLATES))


def get_lab_template(template_id: str | None) -> dict[str, Any] | None:
    candidate = str(template_id or "").strip()
    if not candidate:
        return None
    for template in LAB_TEMPLATES:
        if template["id"] == candidate:
            return deepcopy(template)
    raise ValueError(f"unknown lab template: {candidate}")

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


def _lab_network_shape(lab: dict[str, Any]) -> dict[str, str]:
    zone = str(lab.get("sdn_zone") or "").strip() or f"lab-{str(lab.get('short_code') or '').strip().lower()}"
    vnet = str(lab.get("sdn_vnet") or "").strip() or f"{str(lab.get('short_code') or '').strip().lower()}-vnet"
    subnet = str(lab.get("sdn_subnet") or "").strip() or str(lab.get("network_cidr") or "").strip()
    return {"zone": zone, "vnet": vnet, "subnet": subnet}


def _find_boundary(conn: Connection, *, lab_id: str, provider: str, kind: str, name: str) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM lab_boundaries
        WHERE lab_id = %s AND provider = %s AND kind = %s AND name = %s
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (lab_id, provider, kind, name),
    ).fetchone()
    return _map_json_fields(row, "provider_ids", "desired_state", "actual_state")


def _find_boundary_object(
    conn: Connection,
    *,
    lab_id: str,
    provider: str,
    kind: str,
    name: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM lab_boundary_objects
        WHERE lab_id = %s AND provider = %s AND kind = %s AND name = %s
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (lab_id, provider, kind, name),
    ).fetchone()
    return _map_json_fields(row, "provider_ids", "desired_state", "actual_state")


def _replace_boundary_object_provider_identities(
    conn: Connection,
    *,
    boundary_object_id: str,
    lab_id: str,
    provider: str,
    kind: str,
    provider_ids: dict | None,
) -> None:
    conn.execute(
        "DELETE FROM lab_boundary_object_provider_identities WHERE boundary_object_id = %s",
        (boundary_object_id,),
    )
    for identity_row in _provider_identity_rows(
        boundary_object_id=boundary_object_id,
        lab_id=lab_id,
        provider=provider,
        kind=kind,
        provider_ids=provider_ids,
    ):
        conn.execute(
            """
            INSERT INTO lab_boundary_object_provider_identities (
                boundary_object_id, lab_id, provider, kind, identity_key, identity_value
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            identity_row,
        )


def upsert_boundary(
    conn: Connection,
    *,
    lab_id: str,
    provider: str,
    kind: str,
    name: str,
    ownership: str,
    source: str,
    desired_state: dict | None = None,
    actual_state: dict | None = None,
    provider_ids: dict | None = None,
    baseline_snapshot_id: str | None = None,
    last_reconcile_status: str = "unknown",
    commit: bool = True,
) -> dict:
    existing = _find_boundary(conn, lab_id=lab_id, provider=provider, kind=kind, name=name)
    now = _now()
    if existing is None:
        row = conn.execute(
            """
            INSERT INTO lab_boundaries (
                id, lab_id, provider, kind, name, ownership, source,
                provider_ids_json, desired_state_json, actual_state_json,
                baseline_snapshot_id, last_reconcile_status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                _new_id(),
                lab_id,
                provider,
                kind,
                name,
                ownership,
                source,
                Jsonb(provider_ids or {}),
                Jsonb(desired_state or {}),
                Jsonb(actual_state or {}),
                baseline_snapshot_id,
                last_reconcile_status,
                now,
                now,
            ),
        ).fetchone()
    else:
        row = conn.execute(
            """
            UPDATE lab_boundaries
            SET ownership = %s,
                source = %s,
                provider_ids_json = %s,
                desired_state_json = %s,
                actual_state_json = %s,
                baseline_snapshot_id = COALESCE(%s, baseline_snapshot_id),
                last_reconcile_status = %s,
                updated_at = %s
            WHERE id = %s
            RETURNING *
            """,
            (
                ownership,
                source,
                Jsonb(provider_ids or {}),
                Jsonb(desired_state or {}),
                Jsonb(actual_state or {}),
                baseline_snapshot_id,
                last_reconcile_status,
                now,
                existing["id"],
            ),
        ).fetchone()
    if commit:
        conn.commit()
    mapped = _map_json_fields(row, "provider_ids", "desired_state", "actual_state")
    assert mapped is not None
    return mapped


def upsert_boundary_object(
    conn: Connection,
    *,
    lab_id: str,
    boundary_id: str,
    provider: str,
    kind: str,
    name: str,
    ownership: str,
    source: str,
    desired_state: dict | None = None,
    actual_state: dict | None = None,
    provider_ids: dict | None = None,
    baseline_snapshot_id: str | None = None,
    last_reconcile_status: str = "unknown",
    commit: bool = True,
) -> dict:
    existing = _find_boundary_object(conn, lab_id=lab_id, provider=provider, kind=kind, name=name)
    now = _now()
    if existing is None:
        object_id = _new_id()
        row = conn.execute(
            """
            INSERT INTO lab_boundary_objects (
                id, lab_id, boundary_id, provider, kind, name, ownership, source,
                provider_ids_json, desired_state_json, actual_state_json,
                baseline_snapshot_id, last_reconcile_status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                Jsonb(desired_state or {}),
                Jsonb(actual_state or {}),
                baseline_snapshot_id,
                last_reconcile_status,
                now,
                now,
            ),
        ).fetchone()
    else:
        object_id = existing["id"]
        row = conn.execute(
            """
            UPDATE lab_boundary_objects
            SET boundary_id = %s,
                ownership = %s,
                source = %s,
                provider_ids_json = %s,
                desired_state_json = %s,
                actual_state_json = %s,
                baseline_snapshot_id = COALESCE(%s, baseline_snapshot_id),
                last_reconcile_status = %s,
                updated_at = %s
            WHERE id = %s
            RETURNING *
            """,
            (
                boundary_id,
                ownership,
                source,
                Jsonb(provider_ids or {}),
                Jsonb(desired_state or {}),
                Jsonb(actual_state or {}),
                baseline_snapshot_id,
                last_reconcile_status,
                now,
                object_id,
            ),
        ).fetchone()
    _replace_boundary_object_provider_identities(
        conn,
        boundary_object_id=object_id,
        lab_id=lab_id,
        provider=provider,
        kind=kind,
        provider_ids=provider_ids,
    )
    if commit:
        conn.commit()
    mapped = _map_json_fields(row, "provider_ids", "desired_state", "actual_state")
    assert mapped is not None
    return mapped


def ensure_lab_boundary_model(conn: Connection, *, lab: dict[str, Any], commit: bool = True) -> None:
    network = _lab_network_shape(lab)
    naming_policy = str((lab.get("desired_state") or {}).get("naming_policy") or "{lab_short}-{role}-{index}")

    network_boundary = upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="network",
        kind="reservation",
        name=f"{lab['name']} Network",
        ownership="managed",
        source="created",
        desired_state={
            "cidr": lab["network_cidr"],
            "gateway_ip": lab["gateway_ip"],
            "mode": lab["network_mode"],
        },
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=network_boundary["id"],
        provider="network",
        kind="subnet",
        name=lab["network_cidr"],
        ownership="managed",
        source="created",
        provider_ids={"cidr": lab["network_cidr"]},
        desired_state={"cidr": lab["network_cidr"], "gateway_ip": lab["gateway_ip"]},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )

    proxmox_boundary = upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="proxmox",
        kind="network",
        name=f"{lab['name']} SDN",
        ownership="managed",
        source="created",
        desired_state={
            "mode": lab["network_mode"],
            "zone": network["zone"],
            "vnet": network["vnet"],
            "subnet": network["subnet"],
            "gateway": lab["gateway_ip"],
        },
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=proxmox_boundary["id"],
        provider="proxmox",
        kind="sdn_zone",
        name=network["zone"],
        ownership="managed",
        source="created",
        provider_ids={"zone": network["zone"]},
        desired_state={"zone": network["zone"], "type": "simple"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=proxmox_boundary["id"],
        provider="proxmox",
        kind="sdn_vnet",
        name=network["vnet"],
        ownership="managed",
        source="created",
        provider_ids={"vnet": network["vnet"]},
        desired_state={"vnet": network["vnet"], "zone": network["zone"], "alias": lab["name"]},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=proxmox_boundary["id"],
        provider="proxmox",
        kind="sdn_subnet",
        name=network["subnet"],
        ownership="managed",
        source="created",
        provider_ids={"vnet": network["vnet"], "subnet": network["subnet"]},
        desired_state={
            "vnet": network["vnet"],
            "subnet": network["subnet"],
            "gateway": lab["gateway_ip"],
            "snat": True,
        },
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )

    ad_boundary = upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="ad",
        kind="directory",
        name=f"{lab['name']} AD",
        ownership="managed",
        source="created",
        desired_state={"group_tag": lab["group_tag"], "mode": "modeled"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=ad_boundary["id"],
        provider="ad",
        kind="group",
        name=lab["group_tag"],
        ownership="managed",
        source="created",
        provider_ids={"group_tag": lab["group_tag"]},
        desired_state={"group_tag": lab["group_tag"], "mode": "modeled"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )

    entra_boundary = upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="entra",
        kind="identity",
        name=f"{lab['name']} Entra",
        ownership="managed",
        source="created",
        desired_state={"group_tag": lab["group_tag"], "mode": "modeled"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=entra_boundary["id"],
        provider="entra",
        kind="device_group",
        name=lab["group_tag"],
        ownership="managed",
        source="created",
        provider_ids={"group_tag": lab["group_tag"]},
        desired_state={"group_tag": lab["group_tag"], "mode": "modeled"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )

    intune_boundary = upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="intune",
        kind="endpoint_management",
        name=f"{lab['name']} Intune",
        ownership="managed",
        source="created",
        desired_state={"group_tag": lab["group_tag"], "mode": "modeled"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=intune_boundary["id"],
        provider="intune",
        kind="autopilot_profile",
        name=lab["group_tag"],
        ownership="managed",
        source="created",
        provider_ids={"group_tag": lab["group_tag"]},
        desired_state={"group_tag": lab["group_tag"], "mode": "modeled"},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )

    deployment_boundary = upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="deployment",
        kind="naming",
        name=f"{lab['name']} Naming",
        ownership="managed",
        source="created",
        desired_state={"pattern": naming_policy, "short_code": lab["short_code"]},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=deployment_boundary["id"],
        provider="deployment",
        kind="naming_policy",
        name=f"{lab['short_code']}-naming-policy",
        ownership="managed",
        source="created",
        provider_ids={"short_code": lab["short_code"]},
        desired_state={"pattern": naming_policy, "short_code": lab["short_code"]},
        actual_state={},
        last_reconcile_status="unknown",
        commit=False,
    )

    if commit:
        conn.commit()


def sync_lab_network_current_state(
    conn: Connection,
    *,
    lab: dict[str, Any],
    inventory: dict[str, Any],
    status: str,
    commit: bool = True,
) -> None:
    ensure_lab_boundary_model(conn, lab=lab, commit=False)
    network = _lab_network_shape(lab)
    zone_row = next((row for row in inventory.get("zones", []) if str(row.get("zone") or row.get("id") or "").strip() == network["zone"]), {})
    vnet_row = next((row for row in inventory.get("vnets", []) if str(row.get("vnet") or row.get("id") or "").strip() == network["vnet"]), {})
    subnet_row = next(
        (
            row
            for row in (inventory.get("subnets_by_vnet", {}) or {}).get(network["vnet"], [])
            if str(row.get("subnet") or row.get("id") or "").strip() == network["subnet"]
        ),
        {},
    )

    network_boundary = _find_boundary(conn, lab_id=lab["id"], provider="network", kind="reservation", name=f"{lab['name']} Network")
    assert network_boundary is not None
    upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="network",
        kind="reservation",
        name=network_boundary["name"],
        ownership=network_boundary["ownership"],
        source=network_boundary["source"],
        desired_state=network_boundary["desired_state"],
        actual_state={"cidr": lab["network_cidr"], "gateway_ip": lab["gateway_ip"], "status": "active"},
        provider_ids=network_boundary.get("provider_ids") or {},
        baseline_snapshot_id=network_boundary.get("baseline_snapshot_id"),
        last_reconcile_status=status,
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=network_boundary["id"],
        provider="network",
        kind="subnet",
        name=lab["network_cidr"],
        ownership="managed",
        source="created",
        desired_state={"cidr": lab["network_cidr"], "gateway_ip": lab["gateway_ip"]},
        actual_state={"cidr": lab["network_cidr"], "gateway_ip": lab["gateway_ip"], "status": "active"},
        provider_ids={"cidr": lab["network_cidr"]},
        last_reconcile_status=status,
        commit=False,
    )

    proxmox_boundary = _find_boundary(conn, lab_id=lab["id"], provider="proxmox", kind="network", name=f"{lab['name']} SDN")
    assert proxmox_boundary is not None
    actual_state: dict[str, Any] = {}
    if zone_row:
        actual_state["zone"] = zone_row
    if vnet_row:
        actual_state["vnet"] = vnet_row
    if subnet_row:
        actual_state["subnet"] = subnet_row
    upsert_boundary(
        conn,
        lab_id=lab["id"],
        provider="proxmox",
        kind="network",
        name=proxmox_boundary["name"],
        ownership=proxmox_boundary["ownership"],
        source=proxmox_boundary["source"],
        desired_state=proxmox_boundary["desired_state"],
        actual_state=actual_state,
        provider_ids=proxmox_boundary.get("provider_ids") or {},
        baseline_snapshot_id=proxmox_boundary.get("baseline_snapshot_id"),
        last_reconcile_status=status,
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=proxmox_boundary["id"],
        provider="proxmox",
        kind="sdn_zone",
        name=network["zone"],
        ownership="managed",
        source="created",
        desired_state={"zone": network["zone"], "type": "simple"},
        actual_state=zone_row,
        provider_ids={"zone": network["zone"]},
        last_reconcile_status=status,
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=proxmox_boundary["id"],
        provider="proxmox",
        kind="sdn_vnet",
        name=network["vnet"],
        ownership="managed",
        source="created",
        desired_state={"vnet": network["vnet"], "zone": network["zone"], "alias": lab["name"]},
        actual_state=vnet_row,
        provider_ids={"vnet": network["vnet"]},
        last_reconcile_status=status,
        commit=False,
    )
    upsert_boundary_object(
        conn,
        lab_id=lab["id"],
        boundary_id=proxmox_boundary["id"],
        provider="proxmox",
        kind="sdn_subnet",
        name=network["subnet"],
        ownership="managed",
        source="created",
        desired_state={"vnet": network["vnet"], "subnet": network["subnet"], "gateway": lab["gateway_ip"], "snat": True},
        actual_state=subnet_row,
        provider_ids={"vnet": network["vnet"], "subnet": network["subnet"]},
        last_reconcile_status=status,
        commit=False,
    )

    if commit:
        conn.commit()



def sync_proxmox_network_actual_state(
    conn: Connection,
    *,
    lab_id: str,
    inventory: dict[str, Any],
    reconcile_status: str,
    commit: bool = True,
) -> None:
    lab = get_lab(conn, lab_id)
    if lab is None:
        raise ValueError(f"lab not found: {lab_id}")
    sync_lab_network_current_state(
        conn,
        lab=lab,
        inventory=inventory,
        status=reconcile_status,
        commit=commit,
    )

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
    template_id: str = "",
    desktop_count: int | None = None,
    server_count: int | None = None,
    commit: bool = True,
) -> dict:
    now = _now()
    lab_id = _new_id()
    template = get_lab_template(template_id)
    template_defaults = dict((template or {}).get("defaults") or {})
    template_intent = dict((template or {}).get("intent") or {})
    resolved_desktop_count = int(desktop_count if desktop_count is not None else template_defaults.get("desktop_count") or 0)
    resolved_server_count = int(server_count if server_count is not None else template_defaults.get("server_count") or 0)
    if resolved_desktop_count < 0 or resolved_server_count < 0:
        raise ValueError("device counts must be zero or greater")
    naming_policy = str(template_defaults.get("naming_policy") or "{lab_short}-{role}-{index}")
    desired_state = {
        "template_id": template["id"] if template else "",
        "template_name": template["name"] if template else "Custom lab",
        "naming_policy": naming_policy,
        "device_counts": {
            "desktop": resolved_desktop_count,
            "server": resolved_server_count,
        },
        "network": {
            "mode": network_mode,
            "cidr": network_cidr,
            "gateway_ip": gateway_ip,
            "sdn_zone": sdn_zone,
            "sdn_vnet": sdn_vnet,
            "sdn_subnet": sdn_subnet or network_cidr,
        },
        "deployment": {
            "model": template_intent.get("deployment_model", "custom"),
        },
        "identity": template_intent.get("identity") or {
            "m365_mode": "modeled",
            "ad_mode": "modeled",
        },
        "intune": template_intent.get("intune") or {
            "enrollment_status_profile": "modeled",
            "app_assignments": "modeled",
        },
        "ad": template_intent.get("ad") or {
            "ou": "modeled",
            "gpo": "modeled",
            "users_day0": "modeled",
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
    mapped = _map_json_fields(row, "desired_state")
    assert mapped is not None
    ensure_lab_boundary_model(conn, lab=mapped, commit=False)
    record_event(
        conn,
        lab_id=lab_id,
        event_type="lab_created",
        actor="system",
        detail=f"Created lab {name}",
        commit=False,
    )
    if commit:
        conn.commit()
    return mapped


def get_lab(conn: Connection, lab_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM labs WHERE id = %s", (lab_id,)).fetchone()
    return _map_json_fields(row, "desired_state")


def delete_lab(conn: Connection, lab_id: str) -> bool:
    row = conn.execute(
        "DELETE FROM labs WHERE id = %s RETURNING id",
        (lab_id,),
    ).fetchone()
    conn.commit()
    return row is not None


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
    commit: bool = True,
) -> dict:
    normalized_value = value.strip()
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_reservations (
            id, lab_id, reservation_type, value, metadata_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (reservation_type, value) DO UPDATE
        SET status = 'active',
            metadata_json = COALESCE(%s, lab_reservations.metadata_json),
            updated_at = EXCLUDED.updated_at
        WHERE lab_reservations.lab_id = EXCLUDED.lab_id
        RETURNING *
        """,
        (
            _new_id(),
            lab_id,
            reservation_type,
            normalized_value,
            Jsonb(metadata or {}),
            now,
            now,
            Jsonb(metadata) if metadata is not None else None,
        ),
    ).fetchone()
    if row is None:
        existing_row = conn.execute(
            "SELECT * FROM lab_reservations WHERE reservation_type = %s AND value = %s",
            (reservation_type, normalized_value),
        ).fetchone()
        existing = _map_json_fields(existing_row, "metadata")
        if existing is not None and existing["lab_id"] != lab_id:
            raise ValueError(
                f"reservation {reservation_type}:{normalized_value} is already reserved by another lab"
            )
        raise ValueError(f"reservation {reservation_type}:{normalized_value} could not be reserved")
    if commit:
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
    commit: bool = True,
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
                commit=False,
            )
        )
    if commit:
        conn.commit()
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


def _lab_status_from_reconcile_status(status: str, *, attempt: int) -> str:
    if status == "failed":
        return "blocked" if attempt >= 5 else "validating"
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
            _lab_status_from_reconcile_status(status, attempt=int(mapped["attempt"])),
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
    status: str = "open",
    commit: bool = True,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_reconcile_findings (
            id, lab_id, reconcile_run_id, provider, finding_type, severity, status, detail,
            object_ref_json, desired_state_json, actual_state_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            lab_id,
            reconcile_run_id,
            provider,
            finding_type,
            severity,
            status,
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
        payload={"finding_type": finding_type, "severity": severity, "status": status},
        commit=False,
    )
    if commit:
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
    commit: bool = True,
) -> dict:
    now = _now()
    existing = conn.execute(
        """
        SELECT * FROM lab_fix_actions
        WHERE lab_id = %s
          AND provider = %s
          AND action_type = %s
          AND status = 'pending'
          AND request_json = %s
        ORDER BY priority ASC, created_at ASC
        LIMIT 1
        """,
        (lab_id, provider, action_type, Jsonb(request)),
    ).fetchone()
    if existing is None:
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
    else:
        row = conn.execute(
            """
            UPDATE lab_fix_actions
            SET reconcile_run_id = %s,
                priority = %s,
                detail = %s,
                updated_at = %s
            WHERE id = %s
            RETURNING *
            """,
            (reconcile_run_id, priority, detail, now, existing["id"]),
        ).fetchone()
    record_event(
        conn,
        lab_id=lab_id,
        event_type="fix_action_created",
        actor="reconciler",
        detail=detail,
        payload={"action_type": action_type},
        commit=False,
    )
    if commit:
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


def list_current_fix_actions(conn: Connection, lab_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM lab_fix_actions
        WHERE lab_id = %s AND status IN ('pending', 'failed', 'blocked', 'running')
        ORDER BY priority ASC, created_at ASC
        """,
        (lab_id,),
    ).fetchall()
    return [
        mapped
        for row in rows
        if (mapped := _map_json_fields(row, "request", "result")) is not None
    ]


def clear_current_reconcile_state(
    conn: Connection,
    *,
    lab_id: str,
    providers: tuple[str, ...] = ("network", "proxmox"),
    commit: bool = True,
) -> None:
    now = _now()
    conn.execute(
        """
        UPDATE lab_reconcile_findings
        SET status = 'resolved', updated_at = %s
        WHERE lab_id = %s AND status = 'open' AND provider = ANY(%s)
        """,
        (now, lab_id, list(providers)),
    )
    conn.execute(
        """
        UPDATE lab_fix_actions
        SET status = 'superseded', updated_at = %s, completed_at = COALESCE(completed_at, %s)
        WHERE lab_id = %s
          AND status IN ('pending', 'failed', 'blocked', 'running')
          AND provider = ANY(%s)
        """,
        (now, now, lab_id, list(providers)),
    )
    if commit:
        conn.commit()


def resolve_open_finding(
    conn: Connection,
    *,
    lab_id: str,
    provider: str,
    finding_type: str,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        UPDATE lab_reconcile_findings
        SET status = 'resolved', updated_at = %s
        WHERE lab_id = %s AND provider = %s AND finding_type = %s AND status = 'open'
        """,
        (_now(), lab_id, provider, finding_type),
    )
    if commit:
        conn.commit()


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
    return upsert_boundary(
        conn,
        lab_id=lab_id,
        provider=provider,
        kind=kind,
        name=name,
        ownership=ownership,
        source=source,
        desired_state=desired_state,
    )

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
    return upsert_boundary_object(
        conn,
        lab_id=lab_id,
        boundary_id=boundary_id,
        provider=provider,
        kind=kind,
        name=name,
        ownership=ownership,
        source=source,
        desired_state=desired_state,
        provider_ids=provider_ids,
    )

def record_event(
    conn: Connection,
    *,
    lab_id: str,
    event_type: str,
    actor: str,
    detail: str = "",
    payload: dict | None = None,
    commit: bool = True,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO lab_events (lab_id, event_type, actor, detail, payload_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (lab_id, event_type, actor, detail, Jsonb(payload or {}), _now()),
    ).fetchone()
    if commit:
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
    selected = get_lab(conn, selected_lab_id) if selected_lab_id else None
    if selected is None and labs:
        selected = labs[0]
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
    fix_actions = list_current_fix_actions(conn, lab_id) if lab_id else []
    return {
        "templates": list_lab_templates(),
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
