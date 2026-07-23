from __future__ import annotations

from typing import Any

from psycopg import Connection

from web import managed_labs_pg, proxmox_sdn


SUPPORTED_ACTIONS = {
    "create_sdn_zone",
    "create_sdn_vnet",
    "create_sdn_subnet",
    "apply_sdn",
}

ACTION_FINDING_TYPES = {
    "create_sdn_zone": "sdn_zone_missing",
    "create_sdn_vnet": "sdn_vnet_missing",
    "create_sdn_subnet": "sdn_subnet_missing",
}


def _get_fix(conn: Connection, fix_action_id: str) -> dict[str, Any]:
    row = managed_labs_pg.get_fix_action(conn, fix_action_id)
    if row is None:
        raise ValueError(f"fix action not found: {fix_action_id}")
    return row


def _get_lab(conn: Connection, lab_id: str) -> dict[str, Any]:
    lab = managed_labs_pg.get_lab(conn, lab_id)
    if lab is None:
        raise ValueError(f"lab not found: {lab_id}")
    return lab


def _ids(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...], *keys: str) -> set[str]:
    out: set[str] = set()
    for row in rows or []:
        for key in ("id", *keys):
            value = row.get(key)
            if value is not None and str(value).strip():
                out.add(str(value).strip())
    return out


def _subnet_exists(inventory: dict[str, Any], vnet: str, subnet: str) -> bool:
    rows = (inventory.get("subnets_by_vnet") or {}).get(vnet, []) or []
    return subnet in _ids(rows, "subnet", "cidr")


def _lock_token_from_response(lock: Any) -> str:
    if isinstance(lock, str):
        return lock.strip()
    if isinstance(lock, dict):
        return str(lock.get("lock-token") or lock.get("lock") or lock.get("lock_token") or lock.get("token") or lock.get("data") or "").strip()
    return ""


def _record_snapshot(conn: Connection, *, lab_id: str, action_type: str, pve_api) -> dict[str, Any]:
    snapshot = proxmox_sdn.inventory(pve_api)
    return managed_labs_pg.record_provider_snapshot(
        conn,
        lab_id=lab_id,
        provider="proxmox",
        snapshot_type="pre_fix",
        object_ref={"action_type": action_type},
        snapshot=snapshot,
    )


def _validate_fix_scope(fix: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    provider = str(fix.get("provider") or "").strip()
    if provider != "proxmox":
        raise ValueError(f"unsupported provider: {provider or '<empty>'}")

    status = str(fix.get("status") or "").strip()
    if status != "pending":
        raise ValueError(f"unsupported status: {status or '<empty>'}")

    action_type = str(fix.get("action_type") or "").strip()
    if action_type not in SUPPORTED_ACTIONS:
        raise ValueError(f"unsupported action type: {action_type or '<empty>'}")

    return str(fix["lab_id"]), action_type, dict(fix.get("request") or {})


def _already_present_result(action_type: str, request: dict[str, Any]) -> dict[str, Any] | None:
    if action_type == "create_sdn_zone":
        return {
            "already_present": True,
            "action": action_type,
            "object": {"zone": request.get("zone")},
            "detail": f"SDN zone {request.get('zone')} already present in snapshot.",
        }
    if action_type == "create_sdn_vnet":
        return {
            "already_present": True,
            "action": action_type,
            "object": {"vnet": request.get("vnet")},
            "detail": f"SDN VNet {request.get('vnet')} already present in snapshot.",
        }
    if action_type == "create_sdn_subnet":
        return {
            "already_present": True,
            "action": action_type,
            "object": {"vnet": request.get("vnet"), "subnet": request.get("subnet")},
            "detail": f"SDN subnet {request.get('subnet')} already present in snapshot.",
        }
    return None


def _existing_create_result(
    action_type: str, request: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any] | None:
    if action_type == "create_sdn_zone":
        zone = str(request.get("zone") or "").strip()
        if zone and zone in _ids(inventory.get("zones", []), "zone"):
            return _already_present_result(action_type, request)
        return None

    if action_type == "create_sdn_vnet":
        vnet = str(request.get("vnet") or "").strip()
        if vnet and vnet in _ids(inventory.get("vnets", []), "vnet"):
            return _already_present_result(action_type, request)
        return None

    if action_type == "create_sdn_subnet":
        vnet = str(request.get("vnet") or "").strip()
        subnet = str(request.get("subnet") or "").strip()
        if vnet and subnet and _subnet_exists(inventory, vnet, subnet):
            return _already_present_result(action_type, request)
        return None

    return None


def _verification_rows(action_type: str, request: dict[str, Any], inventory: dict[str, Any], lab: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    if action_type == "create_sdn_zone":
        zone = str(request.get("zone") or "").strip()
        row = next((item for item in inventory.get("zones", []) if str(item.get("zone") or item.get("id") or "").strip() == zone), {})
        return bool(row), {"observed": bool(row), "zone": row or {"zone": zone}}
    if action_type == "create_sdn_vnet":
        vnet = str(request.get("vnet") or "").strip()
        row = next((item for item in inventory.get("vnets", []) if str(item.get("vnet") or item.get("id") or "").strip() == vnet), {})
        return bool(row), {"observed": bool(row), "vnet": row or {"vnet": vnet}}
    if action_type == "create_sdn_subnet":
        vnet = str(request.get("vnet") or "").strip()
        subnet = str(request.get("subnet") or "").strip()
        row = next(
            (
                item
                for item in (inventory.get("subnets_by_vnet", {}) or {}).get(vnet, [])
                if subnet in _ids([item], "subnet", "cidr")
            ),
            {},
        )
        return bool(row), {"observed": bool(row), "subnet": row or {"vnet": vnet, "subnet": subnet}}
    if action_type == "apply_sdn":
        return True, {
            "observed": True,
            "inventory_counts": {
                "zones": len(inventory.get("zones", []) or []),
                "vnets": len(inventory.get("vnets", []) or []),
                "subnet_vnets": len((inventory.get("subnets_by_vnet") or {}).keys()),
            },
        }

    zone = str(lab.get("sdn_zone") or managed_labs_pg.proxmox_sdn_zone_id(str(lab["short_code"]))).strip()
    vnet = str(lab.get("sdn_vnet") or managed_labs_pg.proxmox_sdn_vnet_id(str(lab["short_code"]))).strip()
    subnet = str(lab.get("sdn_subnet") or lab["network_cidr"]).strip()
    zone_row = next((item for item in inventory.get("zones", []) if str(item.get("zone") or item.get("id") or "").strip() == zone), {})
    vnet_row = next((item for item in inventory.get("vnets", []) if str(item.get("vnet") or item.get("id") or "").strip() == vnet), {})
    subnet_row = next(
        (
            item
            for item in (inventory.get("subnets_by_vnet", {}) or {}).get(vnet, [])
            if subnet in _ids([item], "subnet", "cidr")
        ),
        {},
    )
    observed = bool(zone_row and vnet_row and subnet_row)
    return observed, {
        "observed": observed,
        "zone": zone_row or {"zone": zone},
        "vnet": vnet_row or {"vnet": vnet},
        "subnet": subnet_row or {"subnet": subnet, "vnet": vnet},
    }


def _sync_boundary_state(conn: Connection, *, lab: dict[str, Any], inventory: dict[str, Any], status: str) -> None:
    managed_labs_pg.sync_lab_network_current_state(conn, lab=lab, inventory=inventory, status=status, commit=False)


def _record_verification_event(
    conn: Connection,
    *,
    lab_id: str,
    fix_action_id: str,
    action_type: str,
    observed: bool,
    verification: dict[str, Any],
) -> None:
    managed_labs_pg.record_event(
        conn,
        lab_id=lab_id,
        event_type="fix_action_verified",
        actor="reconciler",
        detail=f"Verification {'passed' if observed else 'failed'} for {action_type}.",
        payload={
            "fix_action_id": fix_action_id,
            "action_type": action_type,
            "observed": observed,
            "verification": verification,
        },
        commit=False,
    )


def _record_verification_failure_finding(
    conn: Connection,
    *,
    lab_id: str,
    reconcile_run_id: str | None,
    action_type: str,
    request: dict[str, Any],
    verification: dict[str, Any],
) -> None:
    managed_labs_pg.record_finding(
        conn,
        lab_id=lab_id,
        reconcile_run_id=reconcile_run_id,
        provider="proxmox",
        finding_type=f"{action_type}_verification_failed",
        severity="blocked",
        detail=f"Verification did not observe the expected object after {action_type}.",
        object_ref=request,
        actual_state=verification,
        commit=False,
    )


def execute_fix_action(conn: Connection, *, fix_action_id: str, pve_api, pve_put, pve_delete) -> dict:
    fix = _get_fix(conn, fix_action_id)
    lab_id, action_type, request = _validate_fix_scope(fix)
    lab = _get_lab(conn, lab_id)
    snapshot = _record_snapshot(conn, lab_id=lab_id, action_type=action_type, pve_api=pve_api)
    inventory = dict(snapshot.get("snapshot") or {})
    existing = _existing_create_result(action_type, request, inventory)
    if existing is not None:
        observed, verification = _verification_rows(action_type, request, inventory, lab)
        _sync_boundary_state(conn, lab=lab, inventory=inventory, status="fixing")
        _record_verification_event(
            conn,
            lab_id=lab_id,
            fix_action_id=fix_action_id,
            action_type=action_type,
            observed=observed,
            verification=verification,
        )
        finding_type = ACTION_FINDING_TYPES.get(action_type)
        if finding_type:
            managed_labs_pg.resolve_open_finding(conn, lab_id=lab_id, provider="proxmox", finding_type=finding_type, commit=False)
        return managed_labs_pg.update_fix_action(
            conn,
            fix_action_id,
            status="fixed",
            result={"ok": True, **existing, "verification": verification},
            snapshot_id=snapshot["id"],
        )

    lock_token = ""
    released = False

    try:
        if action_type == "create_sdn_zone":
            mutation_result = proxmox_sdn.create_zone(pve_api, request)
        elif action_type == "create_sdn_vnet":
            mutation_result = proxmox_sdn.create_vnet(pve_api, request)
        elif action_type == "create_sdn_subnet":
            vnet = str(request.get("vnet") or "").strip()
            if not vnet:
                raise ValueError("create_sdn_subnet requires vnet")
            body = {"type": "subnet", **{key: value for key, value in request.items() if key != "vnet"}}
            mutation_result = proxmox_sdn.create_subnet(pve_api, vnet, body)
        else:
            lock = proxmox_sdn.acquire_lock(pve_api, allow_pending=bool(request.get("allow_pending", True)))
            lock_token = _lock_token_from_response(lock)
            if not lock_token:
                raise ValueError("failed to acquire SDN lock token")
            mutation_result = proxmox_sdn.apply_sdn(pve_put, lock_token, release_lock=False)
            proxmox_sdn.release_lock(pve_delete, lock_token, force=False)
            released = True
        post_inventory = proxmox_sdn.inventory(pve_api)
    except Exception as exc:
        if lock_token and not released:
            try:
                proxmox_sdn.release_lock(pve_delete, lock_token, force=True)
            except Exception:
                import logging
                logging.getLogger("web.managed_labs").warning(
                    "failed to force-release SDN lock %s after a fix error",
                    lock_token, exc_info=True,
                )
        return managed_labs_pg.update_fix_action(
            conn,
            fix_action_id,
            status="failed",
            result={"ok": False, "error": str(exc)},
            snapshot_id=snapshot["id"],
        )

    observed, verification = _verification_rows(action_type, request, post_inventory, lab)
    _sync_boundary_state(conn, lab=lab, inventory=post_inventory, status="ready" if action_type == "apply_sdn" and observed else "fixing")
    _record_verification_event(
        conn,
        lab_id=lab_id,
        fix_action_id=fix_action_id,
        action_type=action_type,
        observed=observed,
        verification=verification,
    )

    if not observed:
        _record_verification_failure_finding(
            conn,
            lab_id=lab_id,
            reconcile_run_id=fix.get("reconcile_run_id"),
            action_type=action_type,
            request=request,
            verification=verification,
        )
        return managed_labs_pg.update_fix_action(
            conn,
            fix_action_id,
            status="failed",
            result={
                "ok": False,
                "error": f"verification did not observe the expected object after {action_type}",
                "result": mutation_result,
                "verification": verification,
            },
            snapshot_id=snapshot["id"],
        )

    finding_type = ACTION_FINDING_TYPES.get(action_type)
    if finding_type:
        managed_labs_pg.resolve_open_finding(conn, lab_id=lab_id, provider="proxmox", finding_type=finding_type, commit=False)

    return managed_labs_pg.update_fix_action(
        conn,
        fix_action_id,
        status="fixed",
        result={"ok": True, "result": mutation_result, "verification": verification},
        snapshot_id=snapshot["id"],
    )


def execute_pending_network_fixes(
    conn: Connection,
    *,
    lab_id: str,
    pve_api,
    pve_put,
    pve_delete,
    limit: int = 10,
) -> dict:
    fixed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    pending = [
        fix
        for fix in managed_labs_pg.list_pending_fix_actions(conn, lab_id)
        if str(fix.get("provider") or "").strip() == "proxmox"
        and str(fix.get("action_type") or "").strip() in SUPPORTED_ACTIONS
    ]

    for fix in pending[:limit]:
        result = execute_fix_action(
            conn,
            fix_action_id=str(fix["id"]),
            pve_api=pve_api,
            pve_put=pve_put,
            pve_delete=pve_delete,
        )
        if result["status"] == "fixed":
            fixed.append(result)
        elif result["status"] == "blocked":
            blocked.append(result)
        else:
            failed.append(result)

    return {"fixed": fixed, "blocked": blocked, "failed": failed}
