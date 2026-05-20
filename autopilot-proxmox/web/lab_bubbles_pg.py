"""PostgreSQL store for lab/tenant bubbles and their assets."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_bubbles (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    slug text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    lifecycle_state text NOT NULL DEFAULT 'planned',
    domain_name text NOT NULL DEFAULT '',
    netbios_name text NOT NULL DEFAULT '',
    cidr text NOT NULL DEFAULT '',
    gateway_ip text NOT NULL DEFAULT '',
    planned_bridge text NOT NULL DEFAULT '',
    planned_vlan integer NULL,
    isolation_status text NOT NULL DEFAULT 'planned',
    dhcp_scope text NOT NULL DEFAULT '',
    dhcp_pool_start text NOT NULL DEFAULT '',
    dhcp_pool_end text NOT NULL DEFAULT '',
    dhcp_owner_asset_id uuid NULL,
    dc_ready boolean NOT NULL DEFAULT false,
    dns_ready boolean NOT NULL DEFAULT false,
    dhcp_ready boolean NOT NULL DEFAULT false,
    workload_ready boolean NOT NULL DEFAULT false,
    allow_early_workgroup_launch boolean NOT NULL DEFAULT true,
    require_domain_ready boolean NOT NULL DEFAULT true,
    require_multi_domain_ready boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS lab_bubble_assets (
    id uuid PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    asset_type text NOT NULL,
    asset_role text NOT NULL,
    vmid integer NULL,
    vm_uuid text NULL,
    run_id uuid NULL,
    agent_id text NULL,
    service_id uuid NULL,
    membership_state text NOT NULL DEFAULT 'active',
    evidence_state text NOT NULL DEFAULT 'unknown',
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_bubble_role
    ON lab_bubble_assets(bubble_id, asset_role);
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_vmid
    ON lab_bubble_assets(vmid) WHERE vmid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_run
    ON lab_bubble_assets(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_agent
    ON lab_bubble_assets(agent_id) WHERE agent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS lab_bubble_services (
    id uuid PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    service_kind text NOT NULL,
    service_name text NOT NULL,
    scope text NOT NULL DEFAULT 'bubble_local',
    provider_asset_id uuid NULL REFERENCES lab_bubble_assets(id) ON DELETE SET NULL,
    consumer_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    readiness_state text NOT NULL DEFAULT 'unknown',
    last_evidence_at timestamptz NULL,
    evidence_summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_bubble_services_bubble_kind
    ON lab_bubble_services(bubble_id, service_kind);

CREATE TABLE IF NOT EXISTS lab_bubble_audit_events (
    id bigserial PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    asset_id uuid NULL REFERENCES lab_bubble_assets(id) ON DELETE SET NULL,
    actor text NOT NULL,
    action text NOT NULL,
    reason text NOT NULL DEFAULT '',
    old_values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    new_values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS lab_bubble_audit_events CASCADE;
DROP TABLE IF EXISTS lab_bubble_services CASCADE;
DROP TABLE IF EXISTS lab_bubble_assets CASCADE;
DROP TABLE IF EXISTS lab_bubbles CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or f"bubble-{uuid.uuid4().hex[:8]}"


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


def _row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("id", "dhcp_owner_asset_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _asset_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("id", "bubble_id", "run_id", "service_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _service_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("id", "bubble_id", "provider_asset_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    out["consumer_refs"] = _json_value(out.pop("consumer_refs_json")) or []
    out["evidence_summary"] = _json_value(out.pop("evidence_summary_json")) or {}
    for key in ("last_evidence_at", "created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _audit_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    for key in ("bubble_id", "asset_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
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


def create_bubble(
    conn: Connection,
    *,
    name: str,
    description: str = "",
    domain_name: str = "",
    netbios_name: str = "",
    cidr: str = "",
    gateway_ip: str = "",
    planned_bridge: str = "",
    planned_vlan: int | None = None,
    lifecycle_state: str = "planned",
    isolation_status: str = "planned",
    dhcp_scope: str = "",
    dhcp_pool_start: str = "",
    dhcp_pool_end: str = "",
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_bubbles (
            id, name, slug, description, domain_name, netbios_name, cidr,
            gateway_ip, planned_bridge, planned_vlan, lifecycle_state,
            isolation_status, dhcp_scope,
            dhcp_pool_start, dhcp_pool_end, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            name.strip(),
            _slug(name),
            description.strip(),
            domain_name.strip().lower(),
            netbios_name.strip().upper(),
            cidr.strip(),
            gateway_ip.strip(),
            planned_bridge.strip(),
            planned_vlan,
            lifecycle_state.strip() or "planned",
            isolation_status.strip() or "planned",
            dhcp_scope.strip(),
            dhcp_pool_start.strip(),
            dhcp_pool_end.strip(),
            now,
            now,
        ),
    ).fetchone()
    conn.commit()
    return _row(row)


def get_bubble(conn: Connection, bubble_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM lab_bubbles WHERE id = %s", (bubble_id,)).fetchone()
    return _row(row)


def list_bubbles(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM lab_bubbles ORDER BY name").fetchall()
    return [_row(row) for row in rows]


def update_bubble(conn: Connection, bubble_id: str, commit: bool = True, **fields: Any) -> dict:
    allowed = {
        "name",
        "description",
        "lifecycle_state",
        "domain_name",
        "netbios_name",
        "cidr",
        "gateway_ip",
        "planned_bridge",
        "planned_vlan",
        "isolation_status",
        "dhcp_scope",
        "dhcp_pool_start",
        "dhcp_pool_end",
        "dhcp_owner_asset_id",
        "dc_ready",
        "dns_ready",
        "dhcp_ready",
        "workload_ready",
        "allow_early_workgroup_launch",
        "require_domain_ready",
        "require_multi_domain_ready",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "name" in updates:
        updates["slug"] = _slug(str(updates["name"]))
    if "domain_name" in updates and updates["domain_name"] is not None:
        updates["domain_name"] = str(updates["domain_name"]).strip().lower()
    if "netbios_name" in updates and updates["netbios_name"] is not None:
        updates["netbios_name"] = str(updates["netbios_name"]).strip().upper()
    if not updates:
        current = get_bubble(conn, bubble_id)
        if current is None:
            raise ValueError("bubble not found")
        return current
    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{key} = %s" for key in updates)
    row = conn.execute(
        f"UPDATE lab_bubbles SET {set_sql} WHERE id = %s RETURNING *",
        list(updates.values()) + [bubble_id],
    ).fetchone()
    if row is None:
        raise ValueError("bubble not found")
    if commit:
        conn.commit()
    return _row(row)


def delete_bubble(conn: Connection, bubble_id: str) -> bool:
    result = conn.execute("DELETE FROM lab_bubbles WHERE id = %s", (bubble_id,))
    conn.commit()
    return result.rowcount > 0


def record_audit_event(
    conn: Connection,
    *,
    bubble_id: str,
    action: str,
    actor: str = "system",
    asset_id: str | None = None,
    reason: str = "",
    old_values: dict | None = None,
    new_values: dict | None = None,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO lab_bubble_audit_events (
            bubble_id, asset_id, actor, action, reason,
            old_values_json, new_values_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            bubble_id,
            asset_id,
            actor,
            action,
            reason,
            Jsonb(old_values or {}),
            Jsonb(new_values or {}),
            _now(),
        ),
    ).fetchone()
    return _audit_row(row)


def add_asset(
    conn: Connection,
    bubble_id: str,
    *,
    asset_type: str,
    asset_role: str,
    vmid: int | None = None,
    vm_uuid: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    service_id: str | None = None,
    membership_state: str = "active",
    evidence_state: str = "unknown",
    notes: str = "",
    actor: str = "system",
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_bubble_assets (
            id, bubble_id, asset_type, asset_role, vmid, vm_uuid, run_id,
            agent_id, service_id, membership_state, evidence_state, notes,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            bubble_id,
            asset_type,
            asset_role,
            vmid,
            vm_uuid,
            run_id or None,
            agent_id,
            service_id or None,
            membership_state,
            evidence_state,
            notes,
            now,
            now,
        ),
    ).fetchone()
    asset = _asset_row(row)
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        asset_id=asset["id"],
        action="asset_added",
        actor=actor,
        new_values=asset,
    )
    conn.commit()
    return asset


def list_assets(conn: Connection, bubble_id: str | None = None) -> list[dict]:
    if bubble_id:
        rows = conn.execute(
            """
            SELECT *
            FROM lab_bubble_assets
            WHERE bubble_id = %s
            ORDER BY asset_role, vmid NULLS LAST, agent_id
            """,
            (bubble_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT *
            FROM lab_bubble_assets
            ORDER BY asset_role, vmid NULLS LAST, agent_id
            """
        ).fetchall()
    return [_asset_row(row) for row in rows]


def _require_asset_in_bubble(
    conn: Connection,
    *,
    asset_id: str,
    bubble_id: str,
    role: str | None = None,
    active_only: bool = False,
) -> dict:
    predicates = ["id = %s", "bubble_id = %s"]
    params: list[Any] = [asset_id, bubble_id]
    if role is not None:
        predicates.append("asset_role = %s")
        params.append(role)
    if active_only:
        predicates.append("membership_state IN ('active', 'provisioning')")
    row = conn.execute(
        f"SELECT * FROM lab_bubble_assets WHERE {' AND '.join(predicates)}",
        params,
    ).fetchone()
    if row is None:
        raise ValueError("asset not found in bubble")
    return _asset_row(row)


def update_asset(conn: Connection, asset_id: str, **fields: Any) -> dict:
    allowed = {
        "asset_role",
        "vmid",
        "vm_uuid",
        "run_id",
        "agent_id",
        "service_id",
        "membership_state",
        "evidence_state",
        "notes",
    }
    current = conn.execute(
        "SELECT * FROM lab_bubble_assets WHERE id = %s",
        (asset_id,),
    ).fetchone()
    if current is None:
        raise ValueError("asset not found")
    old = _asset_row(current)
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return old
    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{key} = %s" for key in updates)
    row = conn.execute(
        f"UPDATE lab_bubble_assets SET {set_sql} WHERE id = %s RETURNING *",
        list(updates.values()) + [asset_id],
    ).fetchone()
    asset = _asset_row(row)
    record_audit_event(
        conn,
        bubble_id=asset["bubble_id"],
        asset_id=asset_id,
        action="asset_updated",
        old_values=old,
        new_values=asset,
    )
    conn.commit()
    return asset


def move_asset(
    conn: Connection,
    asset_id: str,
    bubble_id: str,
    *,
    reason: str,
    actor: str = "operator",
) -> dict:
    current = conn.execute(
        "SELECT * FROM lab_bubble_assets WHERE id = %s",
        (asset_id,),
    ).fetchone()
    if current is None:
        raise ValueError("asset not found")
    old = _asset_row(current)
    provider_services = conn.execute(
        """
        SELECT id
        FROM lab_bubble_services
        WHERE provider_asset_id = %s
          AND bubble_id <> %s
        LIMIT 1
        """,
        (asset_id, bubble_id),
    ).fetchone()
    if provider_services is not None:
        raise ValueError("asset provides services in another bubble")
    row = conn.execute(
        """
        UPDATE lab_bubble_assets
        SET bubble_id = %s, updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (bubble_id, _now(), asset_id),
    ).fetchone()
    moved = _asset_row(row)
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        asset_id=asset_id,
        action="asset_moved",
        actor=actor,
        reason=reason,
        old_values=old,
        new_values=moved,
    )
    conn.commit()
    return moved


def add_service(
    conn: Connection,
    bubble_id: str,
    *,
    service_kind: str,
    service_name: str,
    scope: str = "bubble_local",
    provider_asset_id: str | None = None,
    consumer_refs: list | None = None,
    readiness_state: str = "unknown",
    evidence_summary: dict | None = None,
    actor: str = "system",
) -> dict:
    if provider_asset_id is not None:
        _require_asset_in_bubble(
            conn,
            asset_id=provider_asset_id,
            bubble_id=bubble_id,
        )
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_bubble_services (
            id, bubble_id, service_kind, service_name, scope,
            provider_asset_id, consumer_refs_json, readiness_state,
            last_evidence_at, evidence_summary_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            bubble_id,
            service_kind,
            service_name,
            scope,
            provider_asset_id,
            Jsonb(consumer_refs or []),
            readiness_state,
            _now() if readiness_state != "unknown" else None,
            Jsonb(evidence_summary or {}),
            now,
            now,
        ),
    ).fetchone()
    service = _service_row(row)
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        action="service_added",
        actor=actor,
        new_values=service,
    )
    conn.commit()
    return service


def list_services(conn: Connection, bubble_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM lab_bubble_services
        WHERE bubble_id = %s
        ORDER BY service_kind, service_name
        """,
        (bubble_id,),
    ).fetchall()
    return [_service_row(row) for row in rows]


def update_service(conn: Connection, service_id: str, **fields: Any) -> dict:
    allowed = {
        "service_kind",
        "service_name",
        "scope",
        "provider_asset_id",
        "consumer_refs",
        "readiness_state",
        "evidence_summary",
    }
    current = conn.execute(
        "SELECT * FROM lab_bubble_services WHERE id = %s",
        (service_id,),
    ).fetchone()
    if current is None:
        raise ValueError("service not found")
    old = _service_row(current)
    updates = {key: value for key, value in fields.items() if key in allowed}
    if updates.get("provider_asset_id") is not None:
        _require_asset_in_bubble(
            conn,
            asset_id=updates["provider_asset_id"],
            bubble_id=old["bubble_id"],
        )
    if "consumer_refs" in updates:
        updates["consumer_refs_json"] = Jsonb(updates.pop("consumer_refs") or [])
    if "evidence_summary" in updates:
        updates["evidence_summary_json"] = Jsonb(updates.pop("evidence_summary") or {})
    if "readiness_state" in updates:
        updates["last_evidence_at"] = _now()
    if not updates:
        return old
    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{key} = %s" for key in updates)
    row = conn.execute(
        f"UPDATE lab_bubble_services SET {set_sql} WHERE id = %s RETURNING *",
        list(updates.values()) + [service_id],
    ).fetchone()
    service = _service_row(row)
    record_audit_event(
        conn,
        bubble_id=service["bubble_id"],
        action="service_updated",
        old_values=old,
        new_values=service,
    )
    conn.commit()
    return service


def list_audit_events(conn: Connection, bubble_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM lab_bubble_audit_events
        WHERE bubble_id = %s
        ORDER BY id
        """,
        (bubble_id,),
    ).fetchall()
    return [_audit_row(row) for row in rows]


def update_readiness_from_dc_evidence(
    conn: Connection,
    bubble_id: str,
    *,
    dc_asset_id: str,
    evidence: dict,
) -> dict:
    _require_asset_in_bubble(
        conn,
        asset_id=dc_asset_id,
        bubble_id=bubble_id,
        role="domain_controller",
        active_only=True,
    )
    dc_ready = bool(evidence.get("ad_ds_ready"))
    dns_ready = bool(evidence.get("dns_ready"))
    dhcp_ready = bool(evidence.get("dhcp_ready"))
    workload_ready = dc_ready and dns_ready and dhcp_ready
    patched = update_bubble(
        conn,
        bubble_id,
        commit=False,
        dhcp_owner_asset_id=dc_asset_id,
        dc_ready=dc_ready,
        dns_ready=dns_ready,
        dhcp_ready=dhcp_ready,
        workload_ready=workload_ready,
        dhcp_scope=str(evidence.get("dhcp_scope") or ""),
        dhcp_pool_start=str(evidence.get("dhcp_pool_start") or ""),
        dhcp_pool_end=str(evidence.get("dhcp_pool_end") or ""),
    )
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        asset_id=dc_asset_id,
        action="readiness_evidence_updated",
        actor="agent",
        new_values=evidence,
    )
    conn.commit()
    return patched


def evaluate_launch_gate(
    conn: Connection,
    bubble_id: str,
    *,
    requires_domain_join: bool,
    requires_configmgr: bool,
    is_multi_bubble_context: bool,
    is_multi_domain_context: bool,
) -> dict:
    bubble = get_bubble(conn, bubble_id)
    if bubble is None:
        return {"state": "blocked", "allowed": False, "reasons": ["bubble not found"]}

    reasons = []
    ready = bool(bubble["dc_ready"] and bubble["dns_ready"] and bubble["dhcp_ready"])
    if not bubble["dc_ready"]:
        reasons.append("DC agent has not reported AD DS readiness")
    if not bubble["dns_ready"]:
        reasons.append("DC agent has not reported DNS readiness")
    if not bubble["dhcp_ready"]:
        reasons.append("DC agent has not reported DHCP scope readiness")

    if requires_configmgr:
        configmgr_ready = any(
            service["service_kind"] in {"configmgr", "mecm"}
            and service["readiness_state"] == "ready"
            for service in list_services(conn, bubble_id)
        )
        if not configmgr_ready:
            reasons.append("ConfigMgr service readiness is missing")

    hard_requires_ready = (
        (requires_domain_join and bubble["require_domain_ready"])
        or requires_configmgr
        or (is_multi_bubble_context and bubble["require_multi_domain_ready"])
        or (is_multi_domain_context and bubble["require_multi_domain_ready"])
    )
    if hard_requires_ready and reasons:
        return {"state": "blocked", "allowed": False, "reasons": reasons}
    if not ready and reasons:
        return {
            "state": "warning",
            "allowed": bool(bubble["allow_early_workgroup_launch"]),
            "reasons": reasons,
        }
    return {"state": "allowed", "allowed": True, "reasons": []}


def build_vm_page_payload(conn: Connection, *, vms: list[dict], agent_rows: list[dict]) -> dict:
    bubbles = list_bubbles(conn)
    assets = [
        asset
        for asset in list_assets(conn)
        if asset["membership_state"] in {"active", "provisioning"}
    ]
    services_by_bubble = {
        bubble["id"]: list_services(conn, bubble["id"]) for bubble in bubbles
    }
    vm_by_id = {
        int(vm["vmid"]): vm for vm in vms if vm.get("vmid") is not None
    }
    assets_by_bubble: dict[str, list[dict]] = {}
    assigned_vmids = set()
    for asset in assets:
        assets_by_bubble.setdefault(asset["bubble_id"], []).append(asset)
        if asset.get("vmid") is not None:
            assigned_vmids.add(int(asset["vmid"]))
    agent_by_vmid = {
        int(row["vmid"]): row
        for row in agent_rows
        if row.get("vmid") is not None
    }

    workstation_fleets = []
    critical_infrastructure = []
    connected_services = []
    gate_states = []

    for bubble in bubbles:
        bubble_assets = assets_by_bubble.get(bubble["id"], [])
        workstation_assets = [
            asset for asset in bubble_assets if asset["asset_role"] == "workstation"
        ]
        infra_assets = [
            asset for asset in bubble_assets if asset["asset_role"] != "workstation"
        ]
        workstation_vms = [
            vm_by_id[int(asset["vmid"])]
            for asset in workstation_assets
            if asset.get("vmid") is not None and int(asset["vmid"]) in vm_by_id
        ]
        running = sum(1 for vm in workstation_vms if vm.get("status") == "running")
        workstation_fleets.append(
            {
                "bubble": bubble,
                "workstation_count": len(workstation_assets),
                "running_count": running,
                "stopped_count": max(0, len(workstation_assets) - running),
                "assets": workstation_assets,
                "vms": workstation_vms,
                "readiness": {
                    "dc_ready": bubble["dc_ready"],
                    "dns_ready": bubble["dns_ready"],
                    "dhcp_ready": bubble["dhcp_ready"],
                    "workload_ready": bubble["workload_ready"],
                },
            }
        )
        for asset in infra_assets:
            vmid = asset.get("vmid")
            vm = vm_by_id.get(int(vmid)) if vmid is not None else None
            agent = agent_by_vmid.get(int(vmid)) if vmid is not None else None
            critical_infrastructure.append(
                {
                    "bubble": bubble,
                    "asset": asset,
                    "role": asset["asset_role"],
                    "vm": vm,
                    "agent": agent,
                }
            )
        for service in services_by_bubble.get(bubble["id"], []):
            connected_services.append({"bubble": bubble, **service})
        gate_states.append(
            {
                "bubble_id": bubble["id"],
                "workgroup": evaluate_launch_gate(
                    conn,
                    bubble["id"],
                    requires_domain_join=False,
                    requires_configmgr=False,
                    is_multi_bubble_context=len(bubbles) > 1,
                    is_multi_domain_context=False,
                ),
                "domain_join": evaluate_launch_gate(
                    conn,
                    bubble["id"],
                    requires_domain_join=True,
                    requires_configmgr=False,
                    is_multi_bubble_context=len(bubbles) > 1,
                    is_multi_domain_context=False,
                ),
            }
        )

    unassigned_assets = [
        vm
        for vm in vms
        if vm.get("vmid") is not None and int(vm["vmid"]) not in assigned_vmids
    ]
    return {
        "workstation_fleets": workstation_fleets,
        "critical_infrastructure": critical_infrastructure,
        "connected_services": connected_services,
        "unassigned_assets": unassigned_assets,
        "warnings": [],
        "gate_states": gate_states,
    }


def asset_for_agent(conn: Connection, bubble_id: str, agent_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM lab_bubble_assets
        WHERE bubble_id = %s
          AND agent_id = %s
          AND membership_state IN ('active', 'provisioning')
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (bubble_id, agent_id),
    ).fetchone()
    return _asset_row(row)
