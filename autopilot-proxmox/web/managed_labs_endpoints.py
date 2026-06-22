from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web import managed_labs_network, managed_labs_pg, managed_labs_reconciler, proxmox_sdn


router = APIRouter(prefix="/api/labs", tags=["labs"])


class LabCreateBody(BaseModel):
    name: str
    short_code: str
    group_tag: str
    network_cidr: str
    gateway_ip: str = ""
    network_mode: str = "sdn"
    sdn_zone: str = ""
    sdn_vnet: str = ""
    sdn_subnet: str = ""


def _web_app():
    from web import app as web_app

    return web_app


def _database_url() -> str:
    try:
        return _web_app()._database_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Autopilot database is not configured") from exc


@contextmanager
def _conn():
    from web import db_pg, managed_labs_pg

    with db_pg.connection(_database_url()) as conn:
        managed_labs_pg.init(conn)
        yield conn


def _api():
    return _web_app()._proxmox_api


def _put():
    return _web_app()._proxmox_api_put


def _delete():
    return _web_app()._proxmox_api_delete


def _get_lab_or_404(conn, lab_id: str) -> dict:
    lab = managed_labs_pg.get_lab(conn, lab_id)
    if not lab:
        raise HTTPException(status_code=404, detail="lab not found")
    return lab


def _get_fix_for_lab_or_404(conn, *, lab_id: str, fix_id: str) -> dict:
    fix = managed_labs_pg.get_fix_action(conn, fix_id)
    if not fix or str(fix.get("lab_id") or "") != lab_id:
        raise HTTPException(status_code=404, detail="fix not found")
    return fix


@router.get("/page")
def page(selected_lab_id: str | None = None):
    with _conn() as conn:
        return managed_labs_pg.page_payload(conn, selected_lab_id=selected_lab_id)


@router.get("")
def list_labs():
    with _conn() as conn:
        return managed_labs_pg.list_labs(conn)


@router.post("", status_code=201)
def create_lab(body: LabCreateBody):
    with _conn() as conn:
        lab = managed_labs_pg.create_lab(
            conn,
            name=body.name,
            short_code=body.short_code,
            group_tag=body.group_tag,
            network_cidr=body.network_cidr,
            gateway_ip=body.gateway_ip,
            network_mode=body.network_mode,
            sdn_zone=body.sdn_zone,
            sdn_vnet=body.sdn_vnet,
            sdn_subnet=body.sdn_subnet,
        )
        try:
            managed_labs_pg.reserve_value(conn, lab_id=lab["id"], reservation_type="group_tag", value=lab["group_tag"])
            managed_labs_pg.reserve_value(conn, lab_id=lab["id"], reservation_type="cidr", value=lab["network_cidr"])
        except Exception as exc:
            managed_labs_pg.delete_lab(conn, lab["id"])
            raise HTTPException(status_code=500, detail="managed lab create failed") from exc
        return lab


@router.get("/{lab_id}")
def get_lab(lab_id: str):
    with _conn() as conn:
        lab = managed_labs_pg.get_lab(conn, lab_id)
        if not lab:
            raise HTTPException(status_code=404, detail="lab not found")
        return lab


@router.post("/{lab_id}/reconcile")
def reconcile_lab(lab_id: str):
    with _conn() as conn:
        lab = _get_lab_or_404(conn, lab_id)
        run = managed_labs_pg.start_reconcile_run(conn, lab_id=lab_id, attempt=int(lab.get("retry_count") or 0) + 1)
        try:
            inventory = proxmox_sdn.inventory(_api())
            result = managed_labs_reconciler.plan_network_reconcile(
                conn,
                lab_id=lab_id,
                reconcile_run_id=run["id"],
                inventory=inventory,
            )
        except Exception as exc:
            managed_labs_pg.finish_reconcile_run(
                conn,
                run_id=run["id"],
                status="failed",
                summary=f"Managed labs network reconcile failed: {exc}",
            )
            raise HTTPException(status_code=500, detail="managed lab reconcile failed") from exc
        managed_labs_pg.finish_reconcile_run(
            conn,
            run_id=run["id"],
            status=result["status"],
            summary=f"Managed labs network reconcile finished with status {result['status']}.",
        )
        return result


@router.post("/{lab_id}/fixes/run-pending")
def run_pending_fixes(lab_id: str):
    with _conn() as conn:
        _get_lab_or_404(conn, lab_id)
        return managed_labs_network.execute_pending_network_fixes(
            conn,
            lab_id=lab_id,
            pve_api=_api(),
            pve_put=_put(),
            pve_delete=_delete(),
        )


@router.post("/{lab_id}/fixes/{fix_id}/run")
def run_fix(lab_id: str, fix_id: str):
    with _conn() as conn:
        _get_lab_or_404(conn, lab_id)
        _get_fix_for_lab_or_404(conn, lab_id=lab_id, fix_id=fix_id)
        return managed_labs_network.execute_fix_action(
            conn,
            fix_action_id=fix_id,
            pve_api=_api(),
            pve_put=_put(),
            pve_delete=_delete(),
        )
