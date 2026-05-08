"""AutopilotAgent v1 API.

The persistent Windows agent uses this API after OSD or Ninja bootstrap.
Bootstrap accepts either a Task Sequence run bearer token or an operator-set
fleet bootstrap token. Runtime heartbeats and events use per-agent bearer
tokens whose hashes are stored in Postgres.
"""
from __future__ import annotations

import hmac
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from web import agent_telemetry_pg, ts_engine_pg, winpe_token


router = APIRouter(prefix="/api/agent/v1", tags=["agent-v1"])
_HEARTBEAT_INTERVAL_SECONDS = 30


class BootstrapBody(BaseModel):
    agent_id: str = Field(min_length=1)
    run_id: Optional[str] = None
    phase: Optional[str] = None
    vmid: Optional[int] = None
    vm_uuid: Optional[str] = None
    computer_name: Optional[str] = None
    serial_number: Optional[str] = None
    agent_version: Optional[str] = None


class HeartbeatBody(BaseModel):
    agent_id: str = Field(min_length=1)
    vmid: Optional[int] = None
    vm_uuid: Optional[str] = None
    computer_name: Optional[str] = None
    serial_number: Optional[str] = None
    primary_ipv4: Optional[str] = None
    ip_addresses: list[str] = Field(default_factory=list)
    nics: list[dict] = Field(default_factory=list)
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    os_build: Optional[str] = None
    boot_time: Optional[str] = None
    uptime_seconds: Optional[int] = None
    qga_service_name: Optional[str] = None
    qga_state: Optional[str] = None
    domain_name: Optional[str] = None
    domain_joined: Optional[bool] = None
    entra_joined: Optional[bool] = None
    tenant_id: Optional[str] = None
    current_run_id: Optional[str] = None
    current_phase: Optional[str] = None
    current_step_id: Optional[str] = None
    agent_version: Optional[str] = None


class EventBody(BaseModel):
    agent_id: str = Field(min_length=1)
    severity: str = Field(default="info", pattern=r"^(debug|info|warning|error)$")
    event_type: str = Field(min_length=1)
    message: Optional[str] = None
    data: dict = Field(default_factory=dict)


def _database_url() -> str:
    from web import app as web_app

    try:
        return web_app._database_url()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="AutopilotAgent database is not configured",
        )


@contextmanager
def _conn():
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        agent_telemetry_pg.init(conn)
        yield conn


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer")
    return token


def _configured_fleet_bootstrap_token() -> str:
    return os.environ.get("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN", "").strip()


def _bootstrap_run_id(token: str, requested_run_id: Optional[str]) -> str | None:
    fleet_token = _configured_fleet_bootstrap_token()
    if fleet_token and hmac.compare_digest(token, fleet_token):
        return None
    try:
        payload = winpe_token.verify(token)
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="bootstrap token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid bootstrap token")
    run_id = str(payload["run_id"])
    if requested_run_id and str(requested_run_id) != run_id:
        raise HTTPException(status_code=403, detail="token/run mismatch")
    return run_id


def _require_agent(
    authorization: Optional[str] = Header(None),
) -> dict:
    token = _bearer(authorization)
    with _conn() as conn:
        device = agent_telemetry_pg.validate_agent_token(conn, token)
    if not device:
        raise HTTPException(status_code=401, detail="invalid agent token")
    return device


@router.post("/bootstrap")
def bootstrap_agent(
    body: BootstrapBody,
    authorization: Optional[str] = Header(None),
):
    bootstrap_token = _bearer(authorization)
    token_run_id = _bootstrap_run_id(bootstrap_token, body.run_id)
    run_id = body.run_id or token_run_id
    agent_token = agent_telemetry_pg.new_agent_token()
    with _conn() as conn:
        if run_id:
            try:
                run = ts_engine_pg.get_run(conn, run_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="run not found")
            vmid = body.vmid if body.vmid is not None else run.get("vmid")
            vm_uuid = body.vm_uuid or run.get("vm_uuid")
            computer_name = body.computer_name or run.get("computer_name")
            serial_number = body.serial_number or run.get("serial_number")
        else:
            vmid = body.vmid
            vm_uuid = body.vm_uuid
            computer_name = body.computer_name
            serial_number = body.serial_number
        agent_telemetry_pg.upsert_device(
            conn,
            agent_id=body.agent_id,
            token=agent_token,
            vmid=vmid,
            vm_uuid=vm_uuid,
            serial_number=serial_number,
            computer_name=computer_name,
            agent_version=body.agent_version,
            created_from_run_id=run_id,
        )
    return {
        "schema_version": 1,
        "agent_id": body.agent_id,
        "agent_token": agent_token,
        "heartbeat_interval_seconds": _HEARTBEAT_INTERVAL_SECONDS,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/heartbeat")
def heartbeat(body: HeartbeatBody, device: dict = Depends(_require_agent)):
    if body.agent_id != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/agent mismatch")
    payload = body.model_dump()
    with _conn() as conn:
        agent_telemetry_pg.record_heartbeat(
            conn,
            agent_id=body.agent_id,
            payload=payload,
        )
    return {
        "status": "ok",
        "heartbeat_interval_seconds": _HEARTBEAT_INTERVAL_SECONDS,
    }


@router.post("/events")
def events(body: EventBody, device: dict = Depends(_require_agent)):
    if body.agent_id != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/agent mismatch")
    with _conn() as conn:
        event = agent_telemetry_pg.record_event(
            conn,
            agent_id=body.agent_id,
            payload=body.model_dump(),
        )
    return {"status": "ok", "event_id": event["id"]}


@router.get("/config")
def config(device: dict = Depends(_require_agent)):
    with _conn() as conn:
        latest = agent_telemetry_pg.latest_for_agent(conn, device["agent_id"])
    return {
        "schema_version": 1,
        "agent_id": device["agent_id"],
        "heartbeat_interval_seconds": _HEARTBEAT_INTERVAL_SECONDS,
        "last_heartbeat_at": latest["received_at"] if latest else None,
        "last_primary_ipv4": latest["primary_ipv4"] if latest else None,
    }
