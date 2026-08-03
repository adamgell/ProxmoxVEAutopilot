from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from web import agent_telemetry_pg, setup_artifacts
from web.agent_v1_endpoints import _conn, _public_work_item


router = APIRouter(prefix="/api/setup-cm/v1", tags=["setup-cm"])

_SETUP_CM_ROOT = "C:\\ProgramData\\SetupCm\\"
_SETUP_CM_VAULT_ROOT = "\\\\LABZ1-DC02\\SetupCm\\"
_SETUP_CM_MODULE_MAX_BYTES = 64 * 1024 * 1024
_SETUP_CM_MODULE_UPLOAD_CHUNK_BYTES = 1024 * 1024
_LABZ1_CLIENT_NETWORK_REPAIR_AGENT_ID = "agent-ring0ivy24-01"
_WORK_KIND_BY_STAGE = {
    "acquire": "setup_cm_acquire",
    "sql": "setup_cm_sql",
    "mecm": "setup_cm_mecm",
    "health": "setup_cm_health",
}


def _is_inside(value: str, root: str) -> bool:
    normalized = value.replace("/", "\\").strip()
    prefix = root.rstrip("\\") + "\\"
    if not normalized.casefold().startswith(prefix.casefold()):
        return False
    return all(part not in {".", ".."} for part in normalized.split("\\") if part)


class SetupCmWorkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["acquire", "sql", "mecm", "health"]
    config_path: str = Field(min_length=1)
    evidence_root: str = Field(min_length=1)
    module_archive_path: str = Field(min_length=1)
    module_archive_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_paths(self) -> "SetupCmWorkBody":
        if not _is_inside(self.config_path, _SETUP_CM_ROOT) or not self.config_path.lower().endswith(".yaml"):
            raise ValueError("config_path must be a YAML file below C:\\ProgramData\\SetupCm")
        if not _is_inside(self.evidence_root, _SETUP_CM_ROOT):
            raise ValueError("evidence_root must be below C:\\ProgramData\\SetupCm")
        if (
            not _is_inside(self.module_archive_path, _SETUP_CM_ROOT)
            and not _is_inside(self.module_archive_path, _SETUP_CM_VAULT_ROOT)
        ) or not self.module_archive_path.lower().endswith(".zip"):
            raise ValueError("module_archive_path must be a ZIP below an approved Setup-CM root")
        return self


@router.post("/module-artifacts", status_code=201)
async def upload_setup_cm_module_artifact(
    file: UploadFile = File(...),
    sha256: str = Form(...),
    source_commit: str = Form(...),
):
    if Path(file.filename or "").suffix.casefold() != ".zip":
        raise HTTPException(status_code=422, detail="file must be a ZIP")

    target = setup_artifacts.safe_artifact_path("setup-cm-module", "setup-cm.zip")
    total_bytes = 0
    try:
        with target.open("xb") as handle:
            while chunk := await file.read(_SETUP_CM_MODULE_UPLOAD_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > _SETUP_CM_MODULE_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="setup-cm-module exceeds 64 MiB",
                    )
                handle.write(chunk)
        return setup_artifacts.register_existing_artifact(
            kind="setup-cm-module",
            path=target,
            metadata={"sha256": sha256, "source_commit": source_commit},
        )
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


class SetupCmClientInstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_code: str = Field(pattern=r"^[A-Z0-9]{3}$")
    management_point_fqdn: str = Field(min_length=1)
    evidence_root: str = Field(min_length=1)
    module_archive_path: str = Field(min_length=1)
    module_archive_sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_client_install(self) -> "SetupCmClientInstallBody":
        if self.site_code != "LAB":
            raise ValueError("site_code must be LAB")
        if self.management_point_fqdn.casefold() != "labz1-cm01.test.gell.one":
            raise ValueError("management_point_fqdn must be LABZ1-CM01.test.gell.one")
        if not _is_inside(self.evidence_root, _SETUP_CM_ROOT):
            raise ValueError("evidence_root must be below C:\\ProgramData\\SetupCm")
        if (
            not _is_inside(self.module_archive_path, _SETUP_CM_ROOT)
            and not _is_inside(self.module_archive_path, _SETUP_CM_VAULT_ROOT)
        ) or not self.module_archive_path.lower().endswith(".zip"):
            raise ValueError("module_archive_path must be a ZIP below an approved Setup-CM root")
        return self


class SetupCmModulePublicationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class SetupCmSourceDiagnosticsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_code: str = Field(pattern=r"^[A-Z0-9]{3}$")
    target_computer_name: str = Field(pattern=r"^[A-Za-z0-9-]{1,63}$")


class SetupCmContentLocationDiagnosticsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_code: Literal["LAB"]
    target_computer_name: str = Field(pattern=r"^[A-Za-z0-9-]{1,63}$")
    client_ipv4: str

    @field_validator("client_ipv4")
    @classmethod
    def validate_client_ipv4(cls, value: str) -> str:
        try:
            parsed = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError as exc:
            raise ValueError("client_ipv4 must be an IPv4 address") from exc
        return str(parsed)


class SetupCmContentLocationRemediationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_code: Literal["LAB"]
    client_subnet: Literal["192.168.16.0/24"]
    boundary_group_name: Literal["LABZ1 Client Network"]
    distribution_point_fqdn: Literal["LABZ1-CM01.test.gell.one"]


@router.post("/agents/{agent_id}/work", status_code=202)
def queue_setup_cm_work(agent_id: str, body: SetupCmWorkBody):
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        request = body.model_dump()
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind=_WORK_KIND_BY_STAGE[body.stage],
            request=request,
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/client-install", status_code=202)
def queue_setup_cm_client_install(agent_id: str, body: SetupCmClientInstallBody):
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="setup_cm_client_install",
            request=body.model_dump(),
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/client-network-repair", status_code=202)
def queue_setup_cm_client_network_repair(agent_id: str):
    if agent_id != _LABZ1_CLIENT_NETWORK_REPAIR_AGENT_ID:
        raise HTTPException(
            status_code=422,
            detail=(
                "client network repair target must be "
                "agent-ring0ivy24-01"
            ),
        )
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="setup_cm_client_network_repair",
            request={},
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/module-publications", status_code=202)
def queue_setup_cm_module_publication(
    agent_id: str,
    body: SetupCmModulePublicationBody,
):
    if agent_id != "agent-labz1-dc02":
        raise HTTPException(
            status_code=422,
            detail="module publication target must be agent-labz1-dc02",
        )
    artifact = setup_artifacts.get_artifact(body.artifact_id, kind="setup-cm-module")
    if not artifact:
        raise HTTPException(status_code=404, detail="setup-cm-module artifact not found")
    metadata = artifact.get("metadata") or {}
    request = {
        "artifact_id": artifact["artifact_id"],
        "archive_sha256": artifact["sha256"],
        "source_commit": metadata["source_commit"],
    }
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="publish_setup_cm_module",
            request=request,
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/source-diagnostics", status_code=202)
def queue_setup_cm_source_diagnostics(
    agent_id: str,
    body: SetupCmSourceDiagnosticsBody,
):
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="setup_cm_diagnostics",
            request=body.model_dump(),
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/source-access", status_code=202)
def queue_setup_cm_source_access(
    agent_id: str,
    body: SetupCmSourceDiagnosticsBody,
):
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="setup_cm_source_access",
            request=body.model_dump(),
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/content-location-diagnostics", status_code=202)
def queue_setup_cm_content_location_diagnostics(
    agent_id: str,
    body: SetupCmContentLocationDiagnosticsBody,
):
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="setup_cm_content_location_diagnostics",
            request=body.model_dump(),
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)


@router.post("/agents/{agent_id}/content-location-remediation", status_code=202)
def queue_setup_cm_content_location_remediation(
    agent_id: str,
    body: SetupCmContentLocationRemediationBody,
):
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        if not device:
            raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
        work = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="setup_cm_content_location_remediation",
            request=body.model_dump(),
            vmid=device.get("vmid"),
        )
    return _public_work_item(work)
