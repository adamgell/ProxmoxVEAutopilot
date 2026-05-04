"""FastAPI router for the WinPE phase-0 agent.

All endpoints prefixed /winpe/. Routes:
  POST /winpe/run/<id>/identity        Ansible writes vmid + vm_uuid post-clone
  POST /winpe/register                 Agent registers, gets actions + token
  GET  /winpe/sequence/<run_id>        Re-fetch action list (idempotent)
  GET  /winpe/autopilot-config/<run_id>  Per-run JSON payload
  GET  /winpe/unattend/<run_id>        Per-run post_winpe unattend XML
  POST /winpe/step/<step_id>/result    Step state telemetry, refreshes token
  POST /winpe/done                     Detach ide2+sata0, mark awaiting_specialize

Token secret: AUTOPILOT_WINPE_TOKEN_SECRET env var.
Identity-endpoint client allowlist: AUTOPILOT_WINPE_IDENTITY_ALLOWLIST
(comma-separated hostnames or IPs; matched against request.client.host).
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from web import sequences_db, winpe_token


router = APIRouter(prefix="/winpe", tags=["winpe"])


class IdentityBody(BaseModel):
    vmid: int
    vm_uuid: str


def _identity_allowlist() -> set[str]:
    raw = os.environ.get("AUTOPILOT_WINPE_IDENTITY_ALLOWLIST", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _check_identity_caller(request: Request) -> None:
    allow = _identity_allowlist()
    if not allow:
        return
    client = request.client.host if request.client else ""
    if client not in allow:
        raise HTTPException(status_code=403, detail="caller not allowed")


def _db_path() -> str:
    from web import app as web_app
    return web_app.SEQUENCES_DB


@router.post("/run/{run_id}/identity")
def post_identity(run_id: int, body: IdentityBody, request: Request):
    _check_identity_caller(request)
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["state"] == "queued":
        sequences_db.set_provisioning_run_identity(
            db, run_id=run_id, vmid=body.vmid, vm_uuid=body.vm_uuid,
        )
    # Already past queued: idempotent no-op (rerun-safe from Ansible)
    return {"ok": True}
