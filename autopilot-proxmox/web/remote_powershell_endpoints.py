"""Constrained PowerShell runbook delivery for registered Autopilot Agents."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from web import agent_telemetry_pg
from web.agent_v1_endpoints import _conn, _public_work_item


router = APIRouter(prefix="/api/remote-powershell/v1", tags=["remote-powershell"])


@router.post("/agents/{agent_id}/endpoint-facts", status_code=202)
def queue_endpoint_facts(agent_id: str):
    """Queue the sole v1 read-only runbook without accepting arbitrary script text."""
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="remote_powershell",
            request={"command_id": "endpoint_facts"},
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)
