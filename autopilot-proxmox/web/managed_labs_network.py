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


def _get_fix(conn: Connection, fix_action_id: str) -> dict[str, Any]:
    row = managed_labs_pg.get_fix_action(conn, fix_action_id)
    if row is None:
        raise ValueError(f"fix action not found: {fix_action_id}")
    return row


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
    return subnet in _ids(rows, "subnet")


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


def execute_fix_action(conn: Connection, *, fix_action_id: str, pve_api, pve_put, pve_delete) -> dict:
    fix = _get_fix(conn, fix_action_id)
    lab_id, action_type, request = _validate_fix_scope(fix)
    snapshot = _record_snapshot(conn, lab_id=lab_id, action_type=action_type, pve_api=pve_api)
    inventory = dict(snapshot.get("snapshot") or {})
    existing = _existing_create_result(action_type, request, inventory)
    if existing is not None:
        return managed_labs_pg.update_fix_action(
            conn,
            fix_action_id,
            status="fixed",
            result={"ok": True, **existing},
            snapshot_id=snapshot["id"],
        )

    lock_token = ""
    released = False

    try:
        if action_type == "create_sdn_zone":
            result = proxmox_sdn.create_zone(pve_api, request)
        elif action_type == "create_sdn_vnet":
            result = proxmox_sdn.create_vnet(pve_api, request)
        elif action_type == "create_sdn_subnet":
            vnet = str(request.get("vnet") or "").strip()
            if not vnet:
                raise ValueError("create_sdn_subnet requires vnet")
            body = {key: value for key, value in request.items() if key != "vnet"}
            result = proxmox_sdn.create_subnet(pve_api, vnet, body)
        else:
            lock = proxmox_sdn.acquire_lock(pve_api, allow_pending=bool(request.get("allow_pending", True)))
            lock_token = str(
                lock.get("lock") or lock.get("lock_token") or lock.get("data") or ""
            ).strip()
            if not lock_token:
                raise ValueError("failed to acquire SDN lock token")
            result = proxmox_sdn.apply_sdn(pve_put, lock_token, release_lock=False)
            proxmox_sdn.release_lock(pve_delete, lock_token, force=False)
            released = True
    except Exception as exc:
        if lock_token and not released:
            try:
                proxmox_sdn.release_lock(pve_delete, lock_token, force=True)
            except Exception:
                pass
        return managed_labs_pg.update_fix_action(
            conn,
            fix_action_id,
            status="failed",
            result={"ok": False, "error": str(exc)},
            snapshot_id=snapshot["id"],
        )

    return managed_labs_pg.update_fix_action(
        conn,
        fix_action_id,
        status="fixed",
        result={"ok": True, "result": result},
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
