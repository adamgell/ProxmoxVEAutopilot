"""AutopilotAgent v1 API.

The persistent Windows agent uses this API after OSD or Ninja bootstrap.
Bootstrap accepts either a Task Sequence run bearer token or an operator-set
fleet bootstrap token. Runtime heartbeats and events use per-agent bearer
tokens whose hashes are stored in Postgres.
"""
from __future__ import annotations

import csv
import hmac
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from web import agent_telemetry_pg, setup_artifacts, ts_engine_pg, winpe_token


router = APIRouter(prefix="/api/agent/v1", tags=["agent-v1"])
_HEARTBEAT_INTERVAL_SECONDS = 30
_SETUP_STATE_PATH = Path(__file__).resolve().parents[1] / "output" / "setup" / "foundation_state.json"


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
    server_url: Optional[str] = None
    agent_version: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    bubble_id: Optional[str] = None
    dc_readiness: dict = Field(default_factory=dict)


class EventBody(BaseModel):
    agent_id: str = Field(min_length=1)
    severity: str = Field(default="info", pattern=r"^(debug|info|warning|error)$")
    event_type: str = Field(min_length=1)
    message: Optional[str] = None
    data: dict = Field(default_factory=dict)


class WorkNextBody(BaseModel):
    agent_id: str = Field(min_length=1)
    supported_kinds: list[str] = Field(default_factory=list)


class WorkCompleteBody(BaseModel):
    agent_id: str = Field(min_length=1)
    result: dict = Field(default_factory=dict)


class WorkFailBody(BaseModel):
    agent_id: str = Field(min_length=1)
    error: str = Field(min_length=1)
    result: dict = Field(default_factory=dict)


class HashBody(BaseModel):
    work_item_id: Optional[str] = None
    serial_number: str = Field(min_length=1)
    product_id: str = ""
    hardware_hash: str = Field(min_length=1)


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


def _configured_fleet_bootstrap_token_sha256() -> str:
    return os.environ.get("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256", "").strip().lower()


def _fleet_bootstrap_token_matches(token: str) -> bool:
    configured_hash = _configured_fleet_bootstrap_token_sha256()
    if not configured_hash:
        return False
    submitted = token.strip().lower()
    if hmac.compare_digest(submitted, configured_hash):
        return True
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(token_hash, configured_hash)


def _read_setup_state() -> dict:
    try:
        data = json.loads(_SETUP_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _buildhost_auto_approval_matches(
    body: BootstrapBody,
    *,
    vmid: int | None,
    computer_name: str | None,
) -> bool:
    state = _read_setup_state()
    if state.get("build_host_agent_auto_approve") is not True:
        return False
    expected_vmid = state.get("build_host_vmid") or state.get("buildhost_vmid")
    try:
        expected_vmid_int = int(expected_vmid)
    except (TypeError, ValueError):
        return False
    expected_agent_id = (
        str(state.get("build_host_expected_agent_id") or "").strip()
        or f"buildhost-{expected_vmid_int}"
    )
    expected_computer = (
        str(state.get("build_host_expected_computer_name") or "").strip()
        or "AUTOPILOT-BLD"
    )
    actual_computer = (computer_name or body.computer_name or "").strip()
    return (
        (body.phase or "").casefold() == "build-host"
        and body.agent_id == expected_agent_id
        and vmid == expected_vmid_int
        and actual_computer.casefold() == expected_computer.casefold()
    )


def _bootstrap_run_id(token: str, requested_run_id: Optional[str]) -> str | None:
    if _fleet_bootstrap_token_matches(token):
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


def _public_work_item(row: dict) -> dict:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "kind": row["kind"],
        "status": row["status"],
        "vmid": row.get("vmid"),
        "job_id": row.get("job_id"),
        "request": row.get("request_json") or {},
        "result": row.get("result_json") or {},
        "error": row.get("error"),
        "created_at": (
            row["created_at"].isoformat()
            if hasattr(row.get("created_at"), "isoformat")
            else row.get("created_at")
        ),
        "claimed_at": (
            row["claimed_at"].isoformat()
            if hasattr(row.get("claimed_at"), "isoformat")
            else row.get("claimed_at")
        ),
        "completed_at": (
            row["completed_at"].isoformat()
            if hasattr(row.get("completed_at"), "isoformat")
            else row.get("completed_at")
        ),
    }


def _persist_autopilot_hash(
    *,
    vmid: int,
    serial: str,
    product_id: str,
    hardware_hash: str,
    group_tag: str = "",
    source: str = "agent-v1",
) -> Path:
    from web import app as web_app

    web_app.HASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_serial = "".join(c for c in serial if c.isalnum() or c in ("-", "_"))
    if not safe_serial:
        safe_serial = "noserial"
    safe_source = "".join(c for c in source if c.isalnum() or c in ("-", "_"))
    if not safe_source:
        safe_source = "hash"
    out = web_app.HASH_DIR / f"{ts}-vm{vmid}-{safe_serial}-{safe_source}_hwid.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        header = [
            "Device Serial Number",
            "Windows Product ID",
            "Hardware Hash",
        ]
        row = [serial, product_id, hardware_hash]
        if group_tag:
            header.append("Group Tag")
            row.append(group_tag)
        writer.writerow(header)
        writer.writerow(row)
    return out


@router.post("/bootstrap")
def bootstrap_agent(
    body: BootstrapBody,
    authorization: Optional[str] = Header(None),
):
    bootstrap_token = _bearer(authorization)
    token_run_id = _bootstrap_run_id(bootstrap_token, body.run_id)
    run_id = body.run_id or token_run_id
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
        if _fleet_bootstrap_token_matches(bootstrap_token):
            approval = agent_telemetry_pg.create_bootstrap_approval(
                conn,
                bootstrap_token=bootstrap_token,
                agent_id=body.agent_id,
                phase=body.phase,
                vmid=vmid,
                vm_uuid=vm_uuid,
                computer_name=computer_name,
                serial_number=serial_number,
                agent_version=body.agent_version,
                created_from_run_id=run_id,
            )
            if (
                approval["status"] == "pending"
                and _buildhost_auto_approval_matches(
                    body,
                    vmid=vmid,
                    computer_name=computer_name,
                )
            ):
                approval = agent_telemetry_pg.approve_bootstrap_approval(
                    conn,
                    approval["approval_id"],
                )
            if approval["status"] == "approved" and approval.get("agent_token"):
                agent_telemetry_pg.mark_bootstrap_approval_claimed(
                    conn,
                    approval["approval_id"],
                )
                return {
                    "schema_version": 1,
                    "agent_id": body.agent_id,
                    "approval_id": approval["approval_id"],
                    "approval_status": approval["status"],
                    "agent_token": approval["agent_token"],
                    "heartbeat_interval_seconds": _HEARTBEAT_INTERVAL_SECONDS,
                    "server_time": datetime.now(timezone.utc).isoformat(),
                }
            return {
                "schema_version": 1,
                "agent_id": body.agent_id,
                "approval_id": approval["approval_id"],
                "approval_status": approval["status"],
                "poll_url": f"/api/agent/v1/bootstrap/claim/{approval['approval_id']}",
                "retry_after_seconds": 5,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        agent_token = agent_telemetry_pg.new_agent_token()
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


@router.get("/bootstrap/claim/{approval_id}")
def claim_bootstrap_approval(
    approval_id: str,
    authorization: Optional[str] = Header(None),
):
    bootstrap_token = _bearer(authorization)
    approval = None
    with _conn() as conn:
        approval = agent_telemetry_pg.claim_bootstrap_approval(
            conn,
            approval_id=approval_id,
            bootstrap_token=bootstrap_token,
        )
    if not approval:
        raise HTTPException(status_code=401, detail="invalid bootstrap approval")
    if approval["status"] != "approved":
        return {
            "schema_version": 1,
            "agent_id": approval["agent_id"],
            "approval_id": approval["approval_id"],
            "approval_status": approval["status"],
            "retry_after_seconds": 5,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    if not approval.get("agent_token"):
        raise HTTPException(status_code=409, detail="approved token is not ready")
    return {
        "schema_version": 1,
        "agent_id": approval["agent_id"],
        "approval_id": approval["approval_id"],
        "approval_status": approval["status"],
        "agent_token": approval["agent_token"],
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
        if body.bubble_id and body.dc_readiness:
            from web import lab_bubbles_pg

            lab_bubbles_pg.init(conn)
            dc_asset = lab_bubbles_pg.asset_for_agent(
                conn,
                body.bubble_id,
                body.agent_id,
            )
            if dc_asset and dc_asset["asset_role"] == "domain_controller":
                lab_bubbles_pg.update_readiness_from_dc_evidence(
                    conn,
                    body.bubble_id,
                    dc_asset_id=dc_asset["id"],
                    evidence=body.dc_readiness,
                )
        if body.current_run_id and (body.current_phase or "").lower() == "full_os":
            from web import osdeploy_pg

            osdeploy_pg.init(conn)
            osdeploy_pg.mark_complete_from_heartbeat(
                conn,
                run_id=body.current_run_id,
                agent_id=body.agent_id,
                heartbeat=payload,
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


@router.post("/work/next")
def next_work(body: WorkNextBody, device: dict = Depends(_require_agent)):
    if body.agent_id != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/agent mismatch")
    kinds = [kind for kind in body.supported_kinds if kind]
    with _conn() as conn:
        row = agent_telemetry_pg.claim_next_work_item(
            conn,
            agent_id=body.agent_id,
            supported_kinds=kinds,
        )
    return {"work_item": _public_work_item(row) if row else None}


@router.post("/work/{work_item_id}/complete")
def complete_work(
    work_item_id: str,
    body: WorkCompleteBody,
    device: dict = Depends(_require_agent),
):
    if body.agent_id != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/agent mismatch")
    with _conn() as conn:
        row = agent_telemetry_pg.complete_work_item(
            conn,
            work_item_id,
            agent_id=body.agent_id,
            result=body.result,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return {"status": "ok", "work_item": _public_work_item(row)}


@router.post("/work/{work_item_id}/fail")
def fail_work(
    work_item_id: str,
    body: WorkFailBody,
    device: dict = Depends(_require_agent),
):
    if body.agent_id != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/agent mismatch")
    with _conn() as conn:
        row = agent_telemetry_pg.fail_work_item(
            conn,
            work_item_id,
            agent_id=body.agent_id,
            error=body.error,
            result=body.result,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return {"status": "ok", "work_item": _public_work_item(row)}


@router.get("/hash-script")
def hash_script(device: dict = Depends(_require_agent)):
    root = Path(__file__).resolve().parents[1]
    script = root / "files" / "Get-WindowsAutopilotInfo.ps1"
    try:
        content = script.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="hash capture script is missing")
    return Response(content=content, media_type="text/plain; charset=utf-8")


@router.post("/hash")
def post_hash(body: HashBody, device: dict = Depends(_require_agent)):
    work = None
    with _conn() as conn:
        if body.work_item_id:
            work = agent_telemetry_pg.get_work_item(conn, body.work_item_id)
            if work is None:
                raise HTTPException(status_code=404, detail="work item not found")
            if work["agent_id"] != device["agent_id"]:
                raise HTTPException(status_code=403, detail="token/work mismatch")
        latest = agent_telemetry_pg.latest_for_agent(conn, device["agent_id"])
        vmid = (
            work.get("vmid")
            if work and work.get("vmid") is not None
            else latest.get("vmid") if latest else device.get("vmid")
        )
        if vmid is None:
            raise HTTPException(status_code=409, detail="agent has no VMID")
        request = work.get("request_json") if work else {}
        group_tag = str((request or {}).get("group_tag") or "")
        path = _persist_autopilot_hash(
            vmid=int(vmid),
            serial=body.serial_number,
            product_id=body.product_id,
            hardware_hash=body.hardware_hash,
            group_tag=group_tag,
            source="agent-v1",
        )
        result = {
            "filename": path.name,
            "vmid": int(vmid),
            "serial_number": body.serial_number,
            "product_id": body.product_id,
            "group_tag": group_tag,
            "source": "agent-v1",
        }
        if body.work_item_id:
            completed = agent_telemetry_pg.complete_work_item(
                conn,
                body.work_item_id,
                agent_id=device["agent_id"],
                result=result,
            )
            if completed is None:
                raise HTTPException(
                    status_code=409,
                    detail="work item is already terminal",
                )
    return {"ok": True, **result}


@router.post("/artifacts")
async def upload_artifact(
    work_item_id: str = Form(...),
    artifact_kind: str = Form(...),
    metadata_json: str = Form("{}"),
    file: UploadFile = File(...),
    device: dict = Depends(_require_agent),
):
    safe_kind = "".join(
        ch for ch in artifact_kind.strip().lower()
        if ch.isalnum() or ch in ("-", "_", ".")
    )
    if safe_kind not in {
        "agent-msi",
        "winpe-iso",
        "cloudosd-iso",
        "osdeploy-iso",
        "manifest",
        "wim",
        "log",
    }:
        raise HTTPException(status_code=400, detail="unsupported artifact kind")
    with _conn() as conn:
        work = agent_telemetry_pg.get_work_item(conn, work_item_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work item not found")
    if work["agent_id"] != device["agent_id"]:
        raise HTTPException(status_code=403, detail="token/work mismatch")
    try:
        metadata = json.loads(metadata_json or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid metadata_json: {exc}") from exc
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata_json must be an object")

    filename = Path(file.filename or f"{safe_kind}.bin").name
    target = setup_artifacts.safe_artifact_path(safe_kind, filename)
    with target.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    row = setup_artifacts.register_existing_artifact(
        kind=safe_kind,
        path=target,
        metadata=metadata,
        producer_agent_id=device["agent_id"],
        work_item_id=work_item_id,
    )
    return {"ok": True, "artifact": row}


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
