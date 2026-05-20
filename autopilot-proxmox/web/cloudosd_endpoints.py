"""CloudOSD controller and WinPE bridge API."""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from psycopg import errors as pg_errors
from pydantic import BaseModel, Field

from web import agent_telemetry_pg, cloudosd_cache, cloudosd_pg, osd_package, winpe_token
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


class GraphThrottled(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int = 600):
        super().__init__(message)
        self.retry_after_seconds = max(60, int(retry_after_seconds or 600))


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
    bubble_id: Optional[str] = None
    asset_role: Optional[str] = None


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
        cloudosd_cache.init(conn)
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
        "local_admin": run.get("local_admin") or {
            "username": cloudosd_pg.DEFAULT_LOCAL_ADMIN_USERNAME,
            "password": "",
        },
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
    try:
        with _conn() as conn:
            response["cache"] = cloudosd_cache.package_cache_payload(
                conn,
                run=run,
                artifact=artifact,
                server_base_url=server_base_url,
                token=pe_token,
            )
    except Exception as exc:
        response["cache"] = {
            "policy": "direct_on_miss",
            "feature_image": None,
            "quality_updates": [],
            "error": str(exc),
        }
    run_domain_join = run.get("domain_join") or {}
    if not run_domain_join.get("domain_controller_ipv4"):
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


def _same_text(left: str | None, right: str | None) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _device_group_matches(group: dict, candidate_keys: set[str]) -> bool:
    autopilot = group.get("autopilot") or {}
    intune = group.get("intune") or {}
    values = [
        group.get("serial"),
        autopilot.get("serial"),
        autopilot.get("display_name"),
        intune.get("serial"),
        intune.get("device_name"),
    ]
    return any(str(value or "").strip().casefold() in candidate_keys for value in values)


def _assignment_status(
    *,
    autopilot: dict | None,
    expected_group_tag: str,
    matched_hashes: list[dict],
) -> str:
    if not autopilot:
        return "waiting_for_upload" if matched_hashes else "not_found"
    actual_group_tag = str(autopilot.get("group_tag") or "").strip()
    if expected_group_tag and actual_group_tag and not _same_text(expected_group_tag, actual_group_tag):
        return "group_tag_mismatch"
    return str(autopilot.get("profile_status") or "unknown")


def _enrollment_contact_state(
    *,
    autopilot: dict | None,
    intune: dict | None,
) -> str:
    if intune:
        return "enrolled"
    enrollment_state = str((autopilot or {}).get("enrollment_state") or "").strip()
    if not enrollment_state:
        return "not_found"
    if enrollment_state.casefold() == "notcontacted":
        return "not_contacted"
    return enrollment_state


def intune_evidence_for_run(run: dict, heartbeat: dict | None = None) -> dict:
    candidates = _identity_candidates(run, heartbeat)
    candidate_keys = {item.casefold() for item in candidates}
    expected_group_tag = str(run.get("vm_group_tag") or "").strip()
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
                matched_hashes.append(dict(item))
        matched_hashes.sort(
            key=lambda item: (
                int(item.get("modified_epoch") or 0),
                str(item.get("name") or ""),
            ),
            reverse=True,
        )
    except Exception as exc:
        cache_error = f"hash evidence unavailable: {exc}"

    selected_hash_serial = ""
    if matched_hashes:
        selected_hash_serial = str(matched_hashes[0].get("serial") or "").strip()
        if selected_hash_serial and selected_hash_serial.casefold() not in candidate_keys:
            candidates.append(selected_hash_serial)

    device_candidate_keys = (
        {selected_hash_serial.casefold()}
        if selected_hash_serial
        else candidate_keys
    )
    group = None
    synced_at = ""
    try:
        from web import devices_pg

        groups, extra = devices_pg.list_grouped(windows_only=False)
        synced_at = (extra.get("meta") or {}).get("synced_at") or ""
        for item in groups:
            if _device_group_matches(item, device_candidate_keys):
                group = item
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
    assignment_status = _assignment_status(
        autopilot=autopilot,
        expected_group_tag=expected_group_tag,
        matched_hashes=matched_hashes,
    )
    actual_group_tag = str((autopilot or {}).get("group_tag") or "").strip()
    group_tag_match = None
    if expected_group_tag:
        group_tag_match = _same_text(expected_group_tag, actual_group_tag)
    enrollment_status = (
        "enrolled"
        if intune
        else (
            autopilot.get("enrollment_state")
            if autopilot and autopilot.get("enrollment_state")
            else "not_found"
        )
    )
    contact_state = _enrollment_contact_state(autopilot=autopilot, intune=intune)
    errors: list[dict] = []
    if cache_error:
        errors.append({
            "source": "cache",
            "code": "cache_error",
            "message": cache_error,
        })
    if assignment_status == "group_tag_mismatch":
        errors.append({
            "source": "assignment",
            "code": "group_tag_mismatch",
            "message": (
                f"Autopilot group tag {actual_group_tag} does not match "
                f"expected {expected_group_tag}"
            ),
        })
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
        "errors": errors,
        "tracking": {
            "expected_group_tag": expected_group_tag,
            "source_surface": run.get("source_surface") or "cloudosd",
            "source_sequence_id": run.get("source_sequence_id"),
        },
        "hash": {
            "status": "captured" if matched_hashes else "missing",
            "files": matched_hashes,
        },
        "upload": {
            "status": upload_status,
            "autopilot_device_id": autopilot.get("id") if autopilot else None,
            "serial": autopilot.get("serial") if autopilot else None,
            "display_name": autopilot.get("display_name") if autopilot else None,
            "error": None,
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
            "expected_group_tag": expected_group_tag,
            "group_tag": autopilot.get("group_tag") if autopilot else None,
            "actual_group_tag": autopilot.get("group_tag") if autopilot else None,
            "group_tag_match": group_tag_match,
            "profile_status": autopilot.get("profile_status") if autopilot else None,
        },
        "enrollment": {
            "status": enrollment_status,
            "contact_state": contact_state,
            "autopilot_enrollment_state": autopilot.get("enrollment_state") if autopilot else None,
            "autopilot_last_contact": autopilot.get("last_contact") if autopilot else None,
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


def _parse_timestamp(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cache_status(synced_at: str | None) -> str:
    parsed = _parse_timestamp(synced_at)
    if not parsed:
        return "missing"
    age_seconds = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    if age_seconds <= 5 * 60:
        return "fresh"
    if age_seconds <= 15 * 60:
        return "aging"
    if age_seconds <= 60 * 60:
        return "stale"
    return "expired"


def _hash_file_sha256(filename: str | None) -> str | None:
    if not filename:
        return None
    try:
        from web import app as web_app

        candidate = web_app.HASH_DIR / Path(filename).name
        if candidate.is_file():
            return sha256(candidate.read_bytes()).hexdigest()
    except Exception:
        return None
    return None


def _assigned_status(value: str | None) -> bool:
    return str(value or "").strip() in {
        "assigned",
        "assignedInSync",
        "assignedUnkownSyncState",
        "Assigned",
        "Assigned (Synced)",
    }


def _job_log_tail(job_id: str | None) -> str:
    if not job_id:
        return ""
    try:
        from web import app as web_app

        return (web_app.job_manager.get_log(job_id) or "")[-4000:]
    except Exception:
        return ""


def _readiness_error(source: str, code: str, message: str) -> dict:
    return {"source": source, "code": code, "message": message}


def _latest_auto_sync_event(conn, run_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM cloudosd_run_events
        WHERE run_id = %s
          AND event_type IN (
            'autopilot_cache_auto_sync_attempted',
            'autopilot_cache_auto_sync_complete',
            'autopilot_cache_auto_sync_failed'
          )
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def _event_age_seconds(event: dict | None) -> float | None:
    if not event or not event.get("created_at"):
        return None
    created = event["created_at"]
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()


def _readiness_with_sync_error(readiness: dict, message: str) -> dict:
    updated = dict(readiness)
    errors = list(updated.get("errors") or [])
    errors.append(_readiness_error("sync", "sync_failed", message))
    updated["state"] = "sync_failed"
    updated["label"] = "sync failed"
    updated["detail"] = message
    updated["next_action"] = "sync_intune"
    updated["errors"] = errors
    return updated


def _readiness_with_graph_throttle(conn, readiness: dict) -> dict:
    backoff = cloudosd_pg.graph_sync_backoff_active(conn)
    if not backoff:
        return readiness
    state = readiness.get("state") or ""
    if state not in {"upload_submitted", "imported", "assigned", "contacted", "sync_failed", "sync_throttled"}:
        return readiness
    retry_at = backoff.get("retry_after_until") or ""
    message = "Graph throttled, retrying later."
    if retry_at:
        message = f"{message} Retry after {retry_at}."
    updated = dict(readiness)
    errors = [
        error for error in list(updated.get("errors") or [])
        if error.get("code") != "graph_throttled"
    ]
    errors.append(_readiness_error("sync", "graph_throttled", message))
    updated["state"] = "sync_throttled"
    updated["label"] = "Graph throttled"
    updated["detail"] = message
    updated["next_action"] = "wait_for_graph_backoff"
    updated["errors"] = errors
    return updated


def _should_auto_sync_after_upload(readiness: dict) -> bool:
    return (
        readiness.get("state") == "upload_submitted"
        and readiness.get("next_action") == "sync_intune"
        and (readiness.get("cache") or {}).get("status") in {"missing", "stale", "expired"}
        and not (readiness.get("autopilot") or {}).get("device_id")
    )


def autopilot_readiness_needs_graph_sync(readiness: dict) -> bool:
    state = str(readiness.get("state") or "")
    if state in {"upload_submitted", "imported", "assigned", "contacted", "sync_failed", "sync_throttled"}:
        return True
    return str(readiness.get("next_action") or "") in {"sync_intune", "wait_for_contact", "wait_for_assignment"}


def _maybe_auto_sync_after_upload(conn, run: dict, heartbeat: dict | None, readiness: dict) -> dict:
    if not _should_auto_sync_after_upload(readiness):
        return readiness

    latest_event = _latest_auto_sync_event(conn, run["run_id"])
    latest_age = _event_age_seconds(latest_event)
    latest_type = str((latest_event or {}).get("event_type") or "")
    if latest_age is not None and latest_age < 600:
        if latest_type == "autopilot_cache_auto_sync_failed":
            message = str((latest_event.get("data_json") or {}).get("error") or "Cloud device sync failed")
            return _readiness_with_sync_error(readiness, message)
        return readiness

    cloudosd_pg.append_event(
        conn,
        run_id=run["run_id"],
        phase="AutopilotAgent",
        event_type="autopilot_cache_auto_sync_attempted",
        message="CloudOSD Autopilot upload completed; syncing Autopilot and Intune device cache",
    )
    try:
        counts = _sync_cloud_devices_from_graph(conn=conn)
    except GraphThrottled as exc:
        message = str(exc) or "Graph throttled, retrying later."
        cloudosd_pg.append_event(
            conn,
            run_id=run["run_id"],
            phase="AutopilotAgent",
            event_type="autopilot_cache_sync_throttled",
            severity="warn",
            message=message,
            data={"retry_after_seconds": exc.retry_after_seconds},
        )
        return _readiness_with_graph_throttle(conn, readiness)
    except Exception as exc:
        message = f"Cloud device sync failed after hash upload: {exc}"
        cloudosd_pg.append_event(
            conn,
            run_id=run["run_id"],
            phase="AutopilotAgent",
            event_type="autopilot_cache_auto_sync_failed",
            severity="error",
            message=message,
            data={"error": str(exc)},
        )
        failed = _readiness_with_sync_error(readiness, message)
        cloudosd_pg.upsert_autopilot_readiness(
            conn,
            run_id=run["run_id"],
            state=failed["state"],
            expected_group_tag=failed.get("expected_group_tag"),
            hash_status=(failed.get("hash") or {}).get("status"),
            hash_filename=(failed.get("hash") or {}).get("filename"),
            hash_sha256=(failed.get("hash") or {}).get("sha256"),
            hash_serial=(failed.get("hash") or {}).get("serial"),
            upload_status=(failed.get("upload") or {}).get("status"),
            upload_job_id=(failed.get("upload") or {}).get("job_id"),
            upload_started_at=_parse_timestamp((failed.get("upload") or {}).get("started_at")),
            upload_finished_at=_parse_timestamp((failed.get("upload") or {}).get("finished_at")),
            upload_error=(failed.get("upload") or {}).get("error"),
            autopilot_device_id=(failed.get("autopilot") or {}).get("device_id"),
            imported_serial=(failed.get("autopilot") or {}).get("serial"),
            imported_group_tag=(failed.get("assignment") or {}).get("actual_group_tag"),
            assignment_status=(failed.get("assignment") or {}).get("status"),
            enrollment_status=(failed.get("enrollment") or {}).get("status"),
            contact_state=(failed.get("contact") or {}).get("state"),
            cache_status=(failed.get("cache") or {}).get("status"),
            cache_synced_at=_parse_timestamp((failed.get("cache") or {}).get("synced_at")),
            errors=failed.get("errors") or [],
        )
        return failed

    cloudosd_pg.append_event(
        conn,
        run_id=run["run_id"],
        phase="AutopilotAgent",
        event_type="autopilot_cache_auto_sync_complete",
        message="CloudOSD Autopilot and Intune device cache synced after hash upload",
        data={"counts": counts},
    )
    return autopilot_readiness_for_run(
        conn,
        run,
        heartbeat,
        allow_auto_sync=False,
    )


def _http_retry_after_seconds(exc: Exception, default: int = 600) -> int:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 429:
        return default
    header = (response.headers or {}).get("Retry-After") if response is not None else None
    if header:
        try:
            return max(60, int(float(header)))
        except ValueError:
            pass
    return default


def _is_http_429(exc: Exception) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


def _prefer_hash_file(hash_files: list[dict], preferred_hash_filename: str) -> list[dict]:
    preferred = Path(preferred_hash_filename or "").name
    if not preferred:
        return hash_files
    return sorted(
        hash_files,
        key=lambda item: 0 if item.get("name") == preferred else 1,
    )


def autopilot_readiness_for_run(
    conn,
    run: dict,
    heartbeat: dict | None = None,
    *,
    allow_auto_sync: bool = True,
    preferred_hash_filename: str | None = None,
) -> dict:
    evidence = intune_evidence_for_run(run, heartbeat)
    hash_files = evidence.get("hash", {}).get("files") or []
    attempt = cloudosd_pg.latest_autopilot_upload_attempt(conn, run["run_id"])
    preferred_name = preferred_hash_filename or str((attempt or {}).get("hash_filename") or "")
    if preferred_name:
        hash_files = _prefer_hash_file(hash_files, preferred_name)
        evidence.setdefault("hash", {})["files"] = hash_files
    selected_hash = hash_files[0] if hash_files else {}
    hash_filename = selected_hash.get("name")
    hash_serial = selected_hash.get("serial")
    hash_status = "captured" if hash_filename else "missing"
    expected_group_tag = str(run.get("vm_group_tag") or "").strip()
    upload_job = None
    upload_status = "not_started"
    upload_error = None
    upload_job_id = None
    upload_started_at = None
    upload_finished_at = None
    if attempt:
        upload_job_id = attempt.get("job_id")
        upload_status = attempt.get("status") or "queued"
        upload_started_at = attempt.get("started_at")
        upload_finished_at = attempt.get("finished_at")
        upload_error = attempt.get("error")
    if upload_job_id:
        try:
            from web import app as web_app

            upload_job = web_app.job_manager.get_job(upload_job_id)
        except Exception:
            upload_job = None
        if upload_job:
            job_status = str(upload_job.get("status") or "")
            if job_status in {"pending", "running", "complete", "failed", "canceled"}:
                upload_status = job_status
            upload_started_at = upload_job.get("claimed_at") or upload_job.get("started") or upload_started_at
            upload_finished_at = upload_job.get("ended") or upload_finished_at
            if job_status in {"failed", "canceled"}:
                upload_error = upload_error or _job_log_tail(upload_job_id).strip() or job_status

    errors = list(evidence.get("errors") or [])
    if upload_status in {"failed", "canceled"}:
        errors.append(_readiness_error(
            "upload",
            "upload_failed",
            upload_error or f"Upload job {upload_job_id} is {upload_status}",
        ))

    autopilot_device_id = evidence.get("upload", {}).get("autopilot_device_id")
    imported_group_tag = evidence.get("assignment", {}).get("actual_group_tag")
    assignment_status = evidence.get("assignment", {}).get("status") or "not_found"
    enrollment_status = evidence.get("enrollment", {}).get("status") or "not_found"
    contact_state = evidence.get("enrollment", {}).get("contact_state") or "not_found"
    cache_state = _cache_status(evidence.get("cache_synced_at"))
    cache_synced_at = _parse_timestamp(evidence.get("cache_synced_at"))
    group_tag_match = evidence.get("assignment", {}).get("group_tag_match")

    if hash_status != "captured":
        state = "waiting_for_hash"
        next_action = "wait_for_hash"
    elif upload_status in {"failed", "canceled"}:
        state = "upload_failed"
        next_action = "retry_upload"
    elif upload_status == "not_configured":
        state = "upload_not_configured"
        next_action = "configure_entra"
    elif autopilot_device_id:
        if group_tag_match is False:
            state = "group_tag_mismatch"
            next_action = "review_group_tag"
        elif enrollment_status == "enrolled":
            state = "enrolled"
            next_action = "none"
        elif contact_state not in {"", "not_found", "not_contacted"}:
            state = "contacted"
            next_action = "sync_intune"
        elif _assigned_status(assignment_status):
            state = "assigned"
            next_action = "wait_for_contact"
        else:
            state = "imported"
            next_action = "wait_for_assignment"
    elif upload_status == "complete":
        state = "upload_submitted"
        next_action = "sync_intune"
    elif upload_status == "running":
        state = "upload_running"
        next_action = "wait_for_upload"
    elif upload_status == "pending" or upload_status == "queued":
        state = "upload_queued"
        next_action = "wait_for_upload"
    else:
        state = "hash_captured"
        next_action = "upload_hash"

    if state == "group_tag_mismatch":
        state_label = "group tag mismatch"
    else:
        state_label = state.replace("_", " ")
    detail = evidence.get("summary") or state_label
    if state == "upload_failed" and upload_error:
        detail = upload_error
    elif state == "upload_not_configured":
        detail = upload_error or "Hash captured; Entra upload credentials are not configured"
    elif state == "upload_submitted" and cache_state in {"stale", "expired", "missing"}:
        detail = f"Upload submitted; device cache is {cache_state}; sync Intune"

    readiness = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "state": state,
        "label": state_label,
        "detail": detail,
        "next_action": next_action,
        "expected_group_tag": expected_group_tag,
        "hash": {
            "status": hash_status,
            "filename": hash_filename,
            "sha256": _hash_file_sha256(hash_filename),
            "serial": hash_serial,
            "files": hash_files,
        },
        "upload": {
            "status": upload_status,
            "job_id": upload_job_id,
            "error": upload_error,
            "started_at": upload_started_at,
            "finished_at": upload_finished_at,
        },
        "autopilot": {
            "device_id": autopilot_device_id,
            "serial": evidence.get("autopilot", {}).get("serial"),
            "display_name": evidence.get("autopilot", {}).get("display_name"),
        },
        "assignment": {
            "status": assignment_status,
            "expected_group_tag": expected_group_tag,
            "actual_group_tag": imported_group_tag,
            "group_tag_match": group_tag_match,
        },
        "enrollment": {
            "status": enrollment_status,
            "intune_device_id": evidence.get("enrollment", {}).get("intune_device_id"),
            "intune_device_name": evidence.get("enrollment", {}).get("intune_device_name"),
            "last_sync": evidence.get("enrollment", {}).get("last_sync"),
        },
        "contact": {
            "state": contact_state,
            "last_contact": evidence.get("enrollment", {}).get("autopilot_last_contact"),
        },
        "cache": {
            "status": cache_state,
            "synced_at": evidence.get("cache_synced_at") or "",
        },
        "errors": errors,
    }
    readiness = _readiness_with_graph_throttle(conn, readiness)
    cloudosd_pg.upsert_autopilot_readiness(
        conn,
        run_id=run["run_id"],
        state=readiness["state"],
        expected_group_tag=readiness.get("expected_group_tag"),
        hash_status=(readiness.get("hash") or {}).get("status"),
        hash_filename=(readiness.get("hash") or {}).get("filename"),
        hash_sha256=(readiness.get("hash") or {}).get("sha256"),
        hash_serial=(readiness.get("hash") or {}).get("serial"),
        upload_status=(readiness.get("upload") or {}).get("status"),
        upload_job_id=(readiness.get("upload") or {}).get("job_id"),
        upload_started_at=_parse_timestamp((readiness.get("upload") or {}).get("started_at")),
        upload_finished_at=_parse_timestamp((readiness.get("upload") or {}).get("finished_at")),
        upload_error=(readiness.get("upload") or {}).get("error"),
        autopilot_device_id=(readiness.get("autopilot") or {}).get("device_id"),
        imported_serial=(readiness.get("autopilot") or {}).get("serial"),
        imported_group_tag=(readiness.get("assignment") or {}).get("actual_group_tag"),
        assignment_status=(readiness.get("assignment") or {}).get("status"),
        enrollment_status=(readiness.get("enrollment") or {}).get("status"),
        contact_state=(readiness.get("contact") or {}).get("state"),
        cache_status=(readiness.get("cache") or {}).get("status"),
        cache_synced_at=_parse_timestamp((readiness.get("cache") or {}).get("synced_at")),
        errors=readiness.get("errors") or [],
    )
    if allow_auto_sync:
        return _maybe_auto_sync_after_upload(conn, run, heartbeat, readiness)
    return readiness


def _progress_milestone(
    *,
    state: str,
    label: str,
    at: str | None = None,
    detail: str = "",
) -> dict:
    return {
        "state": state,
        "label": label,
        "at": at or "",
        "detail": detail,
    }


def _pending_or_failed(run: dict, detail: str = "") -> dict:
    state = str(run.get("state") or "")
    if state == "failed":
        return _progress_milestone(state="failed", label="failed", detail=detail)
    return _progress_milestone(state="waiting", label="waiting", detail=detail)


def provision_progress_for_run(conn, run: dict) -> dict:
    run_id = str(run["run_id"])
    heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
    cloudosd_pg.sync_ts_progress_for_run(conn, run_id)
    steps = cloudosd_pg.ts_engine_pg.list_run_steps(conn, run_id)
    v2_completion = cloudosd_pg.v2_completion_status(
        conn,
        run_id,
        domain_join=run.get("domain_join"),
    )
    evidence = intune_evidence_for_run(run, heartbeat)
    readiness = autopilot_readiness_for_run(conn, run, heartbeat)
    failed_v2_steps = [step for step in steps if step.get("state") == "failed"]

    vm_created = (
        _progress_milestone(
            state="done",
            label="created",
            detail=f"VMID {run['vmid']}",
        )
        if run.get("vmid") is not None
        else _pending_or_failed(run, "waiting for Proxmox VM identity")
    )
    pe_registered = (
        _progress_milestone(
            state="done",
            label="registered",
            at=run.get("pe_registered_at"),
        )
        if run.get("pe_registered_at")
        else _pending_or_failed(run, "waiting for PE bridge")
    )
    osdcloud_done = (
        _progress_milestone(
            state="done",
            label="done",
            at=run.get("osdcloud_finished_at"),
        )
        if run.get("osdcloud_finished_at")
        else _pending_or_failed(run, "waiting for OSDCloud completion")
    )
    agent_heartbeat = (
        _progress_milestone(
            state="done",
            label="heartbeat",
            at=run.get("first_heartbeat_at") or (heartbeat or {}).get("received_at"),
            detail=(heartbeat or {}).get("agent_id") or "",
        )
        if run.get("first_heartbeat_at") or heartbeat
        else _pending_or_failed(run, "waiting for AutopilotAgent heartbeat")
    )
    if v2_completion.get("ready"):
        v2_steps_done = _progress_milestone(
            state="done",
            label="done",
            detail="required full-OS steps complete",
        )
    elif failed_v2_steps:
        v2_steps_done = _progress_milestone(
            state="failed",
            label="failed",
            detail=", ".join(step["kind"] for step in failed_v2_steps),
        )
    else:
        v2_steps_done = _pending_or_failed(
            run,
            "waiting for " + ", ".join(v2_completion.get("missing") or []),
        )

    readiness_state = readiness.get("state") or ""
    if readiness_state in {"enrolled", "contacted", "assigned"}:
        intune_state = _progress_milestone(
            state="done",
            label=readiness["label"],
            detail=readiness.get("detail") or "",
        )
    elif readiness_state in {"upload_failed", "group_tag_mismatch", "blocked"} or (
        readiness.get("errors") and readiness_state != "sync_throttled"
    ):
        intune_state = _progress_milestone(
            state="failed",
            label=readiness.get("label") or "needs review",
            detail="; ".join(error["message"] for error in readiness.get("errors") or []) or readiness.get("detail") or "",
        )
    elif readiness_state:
        intune_state = _progress_milestone(
            state="waiting",
            label=readiness.get("label") or readiness_state,
            detail=readiness.get("detail") or "",
        )
    else:
        intune_state = _progress_milestone(
            state="waiting",
            label="waiting",
            detail="waiting for hash capture",
        )

    milestones = {
        "vm_created": vm_created,
        "pe_registered": pe_registered,
        "osdcloud_done": osdcloud_done,
        "agent_heartbeat": agent_heartbeat,
        "v2_steps_done": v2_steps_done,
        "intune_state": intune_state,
    }
    done_count = sum(1 for item in milestones.values() if item["state"] == "done")
    failed_count = sum(1 for item in milestones.values() if item["state"] == "failed")
    return {
        "run_id": run_id,
        "vm_name": run.get("requested_vm_name") or run.get("vm_name"),
        "pve_vm_name": run.get("pve_vm_name"),
        "vmid": run.get("vmid"),
        "state": run.get("state"),
        "created_at": run.get("created_at"),
        "group_tag": run.get("vm_group_tag") or "",
        "source_sequence_id": run.get("source_sequence_id"),
        "done_count": done_count,
        "failed_count": failed_count,
        "total_count": len(milestones),
        "milestones": milestones,
        "intune_evidence": evidence,
        "autopilot_readiness": readiness,
    }


def provision_progress_payload(limit: int = 50, include_archived: bool = False) -> dict:
    limit = max(1, min(int(limit or 50), 100))
    with _conn() as conn:
        runs = cloudosd_pg.list_runs(
            conn,
            limit=limit,
            include_archived=include_archived,
            source_surface="provision",
        )
        rows = [provision_progress_for_run(conn, run) for run in runs]
    return {
        "schema_version": 1,
        "summary": provision_progress_summary(rows),
        "runs": rows,
    }


def provision_progress_summary(rows: list[dict]) -> dict:
    def done(row: dict, key: str) -> bool:
        return ((row.get("milestones") or {}).get(key) or {}).get("state") == "done"

    def uploaded(row: dict) -> bool:
        readiness = row.get("autopilot_readiness") or {}
        evidence = row.get("intune_evidence") or {}
        return bool((readiness.get("autopilot") or {}).get("device_id")) or (
            (readiness.get("upload") or {}).get("status") == "complete"
        ) or ((evidence.get("upload") or {}).get("status") == "uploaded")

    def assigned(row: dict) -> bool:
        readiness = row.get("autopilot_readiness") or {}
        state = readiness.get("state")
        status = (readiness.get("assignment") or {}).get("status")
        return state in {"assigned", "contacted", "enrolled"} or _assigned_status(status)

    def contacted_or_enrolled(row: dict) -> bool:
        readiness = row.get("autopilot_readiness") or {}
        state = readiness.get("state")
        contact = (readiness.get("contact") or {}).get("state")
        enrollment = (readiness.get("enrollment") or {}).get("status")
        return state in {"contacted", "enrolled"} or enrollment == "enrolled" or contact not in {
            "",
            None,
            "not_found",
            "not_contacted",
        }

    return {
        "total": len(rows),
        "deployed": sum(1 for row in rows if done(row, "osdcloud_done")),
        "uploaded": sum(1 for row in rows if uploaded(row)),
        "assigned": sum(1 for row in rows if assigned(row)),
        "contacted_enrolled": sum(1 for row in rows if contacted_or_enrolled(row)),
        "failed": sum(1 for row in rows if row.get("failed_count")),
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
        try:
            from web import setup_artifacts

            registered = [
                Path(row.get("path") or "")
                for row in setup_artifacts.list_artifacts(kind="agent-msi")
                if "arm64" not in str(row.get("filename") or "").lower()
            ]
            registered = [path for path in registered if path.is_file()]
            if registered:
                return max(registered, key=lambda path: path.stat().st_mtime)
        except Exception:
            pass
        setup_msi_dir = _APP_ROOT / "output" / "setup" / "artifacts" / "agent-msi"
        setup_msi_candidates = [
            *setup_msi_dir.glob("*win-x64*.msi"),
            *setup_msi_dir.glob("*.msi"),
        ]
        setup_msi_candidates = [
            path for path in setup_msi_candidates
            if path.is_file() and "arm64" not in path.name.lower()
        ]
        if setup_msi_candidates:
            return max(setup_msi_candidates, key=lambda path: path.stat().st_mtime)
        app_output = _APP_ROOT / "output" / "cloudosd" / "AutopilotAgent.msi"
        if app_output.exists():
            return app_output
        for repo_root in [
            Path(os.environ.get("HOST_REPO_MOUNT", "/host/repo")),
            Path(os.environ.get("HOST_REPO_PATH", "")) if os.environ.get("HOST_REPO_PATH", "").strip() else None,
        ]:
            if not repo_root:
                continue
            host_repo_msi = repo_root / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi"
            if host_repo_msi.exists():
                return host_repo_msi
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
    cache_status = {"policy": "direct_on_miss", "feature_image": None, "quality_updates": []}
    try:
        with _conn() as conn:
            feature = cloudosd_cache.find_feature_entry(
                conn,
                windows_version=body.os_version,
                architecture=body.architecture,
                language=body.os_language,
                activation=body.os_activation,
                edition=body.os_edition,
            )
            quality = cloudosd_cache.matching_quality_updates(
                conn,
                windows_version=body.os_version,
                architecture=body.architecture,
            )
        cache_status = {
            "policy": "direct_on_miss",
            "feature_image": feature,
            "quality_updates": quality,
        }
        if feature and feature.get("status") == "ready":
            warnings.append(_check(
                "cloudosd_feature_cache_hit",
                "CloudOSD feature image cache hit",
                f"{feature['file_name']} will be served from the controller cache.",
            ))
        else:
            warnings.append(_check(
                "cloudosd_feature_cache_miss",
                "CloudOSD feature image cache miss",
                "Deployment can continue from Microsoft; queue cache warming for faster future runs.",
            ))
        if quality:
            warnings.append(_check(
                "cloudosd_quality_cache_ready",
                "CloudOSD quality update cache ready",
                f"{len(quality)} cached quality update package(s) will be applied offline.",
            ))
        else:
            warnings.append(_check(
                "cloudosd_quality_cache_missing",
                "CloudOSD quality update cache missing",
                "No cached LCU/SSU packages are ready for offline servicing.",
            ))
    except Exception as exc:
        cache_status["error"] = str(exc)

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
        "cache": cache_status,
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


def _cache_script_cmd(action: str, *, entry_id: str = "", module_version: str = "") -> list[str]:
    script = _APP_ROOT / "scripts" / "cloudosd_cache_job.py"
    cmd = [sys.executable, str(script), action]
    if entry_id:
        cmd.extend(["--entry-id", entry_id])
    if module_version:
        cmd.extend(["--osdcloud-module-version", module_version])
    return cmd


def _queue_cache_job(job_type: str, cmd: list[str], args: dict) -> str:
    from web import app as web_app

    job = web_app.job_manager.start(job_type, cmd, args=args)
    return job["id"]


@router.get("/cache")
def cache_status():
    with _conn() as conn:
        return cloudosd_cache.payload(conn)


@router.post("/cache/catalog/refresh", status_code=202)
def refresh_cache_catalog():
    version = cloudosd_pg.DEFAULT_OSDCLOUD_MODULE_VERSION
    job_id = _queue_cache_job(
        "cloudosd_cache_refresh_catalog",
        _cache_script_cmd("refresh", module_version=version),
        {"osdcloud_module_version": version},
    )
    return {"ok": True, "job_id": job_id}


@router.post("/cache/feature-images/{entry_id}/warm", status_code=202)
def warm_feature_image(entry_id: str):
    job_id = _queue_cache_job(
        "cloudosd_cache_feature_image",
        _cache_script_cmd("warm", entry_id=entry_id),
        {"entry_id": entry_id, "cache_type": "feature_image"},
    )
    return {"ok": True, "job_id": job_id}


@router.post("/cache/quality-updates/{entry_id}/warm", status_code=202)
def warm_quality_update(entry_id: str):
    job_id = _queue_cache_job(
        "cloudosd_cache_quality_update",
        _cache_script_cmd("warm", entry_id=entry_id),
        {"entry_id": entry_id, "cache_type": "quality_update"},
    )
    return {"ok": True, "job_id": job_id}


@router.post("/cache/{entry_id}/verify", status_code=202)
def verify_cache_entry(entry_id: str):
    job_id = _queue_cache_job(
        "cloudosd_cache_feature_image",
        _cache_script_cmd("verify", entry_id=entry_id),
        {"entry_id": entry_id, "cache_action": "verify"},
    )
    return {"ok": True, "job_id": job_id}


@router.post("/cache/{entry_id}/delete", status_code=202)
def delete_cache_entry(entry_id: str):
    job_id = _queue_cache_job(
        "cloudosd_cache_feature_image",
        _cache_script_cmd("delete", entry_id=entry_id),
        {"entry_id": entry_id, "cache_action": "delete"},
    )
    return {"ok": True, "job_id": job_id}


@router.post("/cache/warm-all-windows11", status_code=202)
def warm_all_windows11():
    with _conn() as conn:
        entries = cloudosd_cache.list_entries(conn, limit=2000)
    queued = []
    for entry in entries:
        if not entry["windows_version"].startswith("Windows 11 "):
            continue
        if entry["status"] == "ready":
            continue
        if entry["entry_type"] == "feature_image":
            job_type = "cloudosd_cache_feature_image"
        elif entry["entry_type"] == "quality_update":
            job_type = "cloudosd_cache_quality_update"
        else:
            continue
        queued.append(_queue_cache_job(
            job_type,
            _cache_script_cmd("warm", entry_id=entry["id"]),
            {"entry_id": entry["id"], "cache_type": entry["entry_type"]},
        ))
    return {"ok": True, "job_ids": queued}


def _download_token_payload(run_id: str, token: str) -> dict:
    if not run_id or not token:
        raise HTTPException(status_code=401, detail="missing cache download token")
    try:
        payload = winpe_token.verify(token)
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    _require_run_token(run_id, payload)
    return payload


@router.head(
    "/cache/{entry_id}/download/{file_name:path}",
    operation_id="head_cloudosd_cache_entry_download",
)
@router.get(
    "/cache/{entry_id}/download/{file_name:path}",
    operation_id="get_cloudosd_cache_entry_download",
)
def download_cache_entry(entry_id: str, file_name: str, request: Request, run_id: str = "", token: str = ""):
    _download_token_payload(run_id, token)
    with _conn() as conn:
        entry = cloudosd_cache.get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="CloudOSD cache entry not found")
        if entry["status"] != "ready":
            raise HTTPException(status_code=409, detail="CloudOSD cache entry is not ready")
        path = Path(entry.get("local_path") or "")
        if not path.is_file():
            cloudosd_cache.mark_status(
                conn,
                entry_id,
                status="missing",
                error=f"cache file missing: {path}",
            )
            raise HTTPException(status_code=404, detail="CloudOSD cache file is missing")
        if Path(file_name).name != entry["file_name"]:
            raise HTTPException(status_code=404, detail="CloudOSD cache filename mismatch")
        headers = {
            "Content-Length": str(path.stat().st_size),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
        }
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers, media_type="application/octet-stream")
        cloudosd_cache.mark_served(conn, entry_id)
        cloudosd_pg.append_event(
            conn,
            run_id=run_id,
            phase="cache",
            event_type=f"cloudosd_cache_{entry['entry_type']}_served",
            message=f"Served CloudOSD cache file {entry['file_name']}",
            data={"entry_id": entry_id, "file_name": entry["file_name"], "size_bytes": path.stat().st_size},
        )
    return FileResponse(path, media_type="application/octet-stream", filename=entry["file_name"], headers=headers)


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
        if body.bubble_id:
            from web import lab_bubbles_pg

            lab_bubbles_pg.init(conn)
            if not lab_bubbles_pg.get_bubble(conn, body.bubble_id):
                raise HTTPException(status_code=404, detail="Bubble not found")
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
            if body.bubble_id:
                from web import lab_bubbles_pg

                lab_bubbles_pg.add_asset(
                    conn,
                    body.bubble_id,
                    asset_type="vm",
                    asset_role=(body.asset_role or "workstation").strip() or "workstation",
                    vmid=run.get("vmid") or run.get("requested_vmid") or body.vmid,
                    run_id=run["run_id"],
                    membership_state="provisioning",
                    actor="cloudosd",
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return run


@router.get("/runs")
def list_runs(limit: int = 100, include_archived: bool = False):
    with _conn() as conn:
        return {
            "schema_version": 1,
            "runs": cloudosd_pg.list_runs(
                conn,
                limit=limit,
                include_archived=include_archived,
            ),
        }


@router.get("/provision/progress")
def cloudosd_provision_progress(limit: int = 50, include_archived: bool = False):
    return provision_progress_payload(limit=limit, include_archived=include_archived)


@router.post("/runs/{run_id}/archive")
def archive_run(run_id: str, reason: str = ""):
    with _conn() as conn:
        run = cloudosd_pg.archive_run(
            conn,
            run_id,
            archived_by="operator",
            reason=reason,
        )
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        cloudosd_pg.append_event(
            conn,
            run_id=run_id,
            phase="controller",
            event_type="run_archived",
            message="CloudOSD run hidden from default history",
            data={"reason": reason or ""},
        )
        return {"ok": True, "run": run}


@router.post("/runs/{run_id}/unarchive")
def unarchive_run(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.unarchive_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        cloudosd_pg.append_event(
            conn,
            run_id=run_id,
            phase="controller",
            event_type="run_unarchived",
            message="CloudOSD run restored to default history",
        )
        return {"ok": True, "run": run}


def _archive_runs_bulk(
    *,
    states: list[str],
    older_than_hours: int,
    reason: str,
    event_type: str,
) -> dict:
    with _conn() as conn:
        runs = cloudosd_pg.archive_runs_by_filter(
            conn,
            states=states,
            older_than_hours=older_than_hours,
            archived_by="operator",
            reason=reason,
        )
        for run in runs:
            cloudosd_pg.append_event(
                conn,
                run_id=run["run_id"],
                phase="controller",
                event_type=event_type,
                message="CloudOSD run hidden from default history",
                data={"reason": reason, "bulk": True},
            )
        return {"ok": True, "archived_count": len(runs), "runs": runs}


@router.post("/runs/archive-stale-failed")
def archive_stale_failed_runs(older_than_hours: int = 12):
    return _archive_runs_bulk(
        states=["failed"],
        older_than_hours=max(1, int(older_than_hours or 12)),
        reason="stale failed CloudOSD run",
        event_type="stale_failed_runs_archived",
    )


@router.post("/runs/archive-completed-old")
def archive_completed_old_runs(older_than_hours: int = 24):
    return _archive_runs_bulk(
        states=["complete"],
        older_than_hours=max(1, int(older_than_hours or 24)),
        reason="completed CloudOSD run hidden from default history",
        event_type="completed_old_runs_archived",
    )


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
        intune_evidence = intune_evidence_for_run(run, heartbeat)
        autopilot_readiness = autopilot_readiness_for_run(conn, run, heartbeat)
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
        "intune_evidence": intune_evidence,
        "autopilot_readiness": autopilot_readiness,
        "related_jobs": _related_jobs(run_id),
        "os_settings": cloudosd_pg.os_settings(run),
        "user_settings": cloudosd_pg.user_settings(run),
        "task": cloudosd_pg.task_settings(run),
    }


def _sync_cloud_devices_from_graph(conn=None) -> dict:
    from web import app as web_app
    from web import devices_pg as devices_db

    sync_error = ""
    try:
        web_app._graph_api("/deviceManagement/windowsAutopilotSettings/sync", method="POST")
    except Exception as exc:
        if _is_http_429(exc):
            retry_after = _http_retry_after_seconds(exc)
            retry_until = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
            message = "Graph throttled, retrying later."
            if conn is not None:
                cloudosd_pg.set_graph_sync_backoff(
                    conn,
                    retry_after_until=retry_until,
                    last_error=f"{message} {exc}",
                )
            raise GraphThrottled(message, retry_after_seconds=retry_after) from exc
        sync_error = str(exc)
    try:
        ap = web_app._graph_api_all("/deviceManagement/windowsAutopilotDeviceIdentities") or []
        it = web_app._graph_api_all("/deviceManagement/managedDevices") or []
        en = web_app._graph_api_all("/devices") or []
    except Exception as exc:
        if _is_http_429(exc):
            retry_after = _http_retry_after_seconds(exc)
            retry_until = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
            message = "Graph throttled, retrying later."
            if conn is not None:
                cloudosd_pg.set_graph_sync_backoff(
                    conn,
                    retry_after_until=retry_until,
                    last_error=f"{message} {exc}",
                )
            raise GraphThrottled(message, retry_after_seconds=retry_after) from exc
        raise
    devices_db.init()
    devices_db.upsert_autopilot(ap)
    devices_db.upsert_intune(it)
    devices_db.upsert_entra(en)
    if conn is not None:
        cloudosd_pg.clear_graph_sync_backoff(conn)
    return {
        "autopilot": len(ap),
        "intune": len(it),
        "entra": len(en),
        "autopilot_service_sync_error": sync_error,
    }


def _readiness_response(conn, run: dict, heartbeat: dict | None = None, **extra) -> dict:
    readiness = autopilot_readiness_for_run(conn, run, heartbeat)
    return {
        "ok": True,
        "run_id": run["run_id"],
        "autopilot_readiness": readiness,
        **extra,
    }


def _queue_autopilot_hash_upload(
    conn,
    run: dict,
    heartbeat: dict | None = None,
    *,
    source: str = "cloudosd_autopilot_readiness",
    preferred_hash_filename: str | None = None,
) -> dict:
    from web import app as web_app

    readiness = autopilot_readiness_for_run(
        conn,
        run,
        heartbeat,
        preferred_hash_filename=preferred_hash_filename,
    )
    hash_filename = preferred_hash_filename or readiness.get("hash", {}).get("filename")
    if not hash_filename:
        raise HTTPException(status_code=409, detail="CloudOSD run has no captured Autopilot hash yet")
    if Path(hash_filename).name != hash_filename:
        raise HTTPException(status_code=409, detail="CloudOSD hash filename is invalid")
    autopilot_device_id = readiness.get("autopilot", {}).get("device_id")
    if autopilot_device_id:
        return {
            "queued": False,
            "job_id": None,
            "reason": "autopilot_device_already_imported",
            "autopilot_readiness": readiness,
        }
    existing_status = str(readiness.get("upload", {}).get("status") or "")
    existing_job_id = readiness.get("upload", {}).get("job_id")
    if existing_status in {"pending", "queued", "running"}:
        return {
            "queued": False,
            "job_id": existing_job_id,
            "reason": "upload_already_active",
            "autopilot_readiness": readiness,
        }
    hash_file = web_app.HASH_DIR / hash_filename
    if not hash_file.is_file():
        raise HTTPException(status_code=409, detail=f"CloudOSD hash file is missing: {hash_filename}")
    cfg = web_app._load_proxmox_config()
    missing_credentials = [
        label
        for label, key in (
            ("ENTRA_APP_ID", "vault_entra_app_id"),
            ("ENTRA_TENANT_ID", "vault_entra_tenant_id"),
            ("ENTRA_APP_SECRET", "vault_entra_app_secret"),
        )
        if not str(cfg.get(key) or "").strip() or "{{" in str(cfg.get(key) or "")
    ]
    if missing_credentials:
        message = (
            "Hash captured; Entra upload credentials are not configured: "
            + ", ".join(missing_credentials)
        )
        cloudosd_pg.record_autopilot_upload_attempt(
            conn,
            run_id=run["run_id"],
            job_id=None,
            hash_filename=hash_filename,
            expected_group_tag=str(run.get("vm_group_tag") or "").strip(),
            status="not_configured",
            error=message,
        )
        cloudosd_pg.append_event(
            conn,
            run_id=run["run_id"],
            phase="AutopilotAgent",
            event_type="autopilot_hash_upload_not_configured",
            message=message,
            data={"missing": missing_credentials, "hash_filename": hash_filename, "source": source},
        )
        readiness = autopilot_readiness_for_run(
            conn,
            run,
            heartbeat,
            preferred_hash_filename=hash_filename,
        )
        return {
            "queued": False,
            "job_id": None,
            "reason": "entra_credentials_missing",
            "missing": missing_credentials,
            "autopilot_readiness": readiness,
        }
    cmd = [
        "ansible-playbook",
        str(web_app.PLAYBOOK_DIR / "upload_hashes.yml"),
        "-e",
        f"hash_file={hash_file}",
    ]
    group_tag = str(run.get("vm_group_tag") or "").strip()
    if group_tag:
        cmd += ["-e", f"vm_group_tag={group_tag}"]
    job = web_app.job_manager.start(
        "upload_hash",
        cmd,
        args={
            "cloudosd_run_id": run["run_id"],
            "vmid": run.get("vmid"),
            "file": hash_filename,
            "group_tag": group_tag,
            "source": source,
        },
    )
    cloudosd_pg.record_autopilot_upload_attempt(
        conn,
        run_id=run["run_id"],
        job_id=job["id"],
        hash_filename=hash_filename,
        expected_group_tag=group_tag,
        status="queued",
    )
    cloudosd_pg.append_event(
        conn,
        run_id=run["run_id"],
        phase="AutopilotAgent",
        event_type="autopilot_hash_upload_queued",
        message="CloudOSD Autopilot hash upload queued",
        data={"job_id": job["id"], "hash_filename": hash_filename, "source": source},
    )
    readiness = autopilot_readiness_for_run(
        conn,
        run,
        heartbeat,
        preferred_hash_filename=hash_filename,
    )
    return {
        "queued": True,
        "job_id": job["id"],
        "reason": "queued",
        "autopilot_readiness": readiness,
    }


def auto_queue_autopilot_hash_upload(run_id: str, *, hash_filename: str | None = None) -> dict:
    try:
        with _conn() as conn:
            run = cloudosd_pg.get_run(conn, run_id)
            if not run:
                return {"queued": False, "reason": "not_cloudosd_run"}
            try:
                heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
            except pg_errors.UndefinedTable:
                conn.rollback()
                heartbeat = None
            result = _queue_autopilot_hash_upload(
                conn,
                run,
                heartbeat,
                source="cloudosd_v2_hash_capture",
                preferred_hash_filename=hash_filename,
            )
            return {
                "queued": bool(result.get("queued")),
                "job_id": result.get("job_id"),
                "reason": result.get("reason"),
                "state": (result.get("autopilot_readiness") or {}).get("state"),
            }
    except HTTPException as exc:
        return {
            "queued": False,
            "reason": "upload_not_queued",
            "status_code": exc.status_code,
            "error": exc.detail,
        }
    except pg_errors.UndefinedTable:
        return {"queued": False, "reason": "not_cloudosd_run"}
    except Exception as exc:
        logger.exception("failed to auto-queue CloudOSD Autopilot hash upload for %s", run_id)
        return {"queued": False, "reason": "upload_queue_error", "error": str(exc)}


@router.get("/runs/{run_id}/autopilot/readiness")
def get_autopilot_readiness(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
        return _readiness_response(conn, run, heartbeat)


@router.post("/runs/{run_id}/autopilot/reconcile")
def reconcile_autopilot_readiness(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
        return _readiness_response(conn, run, heartbeat)


@router.post("/runs/{run_id}/autopilot/sync")
def sync_autopilot_readiness(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
    try:
        with _conn() as conn:
            counts = _sync_cloud_devices_from_graph(conn=conn)
    except GraphThrottled as exc:
        with _conn() as conn:
            run = cloudosd_pg.get_run(conn, run_id)
            heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
            readiness = autopilot_readiness_for_run(conn, run, heartbeat, allow_auto_sync=False)
            return {
                "ok": False,
                "run_id": run_id,
                "message": "Graph throttled, retrying later.",
                "retry_after_seconds": exc.retry_after_seconds,
                "autopilot_readiness": readiness,
            }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cloud device sync failed: {exc}")
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
        return _readiness_response(conn, run, heartbeat, sync=counts)


@router.post("/runs/{run_id}/autopilot/upload", status_code=202)
def upload_autopilot_hash(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        heartbeat = agent_telemetry_pg.latest_for_run(conn, run_id)
        result = _queue_autopilot_hash_upload(conn, run, heartbeat)
        return {
            "ok": True,
            "run_id": run["run_id"],
            "queued": bool(result.get("queued")),
            "reason": result.get("reason"),
            "autopilot_readiness": result["autopilot_readiness"],
            "job_id": result.get("job_id"),
        }


@router.post("/runs/{run_id}/autopilot/retry-upload", status_code=202)
def retry_autopilot_hash_upload(run_id: str):
    return upload_autopilot_hash(run_id)


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
        "vm_oem_profile": run.get("vm_oem_profile") or "",
        "chassis_type_override": int(run.get("chassis_type_override") or 0),
    }
    setup_state = web_app._read_json_file(web_app.SETUP_STATE_PATH)
    virtio_iso = setup_state.get("virtio_iso_volid")
    if virtio_iso:
        extra_vars["proxmox_virtio_iso"] = virtio_iso
    cfg = web_app._load_proxmox_config()
    blank_template_vmid = (
        setup_state.get("cloudosd_blank_template_vmid")
        or setup_state.get("winpe_blank_template_vmid")
        or setup_state.get("osdeploy_blank_template_vmid")
        or cfg.get("cloudosd_blank_template_vmid")
        or cfg.get("winpe_blank_template_vmid")
        or cfg.get("osdeploy_blank_template_vmid")
    )
    if blank_template_vmid:
        extra_vars["cloudosd_blank_template_vmid"] = blank_template_vmid
        extra_vars.setdefault("winpe_blank_template_vmid", blank_template_vmid)
    if run.get("vm_group_tag"):
        extra_vars["vm_group_tag"] = run["vm_group_tag"]
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
