"""CloudOSD controller and WinPE bridge API."""
from __future__ import annotations

import logging
import os
import re
import sys
from hashlib import sha256
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from web import agent_telemetry_pg, cloudosd_pg, osd_package, winpe_token
from web.sequence_compiler import _split_domain_user


router = APIRouter(prefix="/api/cloudosd", tags=["cloudosd"])
logger = logging.getLogger(__name__)

_PE_TOKEN_TTL_SECONDS = 6 * 60 * 60
_AGENT_BOOTSTRAP_TOKEN_TTL_SECONDS = 48 * 60 * 60
_MIN_CLOUDOSD_MEMORY_MB = cloudosd_pg.MIN_VM_MEMORY_MB
_RECOMMENDED_CLOUDOSD_MEMORY_MB = cloudosd_pg.RECOMMENDED_VM_MEMORY_MB
_MIN_CLOUDOSD_DISK_GB = cloudosd_pg.MIN_VM_DISK_SIZE_GB
_APP_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _cloudosd_source_root() -> Path:
    configured = os.environ.get("CLOUDOSD_SOURCE_ROOT", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend([_APP_ROOT, _REPO_ROOT])
    for root in candidates:
        if (root / "tools" / "cloudosd-build" / "build-cloudosd.ps1").exists():
            return root
    return _APP_ROOT


_CLOUDOSD_SOURCE_ROOT = _cloudosd_source_root()
_CLOUDOSD_TOOL_ROOT = _CLOUDOSD_SOURCE_ROOT / "tools" / "cloudosd-build"


class RunCreateBody(BaseModel):
    artifact_id: str = Field(min_length=1)
    vm_name: str = Field(min_length=1)
    node: Optional[str] = None
    iso_storage: Optional[str] = None
    storage: Optional[str] = None
    network_bridge: Optional[str] = None
    vmid: Optional[int] = Field(default=None, ge=1)
    architecture: str = "amd64"
    os_version: str = cloudosd_pg.DEFAULT_OS_VERSION
    os_activation: str = cloudosd_pg.DEFAULT_OS_ACTIVATION
    os_edition: str = cloudosd_pg.DEFAULT_OS_EDITION
    os_language: str = cloudosd_pg.DEFAULT_OS_LANGUAGE
    vm_cores: int = Field(default=cloudosd_pg.DEFAULT_VM_CORES, ge=1)
    vm_memory_mb: int = Field(default=cloudosd_pg.DEFAULT_VM_MEMORY_MB, ge=1)
    vm_disk_size_gb: int = Field(default=cloudosd_pg.DEFAULT_VM_DISK_SIZE_GB, ge=1)
    vm_group_tag: str = ""
    vm_oem_profile: str = ""
    chassis_type_override: int = Field(default=0, ge=0)
    source_surface: str = "cloudosd"
    source_sequence_id: Optional[int] = Field(default=None, ge=1)
    tpm_enabled: bool = True
    secure_boot: bool = True
    firmware_updates_enabled: bool = False
    driver_pack_policy: str = cloudosd_pg.DEFAULT_DRIVER_PACK_POLICY
    analytics_enabled: bool = False
    outbound_policy: dict = Field(default_factory=dict)


class RunIdentityBody(BaseModel):
    vmid: int = Field(ge=1)
    vm_uuid: str = Field(min_length=1)
    mac: str = Field(min_length=1)
    node: Optional[str] = None
    computer_name: Optional[str] = None


class PeRegisterBody(BaseModel):
    vm_uuid: str = Field(min_length=1)
    mac: str = Field(min_length=1)
    architecture: str = "amd64"
    build_sha: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    disks: list[dict] = Field(default_factory=list)
    network: list[dict] = Field(default_factory=list)


class EventBody(BaseModel):
    phase: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    severity: str = Field(default="info", pattern=r"^(debug|info|warning|error)$")
    message: Optional[str] = None
    data: dict = Field(default_factory=dict)


class ArtifactBuildBody(BaseModel):
    remote: str = "Adam.Gell@10.211.55.6"
    remote_root: str = r"F:\BuildRoot"
    architecture: str = "amd64"
    osdcloud_module_version: str = cloudosd_pg.DEFAULT_OSDCLOUD_MODULE_VERSION


class ArtifactPublishBody(BaseModel):
    node: Optional[str] = None
    storage: Optional[str] = None


def _database_url() -> str:
    from web import app as web_app

    try:
        return web_app._database_url()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="CloudOSD database is not configured",
        )


@contextmanager
def _conn():
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        cloudosd_pg.init(conn)
        agent_telemetry_pg.init(conn)
        yield conn


def _base_url(request: Request | None = None) -> str:
    configured = os.environ.get("AUTOPILOT_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    if request is not None:
        proto = (
            request.headers.get("x-forwarded-proto")
            or request.url.scheme
            or ""
        ).split(",", 1)[0].strip()
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
            or ""
        ).split(",", 1)[0].strip()
        if proto and host and host not in {"127.0.0.1:5000", "localhost:5000"}:
            return f"{proto}://{host}".rstrip("/")
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:5000"


def _sign(run_id: str, *, ttl_seconds: int) -> str:
    return winpe_token.sign(run_id=run_id, ttl_seconds=ttl_seconds)


def _require_bearer(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    try:
        return winpe_token.verify(authorization.removeprefix("Bearer ").strip())
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid token")


def _require_run_token(run_id: str, payload: dict) -> None:
    if str(payload["run_id"]) != str(run_id):
        raise HTTPException(status_code=403, detail="token/run mismatch")


def _package_response(
    *,
    run: dict,
    artifact: dict,
    server_base_url: str,
    domain_join_secret: dict | None = None,
) -> dict:
    bootstrap_token = _sign(
        run["run_id"],
        ttl_seconds=_AGENT_BOOTSTRAP_TOKEN_TTL_SECONDS,
    )
    pe_token = _sign(run["run_id"], ttl_seconds=_PE_TOKEN_TTL_SECONDS)
    first_boot = _asset_metadata("PVEAutopilot-FirstBoot.ps1")
    postinstall = _asset_metadata("autopilotagent-postinstall.ps1")
    agent_msi = _asset_metadata("autopilotagent.msi", required=False)
    expected_computer_name = (
        run.get("expected_computer_name")
        or cloudosd_pg.normalize_windows_computer_name(run.get("vm_name"))
    )
    response = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "bearer_token": pe_token,
        "workflow_name": run["workflow_name"],
        "server_base_url": server_base_url,
        "artifact": artifact,
        "identity": {
            "vmid": run["vmid"],
            "vm_uuid": run["vm_uuid"],
            "mac": run["mac"],
            "node": run["node"],
            "requested_name": run.get("requested_vm_name") or run["vm_name"],
            "pve_name": run.get("pve_vm_name"),
            "computer_name": expected_computer_name,
        },
        "os_settings": cloudosd_pg.os_settings(run),
        "user_settings": cloudosd_pg.user_settings(run),
        "task": cloudosd_pg.task_settings(run),
        "deployment": {
            "path": "cloudosd",
            "source_surface": run.get("source_surface") or "cloudosd",
            "source_sequence_id": run.get("source_sequence_id"),
            "group_tag": run.get("vm_group_tag") or "",
            "oem_profile": run.get("vm_oem_profile") or "",
            "chassis_type_override": run.get("chassis_type_override"),
        },
        "payloads": {
            "osd_client": {
                "url": (
                    f"{server_base_url}/osd/v2/agent/package/{run['run_id']}"
                    "?phase=full_os"
                ),
                "sha256": None,
            },
            "autopilotagent_msi": {
                "url": f"{server_base_url}/api/cloudosd/assets/autopilotagent.msi",
                "sha256": agent_msi["sha256"],
                "available": agent_msi["available"],
            },
            "autopilotagent_postinstall": {
                "url": (
                    f"{server_base_url}/api/cloudosd/assets/"
                    "autopilotagent-postinstall.ps1"
                ),
                "sha256": postinstall["sha256"],
            },
            "first_boot_script": {
                "url": (
                    f"{server_base_url}/api/cloudosd/assets/"
                    "PVEAutopilot-FirstBoot.ps1"
                ),
                "sha256": first_boot["sha256"],
            },
        },
        "agent": {
            "phase": "cloudosd",
            "bootstrap_token": bootstrap_token,
            "bootstrap_url": f"{server_base_url}/api/agent/v1/bootstrap",
            "config_url": f"{server_base_url}/api/agent/v1/config",
            "run_id": run["run_id"],
            "vmid": run["vmid"],
        },
    }
    domain_join = domain_join_secret or _domain_join_package_stub(run)
    if domain_join.get("enabled"):
        response["domain_join"] = domain_join
    return response


def _domain_join_package_stub(run: dict) -> dict:
    domain_join = run.get("domain_join") or {}
    if not domain_join.get("enabled"):
        return {"enabled": False}
    return {key: value for key, value in domain_join.items() if key != "credential_id"}


def _domain_join_secret_for_run(run: dict) -> dict:
    domain_join = run.get("domain_join") or {}
    if not domain_join.get("enabled"):
        return {"enabled": False}
    credential_id = domain_join.get("credential_id")
    if not credential_id:
        raise HTTPException(
            status_code=409,
            detail="CloudOSD domain join is enabled but no credential_id is stored",
        )
    try:
        from web import app as web_app, sequences_pg

        credential = sequences_pg.get_credential(
            web_app.SEQUENCES_DB,
            web_app._cipher(),
            int(credential_id),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail=f"CloudOSD domain join credential could not be resolved: {exc}",
        ) from exc
    if not credential:
        raise HTTPException(
            status_code=409,
            detail=f"CloudOSD domain join credential id={credential_id} was not found",
        )
    payload = credential.get("payload") or {}
    raw_username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not raw_username or not password:
        raise HTTPException(
            status_code=409,
            detail="CloudOSD domain join credential is missing username or password",
        )
    user_domain, username = _split_domain_user(raw_username)
    package = _domain_join_package_stub(run)
    package.update({
        "credential_domain": package.get("credential_domain") or user_domain,
        "username": username,
        "password": password,
    })
    return package


def _related_jobs(run_id: str) -> list[dict]:
    try:
        from web import app as web_app

        return [
            job for job in web_app.job_manager.list_jobs()
            if (job.get("args") or {}).get("cloudosd_run_id") == run_id
        ]
    except Exception:
        return []


def _identity_candidates(run: dict, heartbeat: dict | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in (
        run.get("expected_computer_name"),
        run.get("pve_vm_name"),
        run.get("requested_vm_name"),
        run.get("vm_name"),
        (heartbeat or {}).get("computer_name"),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def intune_evidence_for_run(run: dict, heartbeat: dict | None = None) -> dict:
    candidates = _identity_candidates(run, heartbeat)
    candidate_keys = {item.casefold() for item in candidates}
    matched_hashes: list[dict] = []
    cache_error = ""
    try:
        from web import app as web_app

        for item in web_app.get_hash_files():
            serial = str(item.get("serial") or "").strip()
            filename = str(item.get("name") or "")
            serial_match = serial and serial.casefold() in candidate_keys
            vmid_match = (
                run.get("vmid") is not None
                and f"-vm{run['vmid']}-".casefold() in filename.casefold()
            )
            if serial_match or vmid_match:
                matched = dict(item)
                matched_hashes.append(matched)
                if serial and serial.casefold() not in candidate_keys:
                    candidate_keys.add(serial.casefold())
                    candidates.append(serial)
    except Exception as exc:
        cache_error = f"hash evidence unavailable: {exc}"

    group = None
    synced_at = ""
    try:
        from web import devices_pg

        groups, extra = devices_pg.list_grouped(windows_only=False)
        synced_at = (extra.get("meta") or {}).get("synced_at") or ""
        by_serial = {
            str(row.get("serial") or "").casefold(): row
            for row in groups
            if row.get("serial")
        }
        for key in candidate_keys:
            if key in by_serial:
                group = by_serial[key]
                break
    except Exception as exc:
        if cache_error:
            cache_error = f"{cache_error}; device cache unavailable: {exc}"
        else:
            cache_error = f"device cache unavailable: {exc}"

    autopilot = (group or {}).get("autopilot") or None
    intune = (group or {}).get("intune") or None
    entra = (group or {}).get("entra") or []
    upload_status = "uploaded" if autopilot else "not_found"
    assignment_status = (
        autopilot.get("profile_status")
        if autopilot and autopilot.get("profile_status")
        else "not_found"
    )
    enrollment_status = (
        "enrolled"
        if intune
        else (
            autopilot.get("enrollment_state")
            if autopilot and autopilot.get("enrollment_state")
            else "not_found"
        )
    )
    if matched_hashes and autopilot:
        summary = f"hash captured; Autopilot upload found; assignment {assignment_status}; enrollment {enrollment_status}"
    elif matched_hashes:
        summary = "hash captured; waiting for Autopilot upload evidence"
    elif autopilot:
        summary = f"Autopilot upload found without a matching local hash file; assignment {assignment_status}"
    else:
        summary = "waiting for local hash capture and Autopilot upload evidence"

    return {
        "schema_version": 1,
        "summary": summary,
        "serial_candidates": candidates,
        "cache_synced_at": synced_at,
        "error": cache_error,
        "hash": {
            "status": "captured" if matched_hashes else "missing",
            "files": matched_hashes,
        },
        "autopilot": {
            "status": upload_status,
            "id": autopilot.get("id") if autopilot else None,
            "serial": autopilot.get("serial") if autopilot else None,
            "group_tag": autopilot.get("group_tag") if autopilot else None,
            "profile_status": autopilot.get("profile_status") if autopilot else None,
            "enrollment_state": autopilot.get("enrollment_state") if autopilot else None,
            "last_contact": autopilot.get("last_contact") if autopilot else None,
            "display_name": autopilot.get("display_name") if autopilot else None,
        },
        "assignment": {
            "status": assignment_status,
            "group_tag": autopilot.get("group_tag") if autopilot else None,
            "profile_status": autopilot.get("profile_status") if autopilot else None,
        },
        "enrollment": {
            "status": enrollment_status,
            "autopilot_enrollment_state": autopilot.get("enrollment_state") if autopilot else None,
            "intune_device_id": intune.get("id") if intune else None,
            "intune_device_name": intune.get("device_name") if intune else None,
            "management_state": intune.get("management_state") if intune else None,
            "compliance_state": intune.get("compliance_state") if intune else None,
            "last_sync": intune.get("last_sync") if intune else None,
            "enrolled_date": intune.get("enrolled_date") if intune else None,
        },
        "entra": {
            "count": len(entra),
            "devices": entra,
        },
    }


def _job_event(job: dict) -> dict:
    status = str(job.get("status") or "unknown")
    severity = "error" if status in {"failed", "canceled"} else "info"
    job_type = job.get("job_type") or job.get("playbook") or "provision_cloudosd"
    job_id = job.get("id") or "unknown"
    return {
        "id": f"job:{job_id}",
        "run_id": str((job.get("args") or {}).get("cloudosd_run_id") or ""),
        "phase": "proxmox_playbook",
        "event_type": "provision_job_status",
        "severity": severity,
        "message": f"{job_type} job {job_id} is {status}",
        "data": {
            "job_id": job_id,
            "job_type": job_type,
            "status": status,
            "playbook": job.get("playbook"),
        },
        "created_at": job.get("ended") or job.get("ended_at") or job.get("started") or job.get("created_at"),
    }


def _event_by_type(events: list[dict], event_type: str) -> dict | None:
    target = event_type.casefold()
    for event in events:
        if str(event.get("event_type") or "").casefold() == target:
            return event
    return None


def _derived_event(
    *,
    run_id: str,
    phase: str,
    event_type: str,
    message: str,
    source_event: dict | None = None,
    created_at: str | None = None,
    data: dict | None = None,
) -> dict:
    payload = {
        "derived": True,
        **(data or {}),
    }
    if source_event:
        payload["source_event_id"] = source_event.get("id")
        payload["source_event_type"] = source_event.get("event_type")
    return {
        "id": f"derived:{run_id}:{event_type}",
        "run_id": run_id,
        "phase": phase,
        "event_type": event_type,
        "severity": "info",
        "message": message,
        "data": payload,
        "created_at": created_at or (source_event or {}).get("created_at"),
    }


def _derived_lifecycle_events(run_id: str, events: list[dict], run: dict | None) -> list[dict]:
    """Fill normalized milestone groups from existing CloudOSD completion gates.

    Older CloudOSD runs did not post every planned normalized event, but the
    controller still has hard gates: PE completion only happens after staging
    and offline validation, and run completion only happens after the agent
    heartbeat. These synthetic events are marked as derived so the UI can make
    that distinction without leaving the milestone groups empty.
    """
    existing = {
        str(event.get("event_type") or "").casefold()
        for event in events
    }
    derived: list[dict] = []
    pe_complete = _event_by_type(events, "cloudosd_pe_complete")
    if pe_complete and "offline_validation_ok" not in existing:
        derived.append(_derived_event(
            run_id=run_id,
            phase="offline_validation",
            event_type="offline_validation_ok",
            message="Offline validation passed before CloudOSD PE completion (derived from PE completion gate)",
            source_event=pe_complete,
        ))
    if pe_complete and "setupcomplete_chained" not in existing:
        derived.append(_derived_event(
            run_id=run_id,
            phase="setupcomplete",
            event_type="setupcomplete_chained",
            message="SetupComplete first-boot chain was staged before CloudOSD PE completion (derived from PE completion gate)",
            source_event=pe_complete,
        ))

    first_heartbeat_at = (run or {}).get("first_heartbeat_at")
    heartbeat_event = _event_by_type(events, "autopilotagent_heartbeat")
    if (
        (first_heartbeat_at or heartbeat_event or (run or {}).get("state") == "complete")
        and "firstboot_complete" not in existing
    ):
        derived.append(_derived_event(
            run_id=run_id,
            phase="first_boot",
            event_type="firstboot_complete",
            message="First boot reached AutopilotAgent heartbeat gate (derived from run completion evidence)",
            source_event=heartbeat_event,
            created_at=first_heartbeat_at or (heartbeat_event or {}).get("created_at"),
            data={"first_heartbeat_at": first_heartbeat_at},
        ))
    return derived


def events_with_related_jobs(
    run_id: str,
    events: list[dict],
    run: dict | None = None,
) -> list[dict]:
    """Return CloudOSD events plus derived lifecycle and Proxmox playbook evidence."""
    derived = [
        *_derived_lifecycle_events(run_id, events, run),
        *[_job_event(job) for job in _related_jobs(run_id)],
    ]
    return [*events, *derived]


def _asset_path(name: str) -> Path:
    if name == "PVEAutopilot-FirstBoot.ps1":
        return _CLOUDOSD_TOOL_ROOT / name
    if name == "Invoke-CloudOSDBridge.ps1":
        return _CLOUDOSD_TOOL_ROOT / name
    if name == "autopilotagent-postinstall.ps1":
        return _APP_ROOT / "files" / "ninja" / "autopilotagent-postinstall.ps1"
    if name == "autopilotagent.msi":
        configured = os.environ.get("AUTOPILOT_AGENT_MSI_PATH", "").strip()
        if configured:
            return Path(configured)
        app_output = _APP_ROOT / "output" / "cloudosd" / "AutopilotAgent.msi"
        if app_output.exists():
            return app_output
        return _REPO_ROOT / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi"
    raise HTTPException(status_code=404, detail="CloudOSD asset not found")


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_metadata(name: str, *, required: bool = True) -> dict:
    path = _asset_path(name)
    if not path.exists():
        if required:
            raise HTTPException(
                status_code=500,
                detail=f"CloudOSD asset is missing: {name}",
            )
        return {"available": False, "sha256": None}
    return {
        "available": True,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _asset_metadata_status(name: str) -> dict:
    path = _asset_path(name)
    if not path.exists():
        return {
            "name": name,
            "available": False,
            "required": True,
            "path": str(path),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "name": name,
        "available": True,
        "required": True,
        "path": str(path),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _osd_client_package_status() -> dict:
    try:
        package_root = osd_package.files_dir()
        sources = [
            package_root / relative_path
            for relative_path, _ in osd_package.PACKAGE_FILES
        ]
        missing = [str(path) for path in sources if not path.is_file()]
        if missing:
            return {
                "name": "OSD client package",
                "available": False,
                "required": True,
                "path": str(package_root),
                "sha256": None,
                "size_bytes": None,
                "missing": missing,
            }
        digest = sha256()
        size = 0
        for source in sources:
            digest.update(source.name.encode("utf-8"))
            content = source.read_bytes()
            digest.update(content)
            size += len(content)
        return {
            "name": "OSD client package",
            "available": True,
            "required": True,
            "path": str(package_root),
            "sha256": digest.hexdigest(),
            "size_bytes": size,
            "files": [str(path) for path in sources],
        }
    except Exception as exc:
        return {
            "name": "OSD client package",
            "available": False,
            "required": True,
            "path": None,
            "sha256": None,
            "size_bytes": None,
            "error": str(exc),
        }


def assets_status_payload() -> dict:
    assets = {
        "autopilotagent_msi": _asset_metadata_status("autopilotagent.msi"),
        "first_boot_script": _asset_metadata_status("PVEAutopilot-FirstBoot.ps1"),
        "bridge_script": _asset_metadata_status("Invoke-CloudOSDBridge.ps1"),
        "postinstall_script": _asset_metadata_status("autopilotagent-postinstall.ps1"),
        "osd_client_package": _osd_client_package_status(),
    }
    required = [
        "autopilotagent_msi",
        "first_boot_script",
        "bridge_script",
        "postinstall_script",
        "osd_client_package",
    ]
    return {
        "schema_version": 1,
        "ready": all(assets[key]["available"] for key in required),
        "assets": assets,
    }


def _artifact_readiness(artifact: dict | None) -> str:
    if not artifact:
        return "missing"
    sha_pattern = re.compile(r"^[A-Fa-f0-9]{64}$")
    if not sha_pattern.match(artifact.get("iso_sha256") or ""):
        return "missing_hash"
    if not sha_pattern.match(artifact.get("wim_sha256") or ""):
        return "missing_hash"
    if not artifact.get("proxmox_volid"):
        return "not_uploaded"
    return "ready"


def enrich_artifact(artifact: dict | None) -> dict | None:
    if not artifact:
        return None
    enriched = dict(artifact)
    readiness = _artifact_readiness(enriched)
    enriched["readiness"] = readiness
    enriched["ready"] = readiness == "ready"
    if enriched.get("build_job_id"):
        enriched["build_job_url"] = f"/jobs/{enriched['build_job_id']}"
        enriched["build_log_url"] = f"/jobs/{enriched['build_job_id']}"
    else:
        enriched["build_job_url"] = None
        enriched["build_log_url"] = None
    if enriched.get("publish_job_id"):
        enriched["publish_job_url"] = f"/jobs/{enriched['publish_job_id']}"
    else:
        enriched["publish_job_url"] = None
    return enriched


def _configured_proxmox_defaults() -> dict:
    from web import app as web_app

    try:
        cfg = web_app._load_vars()
    except Exception:
        cfg = {}
    return {
        "node": cfg.get("proxmox_node") or "pve",
        "iso_storage": cfg.get("proxmox_iso_storage") or "local",
        "disk_storage": cfg.get("proxmox_storage") or "local-lvm",
        "bridge": cfg.get("proxmox_bridge") or "vmbr0",
    }


def catalog_payload() -> dict:
    return {
        "schema_version": 1,
        "defaults": {
            "architecture": cloudosd_pg.DEFAULT_ARCHITECTURE,
            "osdcloud_module_version": cloudosd_pg.DEFAULT_OSDCLOUD_MODULE_VERSION,
            "os_version": cloudosd_pg.DEFAULT_OS_VERSION,
            "os_activation": cloudosd_pg.DEFAULT_OS_ACTIVATION,
            "os_edition": cloudosd_pg.DEFAULT_OS_EDITION,
            "os_language": cloudosd_pg.DEFAULT_OS_LANGUAGE,
            "driver_pack_policy": cloudosd_pg.DEFAULT_DRIVER_PACK_POLICY,
            "firmware_updates_enabled": False,
            "analytics_enabled": False,
            "vm_cores": cloudosd_pg.DEFAULT_VM_CORES,
            "vm_memory_mb": cloudosd_pg.DEFAULT_VM_MEMORY_MB,
            "vm_disk_size_gb": cloudosd_pg.DEFAULT_VM_DISK_SIZE_GB,
            "minimum_vm_memory_mb": _MIN_CLOUDOSD_MEMORY_MB,
            "recommended_vm_memory_mb": _RECOMMENDED_CLOUDOSD_MEMORY_MB,
            "minimum_vm_disk_size_gb": _MIN_CLOUDOSD_DISK_GB,
        },
        "architectures": [cloudosd_pg.DEFAULT_ARCHITECTURE],
        "os_versions": cloudosd_pg.OS_VERSION_CATALOG,
        "os_activations": cloudosd_pg.OS_ACTIVATION_CATALOG,
        "os_editions": cloudosd_pg.OS_EDITION_CATALOG,
        "os_languages": cloudosd_pg.OS_LANGUAGE_CATALOG,
        "driver_pack_policies": ["None", "OSDCloud"],
    }


def proxmox_options_payload() -> dict:
    from web import app as web_app

    defaults = _configured_proxmox_defaults()
    configured = {
        "schema_version": 1,
        "source": "configured",
        "defaults": defaults,
        "catalog": catalog_payload(),
        "nodes": [defaults["node"]],
        "storages": {
            "iso": [defaults["iso_storage"]],
            "disk": [defaults["disk_storage"]],
        },
        "bridges": [defaults["bridge"]],
        "vms": [],
    }
    try:
        nodes_raw = web_app._proxmox_api("/nodes") or []
        nodes = sorted({row["node"] for row in nodes_raw if row.get("node")})
        node = defaults["node"] if defaults["node"] in nodes else (nodes[0] if nodes else defaults["node"])
        storages = web_app._proxmox_api("/storage") or []
        iso_storages = sorted({
            row["storage"]
            for row in storages
            if row.get("storage") and "iso" in (row.get("content") or "")
        })
        disk_storages = sorted({
            row["storage"]
            for row in storages
            if row.get("storage") and "images" in (row.get("content") or "")
        })
        networks = web_app._proxmox_api(f"/nodes/{node}/network") or []
        bridges = sorted({
            row["iface"]
            for row in networks
            if row.get("iface") and row.get("type") in ("bridge", "OVSBridge")
        })
        try:
            vms = web_app._proxmox_api(f"/nodes/{node}/qemu") or []
        except Exception:
            vms = []
        return {
            "schema_version": 1,
            "source": "live",
            "defaults": defaults,
            "catalog": catalog_payload(),
            "nodes": nodes or configured["nodes"],
            "storages": {
                "iso": iso_storages or configured["storages"]["iso"],
                "disk": disk_storages or configured["storages"]["disk"],
            },
            "bridges": bridges or configured["bridges"],
            "vms": [
                {
                    "vmid": row.get("vmid"),
                    "name": row.get("name") or "",
                    "template": bool(row.get("template")),
                    "status": row.get("status"),
                }
                for row in vms
            ],
        }
    except Exception as exc:
        configured["error"] = str(exc)
        return configured


def _check(check_id: str, label: str, detail: str) -> dict:
    return {"id": check_id, "label": label, "detail": detail}


def _resolved_target(body: RunCreateBody, options: dict) -> dict:
    defaults = options["defaults"]
    return {
        "node": body.node or defaults["node"],
        "iso_storage": body.iso_storage or defaults["iso_storage"],
        "storage": body.storage or defaults["disk_storage"],
        "network_bridge": body.network_bridge or defaults["bridge"],
    }


def preflight_payload(body: RunCreateBody) -> dict:
    blocking: list[dict] = []
    warnings: list[dict] = []
    artifact = None
    with _conn() as conn:
        artifact = cloudosd_pg.get_artifact(conn, body.artifact_id)
    artifact = enrich_artifact(artifact)
    asset_status = assets_status_payload()
    options = proxmox_options_payload()
    target = _resolved_target(body, options)
    normalized_name = cloudosd_pg.normalize_windows_computer_name(body.vm_name)

    if body.architecture != cloudosd_pg.DEFAULT_ARCHITECTURE:
        blocking.append(_check(
            "architecture_unsupported",
            "Architecture unsupported",
            "CloudOSD v1 only supports amd64 artifacts.",
        ))
    if body.vm_memory_mb < _MIN_CLOUDOSD_MEMORY_MB:
        blocking.append(_check(
            "memory_minimum",
            "Memory below minimum",
            f"CloudOSD Proxmox VMs need at least {_MIN_CLOUDOSD_MEMORY_MB} MB RAM.",
        ))
    elif body.vm_memory_mb < _RECOMMENDED_CLOUDOSD_MEMORY_MB:
        warnings.append(_check(
            "memory_recommended",
            "Memory below default",
            f"{_RECOMMENDED_CLOUDOSD_MEMORY_MB} MB RAM is the first-lab default.",
        ))
    if body.vm_disk_size_gb < _MIN_CLOUDOSD_DISK_GB:
        blocking.append(_check(
            "disk_minimum",
            "Disk below minimum",
            f"CloudOSD VMs need at least {_MIN_CLOUDOSD_DISK_GB} GB disk.",
        ))
    if not normalized_name:
        blocking.append(_check(
            "computer_name_invalid",
            "Invalid Windows computer name",
            "The requested VM name does not leave any valid Windows computer-name characters.",
        ))
    elif normalized_name != body.vm_name.strip():
        warnings.append(_check(
            "computer_name_normalized",
            "Windows computer name will be normalized",
            f"Windows will receive {normalized_name}.",
        ))

    unsupported_os_choice = False
    os_catalog_checks = [
        (
            body.os_version,
            cloudosd_pg.OS_VERSION_CATALOG,
            "os_version_unsupported",
            "Windows version unsupported",
            "Select a Windows version from the pinned OSDCloud deployable catalog.",
        ),
        (
            body.os_activation,
            cloudosd_pg.OS_ACTIVATION_CATALOG,
            "os_activation_unsupported",
            "Windows activation unsupported",
            "Select Retail or Volume from the pinned OSDCloud deployable catalog.",
        ),
        (
            body.os_edition,
            cloudosd_pg.OS_EDITION_CATALOG,
            "os_edition_unsupported",
            "Windows edition unsupported",
            "Select an edition from the pinned OSDCloud deployable catalog.",
        ),
        (
            body.os_language,
            cloudosd_pg.OS_LANGUAGE_CATALOG,
            "os_language_unsupported",
            "Windows language unsupported",
            "Select a language from the pinned OSDCloud deployable catalog.",
        ),
    ]
    for value, allowed, check_id, label, detail in os_catalog_checks:
        if value not in allowed:
            unsupported_os_choice = True
            blocking.append(_check(check_id, label, detail))

    if not artifact:
        blocking.append(_check(
            "artifact_missing",
            "Artifact missing",
            "The selected CloudOSD artifact does not exist.",
        ))
    elif artifact["readiness"] == "not_uploaded":
        blocking.append(_check(
            "artifact_not_uploaded",
            "Artifact not uploaded",
            "The selected artifact does not have a Proxmox volid.",
        ))
    elif artifact["readiness"] == "missing_hash":
        blocking.append(_check(
            "artifact_missing_hash",
            "Artifact hash missing",
            "The selected artifact must have ISO and WIM SHA-256 hashes.",
        ))

    for key, asset in asset_status["assets"].items():
        if asset.get("required") and not asset.get("available"):
            blocking.append(_check(
                f"asset_{key}_missing",
                f"{asset['name']} missing",
                asset.get("error") or asset.get("path") or "Required CloudOSD asset is missing.",
            ))

    if target["node"] not in options["nodes"]:
        blocking.append(_check(
            "proxmox_node_unavailable",
            "Proxmox node unavailable",
            f"{target['node']} is not in the discovered node list.",
        ))
    if target["iso_storage"] not in options["storages"]["iso"]:
        blocking.append(_check(
            "proxmox_iso_storage_unavailable",
            "ISO storage unavailable",
            f"{target['iso_storage']} is not an ISO-capable storage.",
        ))
    if target["storage"] not in options["storages"]["disk"]:
        blocking.append(_check(
            "proxmox_disk_storage_unavailable",
            "Disk storage unavailable",
            f"{target['storage']} is not an image-capable storage.",
        ))
    if target["network_bridge"] not in options["bridges"]:
        blocking.append(_check(
            "proxmox_bridge_unavailable",
            "Network bridge unavailable",
            f"{target['network_bridge']} is not available on the selected node.",
        ))

    requested_vmid = body.vmid
    requested_name_key = body.vm_name.strip().lower()
    normalized_key = normalized_name.lower()
    for vm in options.get("vms") or []:
        if requested_vmid and int(vm.get("vmid") or 0) == requested_vmid:
            blocking.append(_check(
                "vmid_collision",
                "VMID collision",
                f"VMID {requested_vmid} already exists.",
            ))
        vm_name_key = (vm.get("name") or "").strip().lower()
        if vm_name_key and vm_name_key in {requested_name_key, normalized_key}:
            blocking.append(_check(
                "vm_name_collision",
                "VM name collision",
                f"VM name {vm.get('name')} already exists.",
            ))

    if body.firmware_updates_enabled:
        warnings.append(_check(
            "firmware_updates",
            "Firmware updates enabled",
            "Firmware changes are intentionally off for the first CloudOSD lab path.",
        ))
    if body.driver_pack_policy != cloudosd_pg.DEFAULT_DRIVER_PACK_POLICY:
        warnings.append(_check(
            "driver_pack_policy",
            "Driver pack policy changed",
            "Driver pack policy None is the default first-lab posture.",
        ))
    if body.analytics_enabled:
        warnings.append(_check(
            "analytics_allowed",
            "Analytics allowed",
            "CloudOSD analytics are blocked by default.",
        ))
    if not unsupported_os_choice and (
        body.os_version != cloudosd_pg.DEFAULT_OS_VERSION
        or body.os_activation != cloudosd_pg.DEFAULT_OS_ACTIVATION
        or body.os_edition != cloudosd_pg.DEFAULT_OS_EDITION
        or body.os_language != cloudosd_pg.DEFAULT_OS_LANGUAGE
    ):
        warnings.append(_check(
            "os_outside_default",
            "OS outside first-lab set",
            "The default is Windows 11 25H2 Enterprise Volume en-us.",
        ))

    return {
        "schema_version": 1,
        "ok": not blocking,
        "launch_allowed": not blocking,
        "blocking_checks": blocking,
        "warnings": warnings,
        "normalized_computer_name": normalized_name,
        "artifact": artifact,
        "asset_status": asset_status,
        "proxmox": options,
        "target": target,
        "minimum_vm_memory_mb": _MIN_CLOUDOSD_MEMORY_MB,
        "recommended_vm_memory_mb": _RECOMMENDED_CLOUDOSD_MEMORY_MB,
        "minimum_vm_disk_size_gb": _MIN_CLOUDOSD_DISK_GB,
    }


@router.get("/catalog")
def catalog():
    return catalog_payload()


@router.get("/artifacts")
def list_artifacts(architecture: Optional[str] = None):
    with _conn() as conn:
        return {
            "schema_version": 1,
            "artifacts": [
                enrich_artifact(artifact)
                for artifact in cloudosd_pg.list_artifacts(
                    conn,
                    architecture=architecture,
                )
            ],
        }


@router.post("/artifacts/build", status_code=202)
def build_artifact(body: ArtifactBuildBody):
    from web import app as web_app
    from web import jobs_pg

    job_id = web_app.job_manager._generate_id()
    log_path = Path(web_app.job_manager.jobs_dir) / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    script = _APP_ROOT / "scripts" / "cloudosd_remote_build.py"
    output_dir = _APP_ROOT / "output" / "cloudosd"
    cmd = [
        sys.executable,
        str(script),
        "--job-id",
        job_id,
        "--remote",
        body.remote,
        "--remote-root",
        body.remote_root,
        "--repo-root",
        str(_CLOUDOSD_SOURCE_ROOT),
        "--output-dir",
        str(output_dir),
        "--arch",
        body.architecture,
        "--osdcloud-version",
        body.osdcloud_module_version,
    ]
    jobs_pg.enqueue(
        job_id=job_id,
        job_type="cloudosd_build_iso",
        playbook="cloudosd_remote_build",
        cmd=cmd,
        args=body.model_dump(),
    )
    return {"ok": True, "job_id": job_id}


@router.post("/artifacts/{artifact_id}/publish")
def publish_artifact(artifact_id: str, body: ArtifactPublishBody):
    import requests
    from web import app as web_app

    options = proxmox_options_payload()
    defaults = options["defaults"]
    node = body.node or defaults["node"]
    storage = body.storage or defaults["iso_storage"]
    if node not in options["nodes"]:
        raise HTTPException(status_code=400, detail=f"Proxmox node is unavailable: {node}")
    if storage not in options["storages"]["iso"]:
        raise HTTPException(status_code=400, detail=f"ISO storage is unavailable: {storage}")
    with _conn() as conn:
        artifact = cloudosd_pg.get_artifact(conn, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="CloudOSD artifact not found")
    iso_path = Path(artifact["iso_path"])
    if not iso_path.is_file():
        raise HTTPException(status_code=409, detail=f"CloudOSD ISO is not present: {iso_path}")

    cfg = web_app._load_proxmox_config()
    host = cfg.get("proxmox_host", "")
    port = cfg.get("proxmox_port", 8006)
    token_id = cfg.get("vault_proxmox_api_token_id", "")
    token_secret = cfg.get("vault_proxmox_api_token_secret", "")
    url = f"https://{host}:{port}/api2/json/nodes/{node}/storage/{storage}/upload"
    headers = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
    try:
        with iso_path.open("rb") as handle:
            response = requests.post(
                url,
                headers=headers,
                data={"content": "iso"},
                files={"filename": (iso_path.name, handle, "application/octet-stream")},
                verify=cfg.get("proxmox_validate_certs", False),
                timeout=300,
            )
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CloudOSD artifact publish failed: {exc}")
    volid = f"{storage}:iso/{iso_path.name}"
    with _conn() as conn:
        artifact = cloudosd_pg.update_artifact_proxmox_volid(
            conn,
            artifact_id=artifact_id,
            proxmox_volid=volid,
        )
    return {"ok": True, "artifact": enrich_artifact(artifact)}


@router.get("/proxmox/options")
def proxmox_options():
    return proxmox_options_payload()


@router.get("/assets/status")
def assets_status():
    return assets_status_payload()


@router.post("/preflight")
def preflight(body: RunCreateBody):
    return preflight_payload(body)


@router.post("/runs", status_code=201)
def create_run(body: RunCreateBody):
    if body.vm_memory_mb < _MIN_CLOUDOSD_MEMORY_MB:
        raise HTTPException(
            status_code=400,
            detail=f"CloudOSD Proxmox VMs need at least {_MIN_CLOUDOSD_MEMORY_MB} MB RAM",
        )
    preflight = preflight_payload(body)
    if preflight["blocking_checks"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "CloudOSD blocking preflight checks failed",
                "blocking_checks": preflight["blocking_checks"],
            },
        )
    target = preflight["target"]
    with _conn() as conn:
        artifact = cloudosd_pg.get_artifact(conn, body.artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="CloudOSD artifact not found")
        if not artifact.get("proxmox_volid"):
            raise HTTPException(
                status_code=409,
                detail="CloudOSD artifact is not uploaded to Proxmox ISO storage",
            )
        try:
            run = cloudosd_pg.create_run(
                conn,
                artifact_id=body.artifact_id,
                vm_name=body.vm_name,
                node=target["node"],
                iso_storage=target["iso_storage"],
                storage=target["storage"],
                network_bridge=target["network_bridge"],
                requested_vmid=body.vmid,
                architecture=body.architecture,
                os_version=body.os_version,
                os_activation=body.os_activation,
                os_edition=body.os_edition,
                os_language=body.os_language,
                vm_cores=body.vm_cores,
                vm_memory_mb=body.vm_memory_mb,
                vm_disk_size_gb=body.vm_disk_size_gb,
                vm_group_tag=body.vm_group_tag.strip(),
                vm_oem_profile=body.vm_oem_profile.strip(),
                chassis_type_override=body.chassis_type_override,
                source_surface=body.source_surface.strip() or "cloudosd",
                source_sequence_id=body.source_sequence_id,
                tpm_enabled=body.tpm_enabled,
                secure_boot=body.secure_boot,
                firmware_updates_enabled=body.firmware_updates_enabled,
                driver_pack_policy=body.driver_pack_policy,
                analytics_enabled=body.analytics_enabled,
                outbound_policy=body.outbound_policy,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return run


@router.get("/runs")
def list_runs(limit: int = 100):
    with _conn() as conn:
        return {
            "schema_version": 1,
            "runs": cloudosd_pg.list_runs(conn, limit=limit),
        }


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
        if heartbeat and run["state"] != "complete":
            run = cloudosd_pg.mark_complete_from_heartbeat(
                conn,
                run_id=run_id,
                heartbeat_at=heartbeat["received_at"],
                heartbeat=heartbeat,
            )
        artifact = cloudosd_pg.get_artifact(conn, run["artifact_id"])
        cloudosd_pg.sync_ts_progress_for_run(conn, run_id)
        raw_v2_steps = cloudosd_pg.ts_engine_pg.list_run_steps(conn, run_id)
        v2_steps = cloudosd_pg.enrich_v2_steps_for_operator(raw_v2_steps)
        v2_completion = cloudosd_pg.v2_completion_status(
            conn,
            run_id,
            domain_join=run.get("domain_join"),
        )
        v2_operator_status = cloudosd_pg.v2_operator_status(
            raw_v2_steps,
            v2_completion,
            heartbeat=heartbeat,
        )
        events = cloudosd_pg.list_events(conn, run_id)
        evidence_events = events_with_related_jobs(run_id, events, run)
    heartbeat_name = heartbeat.get("computer_name") if heartbeat else None
    name_comparison = cloudosd_pg.name_comparison(
        requested_name=run.get("requested_vm_name") or run.get("vm_name"),
        pve_name=run.get("pve_vm_name"),
        heartbeat_name=heartbeat_name,
    )
    run["heartbeat_computer_name"] = heartbeat_name
    run["name_comparison"] = name_comparison
    return {
        "schema_version": 1,
        "run": run,
        "artifact": enrich_artifact(artifact),
        "latest_heartbeat": heartbeat,
        "events": evidence_events,
        "event_groups": cloudosd_pg.milestone_event_groups(evidence_events),
        "milestone_labels": cloudosd_pg.CLOUDOSD_MILESTONE_LABELS,
        "v2_steps": v2_steps,
        "v2_completion": v2_completion,
        "v2_operator_status": v2_operator_status,
        "intune_evidence": intune_evidence_for_run(run, heartbeat),
        "related_jobs": _related_jobs(run_id),
        "os_settings": cloudosd_pg.os_settings(run),
        "user_settings": cloudosd_pg.user_settings(run),
        "task": cloudosd_pg.task_settings(run),
    }


@router.post("/runs/{run_id}/v2/steps/{step_id}/retry")
def retry_v2_step(run_id: str, step_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        try:
            step = cloudosd_pg.requeue_agent_v2_step(
                conn,
                run_id=run_id,
                step_id=step_id,
                requested_by="operator",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        cloudosd_pg.sync_ts_progress_for_run(conn, run_id)
        raw_v2_steps = cloudosd_pg.ts_engine_pg.list_run_steps(conn, run_id)
        v2_completion = cloudosd_pg.v2_completion_status(
            conn,
            run_id,
            domain_join=run.get("domain_join"),
        )
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
    return {
        "ok": True,
        "step": cloudosd_pg.enrich_v2_steps_for_operator([step])[0],
        "v2_steps": cloudosd_pg.enrich_v2_steps_for_operator(raw_v2_steps),
        "v2_completion": v2_completion,
        "v2_operator_status": cloudosd_pg.v2_operator_status(
            raw_v2_steps,
            v2_completion,
            heartbeat=heartbeat,
        ),
    }


@router.get("/runs/{run_id}/events")
def list_run_events(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        cloudosd_pg.sync_ts_progress_for_run(conn, run_id)
        raw_v2_steps = cloudosd_pg.ts_engine_pg.list_run_steps(conn, run_id)
        v2_steps = cloudosd_pg.enrich_v2_steps_for_operator(raw_v2_steps)
        v2_completion = cloudosd_pg.v2_completion_status(
            conn,
            run_id,
            domain_join=run.get("domain_join"),
        )
        events = cloudosd_pg.list_events(conn, run_id)
    events = events_with_related_jobs(run_id, events, run)
    groups: dict[str, list[dict]] = {}
    for event in events:
        groups.setdefault(event["phase"], []).append(event)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "events": events,
        "groups": groups,
        "milestone_groups": cloudosd_pg.milestone_event_groups(events),
        "milestone_labels": cloudosd_pg.CLOUDOSD_MILESTONE_LABELS,
        "v2_steps": v2_steps,
        "v2_completion": v2_completion,
    }


def _profile_chassis_type(profile_key: str | None) -> int:
    if not profile_key:
        return 0
    try:
        from web import app as web_app

        profile = web_app.load_oem_profiles().get(profile_key.strip()) or {}
        return int(profile.get("chassis_type") or 0)
    except Exception:
        return 0


def _run_needs_root_ticket_for_chassis(run: dict) -> bool:
    if int(run.get("chassis_type_override") or 0) > 0:
        return True
    return _profile_chassis_type(run.get("vm_oem_profile")) > 0


def cloudosd_provision_extra_vars(
    *,
    run: dict,
    artifact: dict,
    request: Request | None = None,
    root_ticket: str | None = None,
    root_csrf_token: str | None = None,
    require_root_ticket: bool | None = None,
) -> dict:
    """Build playbook vars for a CloudOSD provision job.

    This is shared by the single-run CloudOSD cockpit and the batch
    `/provision` launcher so both paths carry the same identity, metadata,
    and Proxmox root-ticket behavior.
    """
    from web import app as web_app

    run_id = run["run_id"]
    requested_name = run.get("requested_vm_name") or run["vm_name"]
    expected_name = run.get("expected_computer_name") or requested_name
    extra_vars = {
        "cloudosd_run_id": run_id,
        "cloudosd_artifact_volid": artifact["proxmox_volid"],
        "autopilot_base_url": _base_url(request),
        "proxmox_node": run["node"],
        "proxmox_node_ssh_host": web_app._proxmox_node_ssh_host(run["node"]),
        "proxmox_storage": run["storage"],
        "proxmox_bridge": run["network_bridge"],
        "vm_cores": run["vm_cores"],
        "vm_memory_mb": run["vm_memory_mb"],
        "vm_disk_size_gb": run["vm_disk_size_gb"],
        "vm_name": requested_name,
        "vm_custom_serial": expected_name,
        "hostname_pattern": expected_name,
        "tpm_enabled": run["tpm_enabled"],
        "secure_boot": run["secure_boot"],
    }
    if run.get("vm_group_tag"):
        extra_vars["vm_group_tag"] = run["vm_group_tag"]
    if run.get("vm_oem_profile"):
        extra_vars["vm_oem_profile"] = run["vm_oem_profile"]
    if int(run.get("chassis_type_override") or 0) > 0:
        extra_vars["chassis_type_override"] = int(run["chassis_type_override"])
    if run.get("requested_vmid"):
        extra_vars["requested_vmid"] = run["requested_vmid"]
    if run.get("source_sequence_id"):
        extra_vars["source_sequence_id"] = run["source_sequence_id"]

    needs_root_ticket = (
        _run_needs_root_ticket_for_chassis(run)
        if require_root_ticket is None
        else require_root_ticket
    )
    if needs_root_ticket:
        cfg = web_app._load_proxmox_config()
        root_password = cfg.get("vault_proxmox_root_password", "")
        if not root_password and not (root_ticket and root_csrf_token):
            raise HTTPException(
                status_code=400,
                detail=(
                    "CloudOSD OEM/chassis provisioning needs Proxmox root SSH "
                    "for host-local SMBIOS staging and QEMU args. Run Settings "
                    "-> Proxmox Permission Bootstrap to apply the hypervisor "
                    "permissions and store the validated root SSH credential."
                ),
            )
        if root_ticket and root_csrf_token:
            extra_vars["_proxmox_root_ticket"] = root_ticket
            extra_vars["_proxmox_root_csrf_token"] = root_csrf_token

    return extra_vars


@router.post("/runs/{run_id}/provision", status_code=202)
def provision_run(run_id: str, request: Request):
    from web import app as web_app

    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        artifact = cloudosd_pg.get_artifact(conn, run["artifact_id"])
        if not artifact:
            raise HTTPException(status_code=404, detail="CloudOSD artifact not found")
        if not artifact.get("proxmox_volid"):
            raise HTTPException(
                status_code=409,
                detail="CloudOSD artifact is not uploaded to Proxmox ISO storage",
            )
    extra_vars = cloudosd_provision_extra_vars(
        run=run,
        artifact=artifact,
        request=request,
    )
    cmd = [
        "ansible-playbook",
        str(_APP_ROOT / "playbooks" / "provision_proxmox_cloudosd.yml"),
    ]
    for key, value in extra_vars.items():
        cmd.extend(["-e", f"{key}={value}"])
    job = web_app.job_manager.start(
        "provision_cloudosd",
        cmd,
        args=extra_vars,
    )
    return {"ok": True, "job_id": job["id"]}


@router.post("/runs/{run_id}/identity")
def set_run_identity(run_id: str, body: RunIdentityBody):
    with _conn() as conn:
        run = cloudosd_pg.set_run_identity(
            conn,
            run_id=run_id,
            vmid=body.vmid,
            vm_uuid=body.vm_uuid,
            mac=body.mac,
            node=body.node,
            computer_name=body.computer_name,
        )
    if not run:
        raise HTTPException(status_code=404, detail="CloudOSD run not found")
    return run


@router.post("/pe/register")
def pe_register(body: PeRegisterBody):
    with _conn() as conn:
        run = cloudosd_pg.find_run_by_identity(
            conn,
            vm_uuid=body.vm_uuid,
            mac=body.mac,
            architecture=body.architecture,
            build_sha=body.build_sha,
        )
        if not run:
            logger.warning(
                "CloudOSD PE registration identity mismatch: vm_uuid=%s mac=%s architecture=%s build_sha=%s manufacturer=%s model=%s",
                body.vm_uuid,
                body.mac,
                body.architecture,
                body.build_sha,
                body.manufacturer,
                body.model,
            )
            raise HTTPException(status_code=404, detail="matching CloudOSD run not found")
        run = cloudosd_pg.mark_pe_registered(conn, run_id=run["run_id"])
    return {
        "schema_version": 1,
        "run_id": run["run_id"],
        "workflow_name": run["workflow_name"],
        "bearer_token": _sign(run["run_id"], ttl_seconds=_PE_TOKEN_TTL_SECONDS),
        "package_url": f"/api/cloudosd/pe/package/{run['run_id']}",
        "state": run["state"],
    }


@router.get("/pe/package/{run_id}")
def pe_package(
    run_id: str,
    request: Request,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(run_id, payload)
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        artifact = cloudosd_pg.get_artifact(conn, run["artifact_id"])
        if not artifact:
            raise HTTPException(status_code=404, detail="CloudOSD artifact not found")
    return _package_response(
        run=run,
        artifact=artifact,
        server_base_url=_base_url(request),
        domain_join_secret=_domain_join_secret_for_run(run),
    )


@router.get("/assets/{asset_name}")
def get_asset(asset_name: str):
    allowed = {
        "PVEAutopilot-FirstBoot.ps1",
        "Invoke-CloudOSDBridge.ps1",
        "autopilotagent-postinstall.ps1",
        "autopilotagent.msi",
    }
    if asset_name not in allowed:
        raise HTTPException(status_code=404, detail="CloudOSD asset not found")
    path = _asset_path(asset_name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"CloudOSD asset is missing: {asset_name}")
    media_type = "application/octet-stream"
    if asset_name.endswith(".ps1"):
        media_type = "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, filename=asset_name)


@router.post("/runs/{run_id}/events")
def append_event(
    run_id: str,
    body: EventBody,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(run_id, payload)
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        event = cloudosd_pg.append_event(
            conn,
            run_id=run_id,
            phase=body.phase,
            event_type=body.event_type,
            severity=body.severity,
            message=body.message,
            data=body.data,
        )
        if body.event_type == "osdcloud_start":
            cloudosd_pg.mark_osdcloud_started(conn, run_id=run_id)
        elif body.event_type == "cloudosd_pe_complete":
            cloudosd_pg.mark_osdcloud_finished(conn, run_id=run_id)
        if body.severity.lower() == "error" or body.event_type.endswith("_failed"):
            cloudosd_pg.mark_failed(
                conn,
                run_id=run_id,
                message=body.message or body.event_type,
            )
        cloudosd_pg.sync_ts_progress_for_run(conn, run_id)
    return {"schema_version": 1, "event": event}
