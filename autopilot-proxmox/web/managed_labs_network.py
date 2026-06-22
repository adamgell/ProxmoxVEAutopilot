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


def execute_fix_action(conn: Connection, *, fix_action_id: str, pve_api, pve_put, pve_delete) -> dict:
    fix = _get_fix(conn, fix_action_id)
    lab_id = str(fix["lab_id"])
    action_type = str(fix["action_type"])
    request = dict(fix.get("request") or {})
    snapshot = _record_snapshot(conn, lab_id=lab_id, action_type=action_type, pve_api=pve_api)

    if action_type not in SUPPORTED_ACTIONS:
        return managed_labs_pg.update_fix_action(
            conn,
            fix_action_id,
            status="blocked",
            result={"ok": False, "error": f"unsupported action type: {action_type}"},
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

    for fix in managed_labs_pg.list_pending_fix_actions(conn, lab_id)[:limit]:
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
