from __future__ import annotations

from typing import Any

from psycopg import Connection

from web import managed_labs_pg


def _ids(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...], *keys: str) -> set[str]:
    out: set[str] = set()
    for row in rows or []:
        for key in ("id", *keys):
            value = row.get(key)
            if value is not None and str(value).strip():
                out.add(str(value).strip())
    return out


def _subnet_exists(inventory: dict[str, Any], vnet: str, subnet: str) -> bool:
    rows = inventory.get("subnets_by_vnet", {}).get(vnet, []) or []
    return subnet in _ids(rows, "subnet", "cidr")


def _record_fixable(
    conn: Connection,
    *,
    lab: dict[str, Any],
    reconcile_run_id: str | None,
    finding_type: str,
    detail: str,
    action_type: str,
    priority: int,
    request: dict[str, Any],
    actual_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    finding = managed_labs_pg.record_finding(
        conn,
        lab_id=lab["id"],
        reconcile_run_id=reconcile_run_id,
        provider="proxmox",
        finding_type=finding_type,
        severity="fixable",
        detail=detail,
        object_ref=request,
        desired_state=request,
        actual_state=actual_state,
    )
    fix = managed_labs_pg.create_fix_action(
        conn,
        lab_id=lab["id"],
        reconcile_run_id=reconcile_run_id,
        provider="proxmox",
        action_type=action_type,
        priority=priority,
        detail=detail,
        request=request,
    )
    return finding, fix


def plan_network_reconcile(
    conn: Connection,
    *,
    lab_id: str,
    inventory: dict[str, Any],
    reconcile_run_id: str | None = None,
) -> dict[str, Any]:
    lab = managed_labs_pg.get_lab(conn, lab_id)
    if not lab:
        raise ValueError(f"lab not found: {lab_id}")

    managed_labs_pg.ensure_lab_boundary_model(conn, lab=lab, commit=False)
    managed_labs_pg.clear_current_reconcile_state(conn, lab_id=lab["id"], commit=False)

    findings: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    overlaps = managed_labs_pg.find_overlapping_cidr_reservations(
        conn,
        lab["network_cidr"],
        exclude_lab_id=lab["id"],
    )
    if overlaps:
        managed_labs_pg.sync_lab_network_current_state(conn, lab=lab, inventory=inventory, status="blocked", commit=False)
        findings.append(
            managed_labs_pg.record_finding(
                conn,
                lab_id=lab["id"],
                reconcile_run_id=reconcile_run_id,
                provider="network",
                finding_type="subnet_overlaps_existing_lab",
                severity="blocked",
                detail=f"Requested subnet overlaps existing reservation {overlaps[0]['value']}.",
                object_ref={"cidr": lab["network_cidr"], "overlap": overlaps[0]},
                actual_state={"overlap": overlaps[0]},
            )
        )
        return {"status": "blocked", "findings": findings, "fix_actions": fixes}

    managed_labs_pg.reserve_value(
        conn,
        lab_id=lab["id"],
        reservation_type="cidr",
        value=lab["network_cidr"],
        commit=False,
    )

    if lab["network_mode"] != "sdn":
        managed_labs_pg.sync_lab_network_current_state(conn, lab=lab, inventory=inventory, status="blocked", commit=False)
        findings.append(
            managed_labs_pg.record_finding(
                conn,
                lab_id=lab["id"],
                reconcile_run_id=reconcile_run_id,
                provider="proxmox",
                finding_type="bridge_validate_only",
                severity="blocked",
                detail="Bridge targets are validate-only in this slice; create or select an existing bridge before proceeding.",
                object_ref={"network_mode": lab["network_mode"]},
            )
        )
        return {"status": "blocked", "findings": findings, "fix_actions": fixes}

    zone = lab["sdn_zone"] or managed_labs_pg.proxmox_sdn_zone_id(str(lab["short_code"]))
    vnet = lab["sdn_vnet"] or managed_labs_pg.proxmox_sdn_vnet_id(str(lab["short_code"]))
    subnet = lab["sdn_subnet"] or lab["network_cidr"]
    zone_rows = inventory.get("zones", []) or []
    vnet_rows = inventory.get("vnets", []) or []
    subnet_rows = (inventory.get("subnets_by_vnet", {}) or {}).get(vnet, []) or []
    zone_ids = _ids(zone_rows, "zone")
    vnet_ids = _ids(vnet_rows, "vnet")
    zone_row = next((row for row in zone_rows if str(row.get("zone") or row.get("id") or "").strip() == zone), {})
    vnet_row = next((row for row in vnet_rows if str(row.get("vnet") or row.get("id") or "").strip() == vnet), {})
    subnet_row = next((row for row in subnet_rows if subnet in _ids([row], "subnet", "cidr")), {})

    if zone not in zone_ids:
        finding, fix = _record_fixable(
            conn,
            lab=lab,
            reconcile_run_id=reconcile_run_id,
            finding_type="sdn_zone_missing",
            detail=f"SDN zone {zone} is missing.",
            action_type="create_sdn_zone",
            priority=10,
            request={"zone": zone, "type": "simple"},
            actual_state=zone_row,
        )
        findings.append(finding)
        fixes.append(fix)

    if vnet not in vnet_ids:
        finding, fix = _record_fixable(
            conn,
            lab=lab,
            reconcile_run_id=reconcile_run_id,
            finding_type="sdn_vnet_missing",
            detail=f"SDN VNet {vnet} is missing.",
            action_type="create_sdn_vnet",
            priority=20,
            request={"vnet": vnet, "zone": zone, "alias": lab["name"]},
            actual_state=vnet_row,
        )
        findings.append(finding)
        fixes.append(fix)

    if not _subnet_exists(inventory, vnet, subnet):
        request = {"vnet": vnet, "subnet": subnet, "gateway": lab["gateway_ip"], "snat": True}
        finding, fix = _record_fixable(
            conn,
            lab=lab,
            reconcile_run_id=reconcile_run_id,
            finding_type="sdn_subnet_missing",
            detail=f"SDN subnet {subnet} is missing on {vnet}.",
            action_type="create_sdn_subnet",
            priority=30,
            request=request,
            actual_state=subnet_row,
        )
        findings.append(finding)
        fixes.append(fix)

    if fixes:
        managed_labs_pg.sync_lab_network_current_state(conn, lab=lab, inventory=inventory, status="fixing", commit=False)
        fixes.append(
            managed_labs_pg.create_fix_action(
                conn,
                lab_id=lab["id"],
                reconcile_run_id=reconcile_run_id,
                provider="proxmox",
                action_type="apply_sdn",
                priority=90,
                detail="Apply pending Proxmox SDN changes.",
                request={"allow_pending": True},
            )
        )
        return {"status": "fixing", "findings": findings, "fix_actions": fixes}

    managed_labs_pg.sync_lab_network_current_state(conn, lab=lab, inventory=inventory, status="ready", commit=True)
    return {"status": "ready", "findings": [], "fix_actions": []}
