from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from web import agent_telemetry_pg
from web.agent_v1_endpoints import _conn, _public_work_item


router = APIRouter(prefix="/api/setup-cm/v1", tags=["setup-cm"])

_SETUP_CM_ROOT = "C:\\ProgramData\\SetupCm\\"
_SETUP_CM_VAULT_ROOT = "\\\\LABZ1-DC02\\SetupCm\\"
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
