"""OSDeploy v2 controller API."""
from __future__ import annotations

import os
import json
import shutil
import socket
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from web import agent_telemetry_pg, osdeploy_cache, osdeploy_pg, osdeploy_roles, ts_engine_pg, winpe_token


router = APIRouter(prefix="/api/osdeploy/v1", tags=["osdeploy"])
_PE_TOKEN_TTL_SECONDS = 6 * 60 * 60
_AGENT_BOOTSTRAP_TOKEN_TTL_SECONDS = 48 * 60 * 60

_APP_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OSDEPLOY_BUILD_REMOTE = "Adam.Gell@10.211.55.6"
_DEFAULT_OSDEPLOY_BUILD_REMOTE_ROOT = r"F:\BuildRoot"
_DEFAULT_OSDEPLOY_SSH_KEY = _APP_ROOT / "secrets" / "osdeploy_devmachine_ed25519"
_BUILD_HOST_AGENT_CAPABILITIES = [
    "configure_build_host_role",
    "install_build_prerequisites",
    "fetch_source_bundle",
    "build_agent_msi",
    "build_winpe",
    "build_cloudosd",
    "build_osdeploy",
    "publish_artifacts",
]
_BUILD_HOST_DEPENDENCY_CLAIM_TTL_SECONDS = {
    "fetch_source_bundle": 10 * 60,
    "install_build_prerequisites": 2 * 60 * 60,
}


def _osdeploy_source_root() -> Path:
    candidates = [_APP_ROOT, _REPO_ROOT]
    for root in candidates:
        if (root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").exists():
            return root
    return _APP_ROOT


_OSDEPLOY_SOURCE_ROOT = _osdeploy_source_root()


def _asset_path(name: str) -> Path:
    if name == "autopilotagent-postinstall.ps1":
        return _APP_ROOT / "files" / "ninja" / "autopilotagent-postinstall.ps1"
    if name == "autopilotagent.msi":
        return _agent_msi_asset_path()
    raise HTTPException(status_code=404, detail="OSDeploy asset not found")


def _valid_agent_msi(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.stat().st_size < 1024:
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return False
    return header.startswith(b"MZ") or header == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _agent_msi_asset_path() -> Path:
    try:
        from web import setup_artifacts

        release = setup_artifacts.latest_agent_release(runtime_identifier="win-x64")
        if release:
            return Path(release["path"])
    except Exception:
        pass

    candidates: list[Path] = []
    configured = os.environ.get("AUTOPILOT_AGENT_MSI_PATH", "").strip()
    if configured:
        candidates.append(Path(configured))

    setup_msi_dir = _APP_ROOT / "output" / "setup" / "artifacts" / "agent-msi"
    setup_msi_candidates = [
        *setup_msi_dir.glob("*win-x64*.msi"),
        *setup_msi_dir.glob("*.msi"),
    ]
    candidates.extend(
        sorted(
            (
                path
                for path in setup_msi_candidates
                if "arm64" not in path.name.lower()
            ),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
    )
    candidates.append(_APP_ROOT / "output" / "cloudosd" / "AutopilotAgent.msi")
    for repo_root in [
        Path(os.environ.get("HOST_REPO_MOUNT", "/host/repo")),
        Path(os.environ.get("HOST_REPO_PATH", "")) if os.environ.get("HOST_REPO_PATH", "").strip() else None,
    ]:
        if not repo_root:
            continue
        candidates.append(repo_root / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi")
    candidates.append(_REPO_ROOT / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi")

    for candidate in candidates:
        if _valid_agent_msi(candidate):
            return candidate
    raise HTTPException(status_code=404, detail="No valid AutopilotAgent MSI is published.")


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_metadata(name: str, *, required: bool = True) -> dict:
    path = _asset_path(name)
    if not path.is_file():
        if required:
            raise HTTPException(
                status_code=500,
                detail=f"Required OSDeploy asset is missing: {name}",
            )
        return {
            "name": name,
            "available": False,
            "path": str(path),
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "name": name,
        "available": True,
        "path": str(path),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


class ArtifactBuildBody(BaseModel):
    build_mode: str = "auto"
    build_host_agent_id: str = ""
    remote: str = _DEFAULT_OSDEPLOY_BUILD_REMOTE
    remote_root: str = _DEFAULT_OSDEPLOY_BUILD_REMOTE_ROOT
    architecture: str = "amd64"
    osdeploy_module_version: str = osdeploy_pg.DEFAULT_OSDEPLOY_MODULE_VERSION
    osdbuilder_module_version: str = osdeploy_pg.DEFAULT_OSDBUILDER_MODULE_VERSION
    adk_version: str = osdeploy_pg.DEFAULT_ADK_VERSION
    source_media_path: str = ""
    image_name: str = osdeploy_pg.DEFAULT_IMAGE_NAME
    image_index: int = 4
    os_version: str = osdeploy_pg.DEFAULT_OS_VERSION
    os_edition: str = osdeploy_pg.DEFAULT_OS_EDITION
    os_language: str = osdeploy_pg.DEFAULT_OS_LANGUAGE


class BuildHostActivateBody(BaseModel):
    confirm_build_host: bool = False
    work_root: str = r"C:\BuildRoot\ProxmoxVEAutopilot"


class BuildHostRepairBody(BaseModel):
    server_url: Optional[str] = None
    upgrade_agent: bool = True
    runtime_identifier: str = "win-x64"
    allow_stale: bool = False


def build_defaults_payload() -> dict:
    from web import app as web_app

    cfg = web_app._load_vars()
    ssh_key_path = Path(cfg.get("osdeploy_build_ssh_key_path") or str(_DEFAULT_OSDEPLOY_SSH_KEY))
    public_key_path, public_key = _osdeploy_public_key_for_private_key(ssh_key_path)
    return {
        "schema_version": 1,
        "remote": cfg.get("osdeploy_build_remote") or _DEFAULT_OSDEPLOY_BUILD_REMOTE,
        "remote_root": cfg.get("osdeploy_build_remote_root") or _DEFAULT_OSDEPLOY_BUILD_REMOTE_ROOT,
        "ssh_key_path": str(ssh_key_path),
        "ssh_key_exists": _osdeploy_ssh_key_exists(ssh_key_path),
        "ssh_public_key_path": public_key_path,
        "ssh_public_key": public_key,
        "architecture": osdeploy_pg.DEFAULT_ARCHITECTURE,
        "osdeploy_module_version": osdeploy_pg.DEFAULT_OSDEPLOY_MODULE_VERSION,
        "osdbuilder_module_version": osdeploy_pg.DEFAULT_OSDBUILDER_MODULE_VERSION,
        "adk_version": osdeploy_pg.DEFAULT_ADK_VERSION,
        "image_name": osdeploy_pg.DEFAULT_IMAGE_NAME,
        "image_index": 4,
        "os_version": osdeploy_pg.DEFAULT_OS_VERSION,
        "os_edition": osdeploy_pg.DEFAULT_OS_EDITION,
        "os_language": osdeploy_pg.DEFAULT_OS_LANGUAGE,
    }


def _resolved_build_request(body: ArtifactBuildBody) -> dict:
    defaults = build_defaults_payload()
    return {
        "build_mode": _normalize_build_mode(body.build_mode),
        "build_host_agent_id": body.build_host_agent_id.strip(),
        "remote": (
            body.remote
            if body.remote != _DEFAULT_OSDEPLOY_BUILD_REMOTE
            else defaults["remote"]
        ),
        "remote_root": (
            body.remote_root
            if body.remote_root != _DEFAULT_OSDEPLOY_BUILD_REMOTE_ROOT
            else defaults["remote_root"]
        ),
        "architecture": body.architecture or defaults["architecture"],
        "osdeploy_module_version": body.osdeploy_module_version,
        "osdbuilder_module_version": body.osdbuilder_module_version,
        "adk_version": body.adk_version,
        "source_media_path": body.source_media_path or "",
        "image_name": body.image_name,
        "image_index": body.image_index,
        "os_version": body.os_version,
        "os_edition": body.os_edition,
        "os_language": body.os_language,
        "ssh_key_path": defaults["ssh_key_path"],
    }


def _normalize_build_mode(value: str | None) -> str:
    mode = str(value or "auto").strip().lower().replace("-", "_")
    if mode in {"auto", "ssh", "build_host_agent"}:
        return mode
    return mode


def _osdeploy_remote_host(remote: str) -> str:
    host = str(remote or "").strip().rsplit("@", 1)[-1].strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    if ":" in host and host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _osdeploy_ssh_key_exists(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _osdeploy_public_key_for_private_key(path: Path) -> tuple[str, str | None]:
    public_key_path = Path(f"{path}.pub")
    if not public_key_path.is_file():
        return str(public_key_path), None
    return str(public_key_path), public_key_path.read_text(encoding="utf-8").strip() or None


def _osdeploy_remote_ssh_reachable(host: str) -> bool:
    try:
        with socket.create_connection((host, 22), timeout=3):
            return True
    except OSError:
        return False


def _heartbeat_age_seconds(value: object) -> int | None:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return None


def _agent_capabilities(latest: dict | None) -> set[str]:
    raw = (latest or {}).get("raw_json") or {}
    capabilities = raw.get("capabilities") if isinstance(raw, dict) else []
    if not isinstance(capabilities, list):
        return set()
    return {str(item).strip() for item in capabilities if str(item).strip()}


def _is_loopback_controller_url(value: str) -> bool:
    try:
        host = (urlparse(str(value)).hostname or "").lower()
    except Exception:
        return False
    return host in {"localhost", "::1"} or host.startswith("127.")


def _preferred_controller_url(*candidates: str) -> str:
    normalized = [str(item or "").strip().rstrip("/") for item in candidates]
    for candidate in normalized:
        if candidate and not _is_loopback_controller_url(candidate):
            return candidate
    for candidate in normalized:
        if candidate:
            return candidate
    return ""


def _guest_reachable_controller_url() -> str:
    from web import app as web_app

    try:
        return str(
            web_app._derive_guest_reachable_base_url(
                web_app._load_proxmox_config(),
            ) or ""
        ).strip().rstrip("/")
    except Exception:
        return ""


def _explicit_build_host_agent_target(agent_id: str, controller_url: str) -> dict:
    agent_id = agent_id.strip()
    blocking = []
    latest = None
    device = None
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        latest = agent_telemetry_pg.latest_for_agent(conn, agent_id)
    if not device:
        blocking.append(_blocking_check(
            "build_host_agent_not_registered",
            f"Build-host agent is not registered: {agent_id}",
        ))
    if not latest:
        blocking.append(_blocking_check(
            "build_host_agent_not_ready",
            "Build-host agent has not produced a heartbeat.",
        ))
    current_phase = str((latest or {}).get("current_phase") or "").strip().lower()
    if latest and current_phase != "build-host":
        blocking.append(_blocking_check(
            "build_host_agent_wrong_phase",
            f"Agent {agent_id} is reporting phase {current_phase or 'unknown'}, not build-host.",
        ))
    heartbeat_at = (latest or {}).get("received_at") or (device or {}).get("last_seen_at")
    age_seconds = _heartbeat_age_seconds(heartbeat_at)
    if latest and (age_seconds is None or age_seconds > 180):
        blocking.append(_blocking_check(
            "build_host_agent_stale",
            "Build-host agent heartbeat is stale.",
        ))
    raw = (latest or {}).get("raw_json") or {}
    agent_server_url = ""
    if isinstance(raw, dict):
        agent_server_url = str(raw.get("server_url") or "").strip().rstrip("/")
    effective_controller_url = _preferred_controller_url(
        agent_server_url,
        controller_url,
        _guest_reachable_controller_url(),
    )
    if not effective_controller_url:
        blocking.append(_blocking_check(
            "controller_url_missing",
            "Controller URL is required so the build-host agent can fetch the source bundle.",
        ))
    if not device:
        agent_state = "missing"
    elif not latest:
        agent_state = "no_heartbeat"
    elif current_phase != "build-host":
        agent_state = "wrong_phase"
    elif age_seconds is None or age_seconds > 180:
        agent_state = "stale"
    elif blocking:
        agent_state = "blocked"
    else:
        agent_state = "ready"
    return {
        "available": not blocking,
        "agent_id": agent_id,
        "vmid": (latest or device or {}).get("vmid"),
        "controller_url": effective_controller_url,
        "source_bundle_url": (
            f"{effective_controller_url}/api/setup/v1/source-bundle.zip"
            if effective_controller_url else None
        ),
        "agent_state": agent_state,
        "last_heartbeat_at": heartbeat_at.isoformat() if hasattr(heartbeat_at, "isoformat") else heartbeat_at,
        "last_heartbeat_age_seconds": age_seconds,
        "primary_ipv4": (latest or {}).get("primary_ipv4"),
        "computer_name": (latest or device or {}).get("computer_name"),
        "blocking_checks": blocking,
    }


def _build_host_agent_target(agent_id_override: str = "") -> dict:
    from web import app as web_app

    try:
        readiness = web_app._setup_readiness()
    except Exception:
        readiness = {}
    controller = readiness.get("controller") or {}
    build_host = readiness.get("build_host") or {}
    controller_url = _preferred_controller_url(
        str(controller.get("url") or "").strip().rstrip("/"),
        os.environ.get("AUTOPILOT_BASE_URL", "").strip().rstrip("/"),
        _guest_reachable_controller_url(),
    )
    if agent_id_override.strip():
        return _explicit_build_host_agent_target(agent_id_override, controller_url)
    agent_id = str(build_host.get("expected_agent_id") or "").strip()
    vmid = build_host.get("vmid")
    blocking = []
    if not agent_id:
        blocking.append(_blocking_check(
            "build_host_agent_missing",
            "Build-host agent identity is not published in setup readiness.",
        ))
    if not bool(build_host.get("agent_ready")):
        blocking.append(_blocking_check(
            "build_host_agent_not_ready",
            "Build-host agent is not ready or has not produced a recent heartbeat.",
        ))
    if not controller_url:
        blocking.append(_blocking_check(
            "controller_url_missing",
            "Controller URL is required so the build-host agent can fetch the source bundle.",
        ))
    return {
        "available": not blocking,
        "agent_id": agent_id,
        "vmid": vmid,
        "controller_url": controller_url,
        "source_bundle_url": (
            f"{controller_url}/api/setup/v1/source-bundle.zip"
            if controller_url else None
        ),
        "agent_state": build_host.get("agent_state"),
        "last_heartbeat_age_seconds": build_host.get("last_heartbeat_age_seconds"),
        "blocking_checks": blocking,
    }


class ArtifactPublishBody(BaseModel):
    node: Optional[str] = None
    storage: Optional[str] = None


class RunCreateBody(BaseModel):
    artifact_id: str = Field(min_length=1)
    vm_name: str = Field(min_length=1)
    node: Optional[str] = None
    iso_storage: Optional[str] = None
    storage: Optional[str] = None
    network_bridge: Optional[str] = None
    vmid: Optional[int] = Field(default=None, ge=1)
    architecture: str = "amd64"
    server_role: str = "base"
    os_version: str = osdeploy_pg.DEFAULT_OS_VERSION
    os_edition: str = osdeploy_pg.DEFAULT_OS_EDITION
    os_language: str = osdeploy_pg.DEFAULT_OS_LANGUAGE
    vm_cores: int = Field(default=osdeploy_pg.DEFAULT_VM_CORES, ge=1)
    vm_memory_mb: int = Field(default=osdeploy_pg.RECOMMENDED_VM_MEMORY_MB, ge=1)
    vm_disk_size_gb: int = Field(default=osdeploy_pg.DEFAULT_VM_DISK_SIZE_GB, ge=1)
    secure_boot: bool = False
    outbound_policy: dict = Field(default_factory=dict)
    role_options: dict = Field(default_factory=dict)
    bubble_id: Optional[str] = None
    asset_role: Optional[str] = None


class IdentityBody(BaseModel):
    vmid: Optional[int] = Field(default=None, ge=1)
    vm_uuid: Optional[str] = None
    mac: Optional[str] = None
    node: Optional[str] = None
    computer_name: Optional[str] = None


class PeRegisterBody(BaseModel):
    client_version: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class PeIdentityRegisterBody(BaseModel):
    vm_uuid: str = Field(min_length=1)
    mac: str = Field(min_length=1)
    architecture: str = "amd64"
    build_sha: Optional[str] = None
    client_version: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class EventBody(BaseModel):
    phase: str = "controller"
    event_type: str = Field(min_length=1)
    severity: str = "info"
    message: Optional[str] = None
    data: dict = Field(default_factory=dict)


def _database_url() -> str:
    from web import app as web_app

    try:
        return web_app._database_url()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="OSDeploy database is not configured")


@contextmanager
def _conn():
    from web import db_pg

    with db_pg.connection(_database_url()) as conn:
        ts_engine_pg.init(conn)
        agent_telemetry_pg.init(conn)
        osdeploy_pg.init(conn)
        osdeploy_cache.init(conn)
        yield conn


def catalog_payload() -> dict:
    return {
        "schema_version": 1,
        "defaults": {
            "architecture": osdeploy_pg.DEFAULT_ARCHITECTURE,
            "os_version": osdeploy_pg.DEFAULT_OS_VERSION,
            "os_edition": osdeploy_pg.DEFAULT_OS_EDITION,
            "os_language": osdeploy_pg.DEFAULT_OS_LANGUAGE,
            "osdeploy_module_version": osdeploy_pg.DEFAULT_OSDEPLOY_MODULE_VERSION,
            "osdbuilder_module_version": osdeploy_pg.DEFAULT_OSDBUILDER_MODULE_VERSION,
            "adk_version": osdeploy_pg.DEFAULT_ADK_VERSION,
            "vm_cores": osdeploy_pg.DEFAULT_VM_CORES,
            "vm_memory_mb": osdeploy_pg.RECOMMENDED_VM_MEMORY_MB,
            "minimum_vm_memory_mb": osdeploy_pg.MIN_VM_MEMORY_MB,
            "vm_disk_size_gb": osdeploy_pg.DEFAULT_VM_DISK_SIZE_GB,
            "minimum_vm_disk_size_gb": osdeploy_pg.MIN_VM_DISK_SIZE_GB,
        },
        "server_roles": osdeploy_pg.SERVER_ROLE_CATALOG,
        "role_catalog": osdeploy_roles.catalog_payload(),
        "os_versions": ["Windows Server 2022", "Windows Server 2025"],
        "os_editions": ["Datacenter", "Standard"],
        "os_languages": ["en-us"],
    }


def proxmox_options_payload() -> dict:
    from web import app as web_app

    cfg = web_app._load_vars()
    defaults = {
        "node": cfg.get("proxmox_node", "pve"),
        "iso_storage": cfg.get("proxmox_iso_storage", "local"),
        "disk_storage": cfg.get("proxmox_storage", "local-lvm"),
        "bridge": cfg.get("proxmox_bridge", "vmbr0"),
    }
    try:
        nodes = [item["node"] for item in web_app._proxmox_api("/nodes")]
        storages = web_app._proxmox_api("/storage")
        networks = web_app._proxmox_api(f"/nodes/{defaults['node']}/network")
        iso = [item["storage"] for item in storages if "iso" in str(item.get("content") or "")]
        disk = [item["storage"] for item in storages if "images" in str(item.get("content") or "")]
        bridges = [item["iface"] for item in networks if item.get("type") == "bridge"]
    except Exception:
        nodes = [defaults["node"]]
        iso = [defaults["iso_storage"]]
        disk = [defaults["disk_storage"]]
        bridges = [defaults["bridge"]]
    return {
        "schema_version": 1,
        "nodes": nodes or [defaults["node"]],
        "storages": {
            "iso": iso or [defaults["iso_storage"]],
            "disk": disk or [defaults["disk_storage"]],
        },
        "bridges": bridges or [defaults["bridge"]],
        "defaults": defaults,
    }


def _base_url(request: Request | None = None) -> str:
    configured = os.environ.get("AUTOPILOT_BASE_URL", "").strip().rstrip("/")
    if configured and not _is_loopback_controller_url(configured):
        return configured
    request_url = ""
    if request is None:
        request_url = "http://127.0.0.1:5000"
    else:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        request_url = f"{proto}://{host}".rstrip("/")
    return _preferred_controller_url(configured, request_url, _guest_reachable_controller_url())


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


def osdeploy_provision_extra_vars(
    *,
    run: dict,
    artifact: dict,
    request: Request | None = None,
) -> dict:
    from web import app as web_app

    cfg = web_app._load_vars()
    requested_name = run.get("requested_vm_name") or run["vm_name"]
    expected_name = run.get("expected_computer_name") or requested_name
    extra_vars = {
        "osdeploy_run_id": run["run_id"],
        "osdeploy_artifact_id": artifact["id"],
        "osdeploy_artifact_volid": artifact["proxmox_volid"],
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
        "server_role": run["server_role"],
        "secure_boot": run["secure_boot"],
    }
    if not run["secure_boot"]:
        extra_vars["vm_bios"] = "seabios"
    blank_template = cfg.get("osdeploy_blank_template_vmid") or cfg.get("winpe_blank_template_vmid")
    if blank_template:
        extra_vars["osdeploy_blank_template_vmid"] = blank_template
    virtio_iso = _resolve_virtio_iso_volid(web_app, cfg, run["node"])
    if not virtio_iso:
        raise HTTPException(
            status_code=409,
            detail="OSDeploy requires a configured VirtIO driver ISO before provisioning.",
        )
    extra_vars["proxmox_virtio_iso"] = virtio_iso
    if run.get("requested_vmid"):
        extra_vars["requested_vmid"] = run["requested_vmid"]
    return extra_vars


def _volid_storage(volid: str | None) -> str:
    return str(volid or "").split(":", 1)[0].strip()


def _volid_exists(web_app, *, node: str, volid: str | None) -> bool:
    storage = _volid_storage(volid)
    if not storage:
        return False
    try:
        content = web_app._proxmox_api(f"/nodes/{node}/storage/{storage}/content")
    except Exception:
        return False
    return any(str(item.get("volid") or "") == str(volid) for item in content or [])


def _discover_virtio_iso_volid(web_app, *, node: str, preferred_storage: str | None = None) -> str | None:
    try:
        storages = web_app._proxmox_api("/storage")
    except Exception:
        storages = []
    iso_storages = [
        str(item.get("storage") or "")
        for item in storages or []
        if "iso" in str(item.get("content") or "")
    ]
    ordered_storages = []
    for storage in [preferred_storage, *iso_storages]:
        if storage and storage not in ordered_storages:
            ordered_storages.append(storage)
    for storage in ordered_storages:
        try:
            content = web_app._proxmox_api(f"/nodes/{node}/storage/{storage}/content")
        except Exception:
            continue
        candidates = sorted(
            str(item.get("volid") or "")
            for item in content or []
            if str(item.get("format") or "") == "iso"
            and Path(str(item.get("volid") or "")).name.lower().startswith("virtio-win")
        )
        if candidates:
            return candidates[-1]
    return None


def _resolve_virtio_iso_volid(web_app, cfg: dict, node: str) -> str | None:
    state = web_app._read_json_file(web_app.SETUP_STATE_PATH)
    candidates = [
        cfg.get("proxmox_virtio_iso"),
        cfg.get("virtio_iso"),
        state.get("virtio_iso_volid"),
    ]
    for volid in candidates:
        if volid and _volid_exists(web_app, node=node, volid=str(volid)):
            return str(volid)
    return _discover_virtio_iso_volid(
        web_app,
        node=node,
        preferred_storage=cfg.get("proxmox_iso_storage"),
    )


def _storage_names_by_content(web_app) -> dict[str, set[str]] | None:
    try:
        storages = web_app._proxmox_api("/storage")
    except Exception:
        return None
    return {
        "iso": {
            str(item.get("storage") or "")
            for item in storages or []
            if "iso" in str(item.get("content") or "")
        },
        "images": {
            str(item.get("storage") or "")
            for item in storages or []
            if "images" in str(item.get("content") or "")
        },
    }


def _package_response(*, run: dict, artifact: dict, server_base_url: str) -> dict:
    expected_computer_name = (
        run.get("expected_computer_name")
        or osdeploy_pg.normalize_windows_computer_name(run.get("vm_name"))
    )
    enriched_artifact = enrich_artifact(artifact)
    agent_msi = _asset_metadata("autopilotagent.msi", required=False)
    postinstall = _asset_metadata("autopilotagent-postinstall.ps1")
    agent_id = f"agent-{expected_computer_name.lower()}" if expected_computer_name else ""
    return {
        "schema_version": 1,
        "run_id": run["run_id"],
        "bearer_token": _sign(run["run_id"], ttl_seconds=_PE_TOKEN_TTL_SECONDS),
        "workflow_name": run["workflow_name"],
        "server_base_url": server_base_url,
        "artifact": enriched_artifact,
        "identity": {
            "vmid": run["vmid"],
            "vm_uuid": run["vm_uuid"],
            "mac": run["mac"],
            "node": run["node"],
            "requested_name": run.get("requested_vm_name") or run["vm_name"],
            "pve_name": run.get("pve_vm_name"),
            "computer_name": expected_computer_name,
        },
        "server_settings": {
            "role": run["server_role"],
            "os_version": run["os_version"],
            "os_edition": run["os_edition"],
            "os_language": run["os_language"],
            "secure_boot": run["secure_boot"],
            "outbound_policy": run.get("outbound_policy") or {},
        },
        "local_admin": run.get("local_admin") or {
            "username": osdeploy_pg.DEFAULT_LOCAL_ADMIN_USERNAME,
            "password": "",
        },
        "deployment": {
            "path": "osdeploy_v2",
            "factory": "OSDeploy/OSD/OSDBuilder",
            "source_surface": "osdeploy",
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
            "pe_event": {
                "url": f"{server_base_url}/api/osdeploy/v1/runs/{run['run_id']}/events",
            },
        },
        "agent": {
            "phase": "full_os",
            "role": run["server_role"],
            "agent_id": agent_id,
            "bootstrap_token": _sign(
                run["run_id"],
                ttl_seconds=_AGENT_BOOTSTRAP_TOKEN_TTL_SECONDS,
            ),
            "bootstrap_url": f"{server_base_url}/api/agent/v1/bootstrap",
            "config_url": f"{server_base_url}/api/agent/v1/config",
            "run_id": run["run_id"],
            "vmid": run["vmid"],
        },
    }


def enrich_artifact(artifact: dict | None) -> dict | None:
    if artifact is None:
        return None
    out = dict(artifact)
    out["source_image_index"] = out.get("image_index")
    output_image_index = _artifact_manifest_int(out, "output_image_index")
    if output_image_index:
        out["output_image_index"] = output_image_index
        out["apply_image_index"] = output_image_index
    else:
        out["apply_image_index"] = out.get("image_index")
    missing = []
    if not out.get("iso_sha256"):
        missing.append("iso_sha256")
    if not out.get("wim_sha256"):
        missing.append("wim_sha256")
    if not out.get("proxmox_volid"):
        missing.append("proxmox_volid")
    out["ready"] = not missing
    out["readiness"] = "ready" if not missing else "missing_" + "_".join(missing)
    out["build_job_url"] = f"/jobs/{out['build_job_id']}" if out.get("build_job_id") else None
    out["build_log_url"] = f"/api/jobs/{out['build_job_id']}/log" if out.get("build_job_id") else None
    out["publish_job_url"] = f"/jobs/{out['publish_job_id']}" if out.get("publish_job_id") else None
    out["publish_log_url"] = f"/api/jobs/{out['publish_job_id']}/log" if out.get("publish_job_id") else None
    return out


def _artifact_manifest_int(artifact: dict, key: str) -> int | None:
    manifest_path = str(artifact.get("manifest_path") or "").strip()
    if not manifest_path:
        return None
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        value = int(data.get(key) or 0)
    except Exception:
        return None
    return value if value > 0 else None


def _job_evidence(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    from web import jobs_pg

    job = jobs_pg.get_job(job_id)
    if not job:
        return {
            "id": job_id,
            "status": "not_found",
            "url": f"/jobs/{job_id}",
            "log_url": f"/api/jobs/{job_id}/log",
        }
    return {
        "id": job["id"],
        "job_type": job["job_type"],
        "status": job["status"],
        "exit_code": job.get("exit_code"),
        "created_at": job.get("created_at"),
        "claimed_at": job.get("claimed_at"),
        "last_heartbeat": job.get("last_heartbeat"),
        "ended_at": job.get("ended_at"),
        "url": f"/jobs/{job['id']}",
        "log_url": f"/api/jobs/{job['id']}/log",
    }


def _blocking_check(check_id: str, message: str) -> dict:
    return {"id": check_id, "message": message, "severity": "block"}


def _warning_check(check_id: str, message: str) -> dict:
    return {"id": check_id, "message": message, "severity": "warning"}


def build_preflight_payload(body: ArtifactBuildBody) -> dict:
    generic_blocking = []
    ssh_blocking = []
    warnings = []
    resolved = _resolved_build_request(body)
    build_mode = resolved["build_mode"]
    build_script = _OSDEPLOY_SOURCE_ROOT / "tools" / "osdeploy-build" / "build-osdeploy.ps1"
    wrapper = _APP_ROOT / "scripts" / "osdeploy_remote_build.py"
    key_path = Path(resolved["ssh_key_path"])
    remote_host = _osdeploy_remote_host(resolved["remote"])
    build_host_agent = _build_host_agent_target(resolved["build_host_agent_id"])

    if build_mode not in {"auto", "ssh", "build_host_agent"}:
        generic_blocking.append(_blocking_check(
            "unsupported_build_mode",
            f"Unsupported OSDeploy artifact build mode: {build_mode}",
        ))
    if not build_script.exists():
        generic_blocking.append(_blocking_check(
            "build_tools_missing",
            "OSDeploy build tools are missing from the selected source root.",
        ))
    if not wrapper.exists():
        ssh_blocking.append(_blocking_check(
            "build_wrapper_missing",
            "OSDeploy remote build wrapper is missing from the app scripts directory.",
        ))
    if not shutil.which("ssh"):
        ssh_blocking.append(_blocking_check("ssh_missing", "ssh is not installed in the runtime container."))
    if not shutil.which("scp"):
        ssh_blocking.append(_blocking_check("scp_missing", "scp is not installed in the runtime container."))
    if not _osdeploy_ssh_key_exists(key_path):
        ssh_blocking.append(_blocking_check(
            "ssh_key_missing",
            f"OSDeploy build host SSH key is missing at {key_path}.",
        ))
    if not remote_host:
        ssh_blocking.append(_blocking_check("remote_host_missing", "OSDeploy build host was not parsed from the remote value."))
    elif not _osdeploy_remote_ssh_reachable(remote_host):
        ssh_blocking.append(_blocking_check(
            "remote_ssh_unreachable",
            f"OSDeploy build host {remote_host}:22 is not reachable from the runtime container.",
        ))
    if not resolved["remote_root"]:
        ssh_blocking.append(_blocking_check("remote_root_missing", "OSDeploy remote build root is required."))

    ssh_available = not generic_blocking and not ssh_blocking
    agent_blocking = build_host_agent["blocking_checks"]
    agent_available = not generic_blocking and not agent_blocking
    selected_build_mode = None
    if build_mode == "ssh" and ssh_available:
        selected_build_mode = "ssh"
    elif build_mode == "build_host_agent" and agent_available:
        selected_build_mode = "build_host_agent"
    elif build_mode == "auto":
        selected_build_mode = "ssh" if ssh_available else ("build_host_agent" if agent_available else None)

    if build_mode == "ssh":
        blocking = generic_blocking + ssh_blocking
    elif build_mode == "build_host_agent":
        blocking = generic_blocking + agent_blocking
    else:
        blocking = generic_blocking
        if not selected_build_mode:
            blocking.extend(ssh_blocking)
            blocking.extend(agent_blocking)
            if not generic_blocking:
                blocking.append(_blocking_check(
                    "no_build_path_ready",
                    "No OSDeploy artifact build path is ready; configure direct SSH or a ready build-host agent.",
                ))

    return {
        "schema_version": 1,
        "build_allowed": bool(selected_build_mode) and not blocking,
        "blocking_checks": blocking,
        "warnings": warnings,
        "target": {
            "build_mode": build_mode,
            "build_host_agent_id": resolved["build_host_agent_id"],
            "selected_build_mode": selected_build_mode,
            "remote": resolved["remote"],
            "remote_host": remote_host,
            "remote_root": resolved["remote_root"],
            "architecture": resolved["architecture"],
            "source_root": str(_OSDEPLOY_SOURCE_ROOT),
            "build_script": str(build_script),
            "ssh_key_path": str(key_path),
            "ssh": {
                "available": ssh_available,
                "blocking_checks": ssh_blocking,
            },
            "build_host_agent": build_host_agent,
        },
    }


def _queue_build_host_agent_build(
    body: ArtifactBuildBody,
    build_preflight: dict,
) -> dict:
    from web import app as web_app

    agent = build_preflight["target"]["build_host_agent"]
    agent_id = agent["agent_id"]
    vmid = int(agent["vmid"] or 0) or None
    controller_url = str(agent["controller_url"]).rstrip("/")
    request_base = {
        "controller_url": controller_url,
        "source_bundle_url": f"{controller_url}/api/setup/v1/source-bundle.zip",
        "work_root": r"C:\BuildRoot\ProxmoxVEAutopilot",
        "build_contract_version": 11,
        "architecture": body.architecture,
        "install_adk": True,
        "adk_url": "https://go.microsoft.com/fwlink/?linkid=2289980",
        "winpe_addon_url": "https://go.microsoft.com/fwlink/?linkid=2289981",
        "osdeploy_version": body.osdeploy_module_version,
        "osdbuilder_version": body.osdbuilder_module_version,
        "adk_version": body.adk_version,
        "source_manifest": web_app._repo_git_metadata_for_bundle(),
    }
    dependencies: list[dict] = []
    with _conn() as conn:
        if not agent_telemetry_pg.get_device(conn, agent_id):
            raise HTTPException(
                status_code=409,
                detail=f"build-host agent is not registered: {agent_id}",
            )
        for kind in ("install_build_prerequisites", "fetch_source_bundle"):
            dependency = _ensure_build_host_dependency_work_item(
                conn,
                agent_id=agent_id,
                kind=kind,
                vmid=vmid,
                request={**request_base, "kind": kind},
            )
            dependencies.append(dependency)
        row = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="build_osdeploy",
            vmid=vmid,
            request={
                **request_base,
                "kind": "build_osdeploy",
                "source_media_path": body.source_media_path or "",
                "image_name": body.image_name,
                "image_index": body.image_index,
                "os_version": body.os_version,
                "os_edition": body.os_edition,
                "os_language": body.os_language,
            },
        )
    return {
        "ok": True,
        "job_type": "osdeploy_build_host_agent",
        "work_item_id": row["id"],
        "agent_id": row["agent_id"],
        "kind": row["kind"],
        "status": row["status"],
        "dependencies": dependencies,
    }


def _ensure_build_host_dependency_work_item(
    conn,
    *,
    agent_id: str,
    kind: str,
    vmid: int | None,
    request: dict,
) -> dict:
    existing = conn.execute(
        """
        SELECT *
        FROM agent_work_items
        WHERE agent_id = %s AND kind = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (agent_id, kind),
    ).fetchone()
    if existing and existing["status"] in {"pending", "complete"} and _dependency_request_matches(existing, request):
        return {
            "id": str(existing["id"]),
            "kind": str(existing["kind"]),
            "status": str(existing["status"]),
            "queued": False,
        }
    if (
        existing
        and existing["status"] == "claimed"
        and _dependency_request_matches(existing, request)
        and _claimed_dependency_is_fresh(existing, kind)
    ):
        return {
            "id": str(existing["id"]),
            "kind": str(existing["kind"]),
            "status": str(existing["status"]),
            "queued": False,
        }
    row = agent_telemetry_pg.create_work_item(
        conn,
        agent_id=agent_id,
        kind=kind,
        vmid=vmid,
        request=request,
    )
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "queued": True,
    }


def _dependency_request_matches(row: dict, request: dict) -> bool:
    current = row.get("request_json")
    if not isinstance(current, dict):
        return False
    return current.get("build_contract_version") == request.get("build_contract_version")


def _claimed_dependency_is_fresh(row: dict, kind: str) -> bool:
    claimed_at = row.get("claimed_at")
    age_seconds = _heartbeat_age_seconds(claimed_at)
    if age_seconds is None:
        return False
    ttl = _BUILD_HOST_DEPENDENCY_CLAIM_TTL_SECONDS.get(kind, 30 * 60)
    return age_seconds <= ttl


@router.post("/build-host/agents/{agent_id}/activate", status_code=202)
def activate_build_host_agent(agent_id: str, body: BuildHostActivateBody):
    if not body.confirm_build_host:
        raise HTTPException(
            status_code=400,
            detail="confirm_build_host must be true before activating an agent as an OSDeploy build host",
        )
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        latest = agent_telemetry_pg.latest_for_agent(conn, agent_id)
        if not device:
            raise HTTPException(
                status_code=404,
                detail=f"agent is not registered: {agent_id}",
            )
        if not latest:
            raise HTTPException(
                status_code=409,
                detail=f"agent has not produced a heartbeat: {agent_id}",
            )
        heartbeat_at = latest.get("received_at") or device.get("last_seen_at")
        age_seconds = _heartbeat_age_seconds(heartbeat_at)
        if age_seconds is None or age_seconds > 180:
            raise HTTPException(
                status_code=409,
                detail="agent heartbeat is stale; wait for a fresh heartbeat before build-host activation",
            )
        capabilities = _agent_capabilities(latest)
        if "configure_build_host_role" not in capabilities:
            raise HTTPException(
                status_code=409,
                detail=(
                    "agent does not advertise configure_build_host_role; "
                    "upgrade/restart AutopilotAgent before build-host activation"
                ),
            )
        vmid = latest.get("vmid") or device.get("vmid")
        row = agent_telemetry_pg.create_work_item(
            conn,
            agent_id=agent_id,
            kind="configure_build_host_role",
            vmid=int(vmid) if vmid is not None else None,
            request={
                "kind": "configure_build_host_role",
                "phase": "build-host",
                "role": "build-host",
                "work_root": body.work_root or r"C:\BuildRoot\ProxmoxVEAutopilot",
                "controller_url": _base_url(None),
                "capabilities": _BUILD_HOST_AGENT_CAPABILITIES,
            },
        )
    return {
        "ok": True,
        "agent_id": row["agent_id"],
        "work_item_id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "vmid": row.get("vmid"),
        "next_expected_phase": "build-host",
    }


@router.post("/build-host/agents/{agent_id}/repair")
def repair_build_host_agent(agent_id: str, body: BuildHostRepairBody):
    from web import app as web_app

    runtime_identifier = body.runtime_identifier.strip().lower()
    if runtime_identifier not in {"win-x64", "win-arm64"}:
        raise HTTPException(status_code=400, detail="unsupported runtime_identifier")
    with _conn() as conn:
        device = agent_telemetry_pg.get_device(conn, agent_id)
        latest = agent_telemetry_pg.latest_for_agent(conn, agent_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"agent is not registered: {agent_id}")
    if not latest:
        raise HTTPException(status_code=409, detail=f"agent has not produced a heartbeat: {agent_id}")
    heartbeat_at = latest.get("received_at") or device.get("last_seen_at")
    age_seconds = _heartbeat_age_seconds(heartbeat_at)
    if (age_seconds is None or age_seconds > 180) and not body.allow_stale:
        raise HTTPException(
            status_code=409,
            detail="agent heartbeat is stale; wait for a fresh heartbeat before repair",
        )
    vmid = latest.get("vmid") or device.get("vmid")
    if vmid is None:
        raise HTTPException(status_code=409, detail="agent heartbeat does not include a VMID")
    node = web_app._resolve_vm_node(int(vmid))
    server_url = (body.server_url or _base_url(None)).strip().rstrip("/")
    if server_url and not server_url.startswith(("http://", "https://")):
        server_url = f"http://{server_url}:5000"
    if not server_url:
        raise HTTPException(status_code=409, detail="controller URL is required for agent repair")
    agent_url = f"{server_url}/api/setup/v1/agent-seed/{runtime_identifier}/AutopilotAgent.exe"
    capabilities_literal = "@(" + ",".join(
        web_app._ps_quote(item) for item in _BUILD_HOST_AGENT_CAPABILITIES
    ) + ")"
    upgrade_literal = "$true" if body.upgrade_agent else "$false"
    ps_quote = web_app._ps_quote
    ps = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$configPath = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\\AutopilotAgent\\agent.json'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configPath) | Out-Null
if (Test-Path -LiteralPath $configPath) {{
  $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
}} else {{
  $config = [pscustomobject]@{{}}
}}
$capabilities = {capabilities_literal}
$config | Add-Member -NotePropertyName serverUrl -NotePropertyValue {ps_quote(server_url)} -Force
$config | Add-Member -NotePropertyName phase -NotePropertyValue 'build-host' -Force
$config | Add-Member -NotePropertyName role -NotePropertyValue 'build-host' -Force
$config | Add-Member -NotePropertyName vmid -NotePropertyValue {int(vmid)} -Force
$config | Add-Member -NotePropertyName agentId -NotePropertyValue {ps_quote(agent_id)} -Force
$config | Add-Member -NotePropertyName capabilities -NotePropertyValue $capabilities -Force
$config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding UTF8
$service = Get-CimInstance Win32_Service -Filter "Name='AutopilotAgent'" -ErrorAction SilentlyContinue
$exe = 'C:\\Program Files\\ProxmoxVEAutopilot\\AutopilotAgent\\AutopilotAgent.exe'
if (-not (Test-Path -LiteralPath $exe) -and $service -and $service.PathName) {{
  $raw = $service.PathName.Trim()
  if ($raw.StartsWith('"')) {{ $exe = ($raw -replace '^"([^"]+)".*$', '$1') }}
}}
$programDataExe = Join-Path (Split-Path -Parent $configPath) 'AutopilotAgent.exe'
$targetExePaths = @($exe, $programDataExe)
$targetExePaths = $targetExePaths | Where-Object {{
  $_ -and (Test-Path -LiteralPath (Split-Path -Parent $_))
}} | Select-Object -Unique
if ({upgrade_literal} -and $targetExePaths.Count -gt 0) {{
  Stop-Service -Name AutopilotAgent -Force -ErrorAction SilentlyContinue
  Stop-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent' -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
  Get-Process -Name AutopilotAgent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  $tmp = "$programDataExe.new"
  Invoke-WebRequest -UseBasicParsing -Uri {ps_quote(agent_url)} -OutFile $tmp
  if ((Get-Item -LiteralPath $tmp).Length -lt 1048576) {{ throw 'downloaded seed agent is unexpectedly small' }}
  foreach ($targetExe in $targetExePaths) {{
    Copy-Item -LiteralPath $tmp -Destination $targetExe -Force
  }}
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}}
$service = Get-CimInstance Win32_Service -Filter "Name='AutopilotAgent'" -ErrorAction SilentlyContinue
if ($service) {{
  Start-Service -Name AutopilotAgent
}} elseif (Get-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent' -ErrorAction SilentlyContinue) {{
  Start-ScheduledTask -TaskName 'ProxmoxVEAutopilotBuildHostAgent'
}} else {{
  throw 'AutopilotAgent service or scheduled task was not found'
}}
[pscustomobject]@{{
  ok = $true
  vmid = {int(vmid)}
  agentId = {ps_quote(agent_id)}
  serverUrl = {ps_quote(server_url)}
  upgraded = [bool]{upgrade_literal}
}} | ConvertTo-Json -Compress
""".strip()
    status = web_app._guest_exec_ps_status(str(node), int(vmid), ps, timeout_s=300)
    out = str(status.get("out") or "").strip()
    if not status.get("ok"):
        detail = str(status.get("error") or "OSDeploy build-host agent repair failed")
        err = str(status.get("err") or "").strip()
        if err and err not in detail:
            detail = f"{detail}: {err[:400]}"
        raise HTTPException(status_code=502, detail=detail[:800])
    if not out:
        raise HTTPException(status_code=502, detail="OSDeploy build-host agent repair did not return success")
    try:
        result = json.loads(out)
    except Exception:
        result = {"raw": out}
    return {
        "ok": True,
        "agent_id": agent_id,
        "vmid": int(vmid),
        "node": node,
        "result": result,
    }


def preflight_payload(body: RunCreateBody) -> dict:
    from web import app as web_app

    blocking = []
    warnings = []
    cfg = web_app._load_vars()
    with _conn() as conn:
        artifact = osdeploy_pg.get_artifact(conn, body.artifact_id)
        cache_payload = osdeploy_cache.payload(conn)
        try:
            from web import sequences_pg

            credential_ids = {
                int(item["id"])
                for item in sequences_pg.list_credentials(conn)
            }
        except Exception:
            credential_ids = set()
        bubble_gate = None
        if body.bubble_id:
            from web import lab_bubbles_pg

            lab_bubbles_pg.init(conn)
            sanitized_role_options = osdeploy_roles.sanitize_role_options(
                body.server_role,
                body.role_options,
            )
            bootstrap_role = _is_bubble_bootstrap_role(body.server_role)
            requires_domain_join = (
                _requires_bubble_domain_readiness(body.server_role, sanitized_role_options)
                and not bootstrap_role
            )
            bubbles = lab_bubbles_pg.list_bubbles(conn)
            domain_names = {
                str(bubble.get("domain_name") or "").strip().lower()
                for bubble in bubbles
                if str(bubble.get("domain_name") or "").strip()
            }
            bubble_gate = lab_bubbles_pg.evaluate_launch_gate(
                conn,
                body.bubble_id,
                requires_domain_join=requires_domain_join,
                requires_configmgr=False,
                is_multi_bubble_context=(len(bubbles) > 1 and not bootstrap_role),
                is_multi_domain_context=(len(domain_names) > 1 and not bootstrap_role),
            )
    if not artifact:
        blocking.append(_blocking_check("artifact_missing", "OSDeploy artifact was not found."))
    else:
        artifact = enrich_artifact(artifact)
        if not artifact["ready"]:
            blocking.append(_blocking_check("artifact_not_published", "OSDeploy artifact is not published to Proxmox ISO storage."))
        if artifact["architecture"] != body.architecture:
            blocking.append(_blocking_check("artifact_architecture_mismatch", "Artifact architecture does not match the requested VM."))
        if artifact.get("os_version") != body.os_version or artifact.get("os_edition") != body.os_edition:
            blocking.append(_blocking_check(
                "artifact_os_mismatch",
                "Artifact OS metadata does not match the requested OS version and edition.",
            ))
    if body.vm_memory_mb < osdeploy_pg.MIN_VM_MEMORY_MB:
        blocking.append(_blocking_check("memory_too_small", f"OSDeploy Server VMs need at least {osdeploy_pg.MIN_VM_MEMORY_MB} MB RAM."))
    if body.vm_disk_size_gb < osdeploy_pg.MIN_VM_DISK_SIZE_GB:
        blocking.append(_blocking_check("disk_too_small", f"OSDeploy Server VMs need at least {osdeploy_pg.MIN_VM_DISK_SIZE_GB} GB disk."))
    role_checks = osdeploy_roles.validate_role_options(
        body.server_role,
        body.role_options,
        credential_exists=(lambda cred_id: int(cred_id) in credential_ids),
    )
    blocking.extend(role_checks)
    selected_iso_storage = body.iso_storage or cfg.get("proxmox_iso_storage", "local")
    selected_disk_storage = body.storage or cfg.get("proxmox_storage", "local-lvm")
    storage_names = _storage_names_by_content(web_app)
    if storage_names is not None:
        if selected_iso_storage not in storage_names["iso"]:
            blocking.append(_blocking_check(
                "iso_storage_missing",
                f"ISO storage '{selected_iso_storage}' is not available on this Proxmox node.",
            ))
        if selected_disk_storage not in storage_names["images"]:
            available = ", ".join(sorted(storage_names["images"])) or "none"
            blocking.append(_blocking_check(
                "disk_storage_missing",
                (
                    f"VM disk storage '{selected_disk_storage}' is not available "
                    f"for images. Available image storage: {available}."
                ),
            ))
    selected_node = body.node or cfg.get("proxmox_node") or ""
    virtio_iso = _resolve_virtio_iso_volid(web_app, cfg, str(selected_node))
    if not virtio_iso:
        blocking.append(_blocking_check("virtio_iso_missing", "OSDeploy requires a configured VirtIO driver ISO."))
    else:
        configured_virtio = cfg.get("proxmox_virtio_iso") or cfg.get("virtio_iso")
        if configured_virtio and configured_virtio != virtio_iso:
            warnings.append(_warning_check(
                "virtio_iso_recovered",
                f"Configured VirtIO ISO {configured_virtio} is stale; using discovered {virtio_iso}.",
            ))
    if (cache_payload.get("summary") or {}).get("ready", 0) == 0:
        warnings.append(_warning_check("cache_empty", "No ready OSDeploy cache entries are available."))
    if bubble_gate:
        message = "; ".join(bubble_gate["reasons"])
        if not bubble_gate["allowed"]:
            blocking.append(_blocking_check("bubble_not_ready", message))
        elif bubble_gate["state"] == "warning":
            warnings.append(_warning_check("bubble_warning", message))
    return {
        "schema_version": 1,
        "launch_allowed": not blocking,
        "blocking_checks": blocking,
        "warnings": warnings,
        "artifact": artifact,
        "cache": cache_payload,
        "role": osdeploy_roles.catalog_payload().get(body.server_role),
        "target": {
            "vm_name": body.vm_name,
            "computer_name": osdeploy_pg.normalize_windows_computer_name(body.vm_name),
            "server_role": body.server_role,
            "os": f"{body.os_version} {body.os_edition} {body.os_language}",
        },
    }


def _requires_bubble_domain_readiness(server_role: str, role_options: dict) -> bool:
    domain_join = role_options.get("domain_join")
    if isinstance(domain_join, dict) and domain_join.get("enabled"):
        return True
    return False


def _is_bubble_bootstrap_role(server_role: str) -> bool:
    return server_role in {"isolated_domain_controller", "lab_in_a_box"}


def _bubble_asset_role(server_role: str, explicit_role: str | None = None) -> str:
    if explicit_role:
        return explicit_role
    if server_role == "isolated_domain_controller":
        return "domain_controller"
    if server_role == "mecm_prereq":
        return "configmgr"
    return server_role or "server"


def provision_progress_summary(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "deployed": sum(1 for row in rows if row.get("state") == "complete"),
        "heartbeat": sum(1 for row in rows if row.get("first_heartbeat_at")),
        "failed": sum(1 for row in rows if row.get("state") == "failed"),
    }


def provision_progress_payload(*, limit: int = 50, include_archived: bool = False) -> dict:
    with _conn() as conn:
        runs = osdeploy_pg.list_runs(conn, limit=limit, include_archived=include_archived)
    return {
        "schema_version": 1,
        "summary": provision_progress_summary(runs),
        "runs": runs,
    }


@router.get("/catalog")
def catalog():
    return catalog_payload()


@router.get("/proxmox/options")
def proxmox_options():
    return proxmox_options_payload()


@router.get("/artifacts")
def list_artifacts(architecture: Optional[str] = None):
    with _conn() as conn:
        artifacts = [
            enrich_artifact(artifact)
            for artifact in osdeploy_pg.list_artifacts(conn, architecture=architecture)
        ]
    return {"schema_version": 1, "artifacts": artifacts}


@router.get("/artifacts/{artifact_id}/status")
def artifact_status(artifact_id: str):
    with _conn() as conn:
        artifact = osdeploy_pg.get_artifact(conn, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="OSDeploy artifact not found")
    artifact = enrich_artifact(artifact)
    return {
        "schema_version": 1,
        "artifact": artifact,
        "build_job": _job_evidence(artifact.get("build_job_id")),
        "publish_job": _job_evidence(artifact.get("publish_job_id")),
    }


@router.post("/artifacts/build", status_code=202)
def build_artifact(body: ArtifactBuildBody):
    from web import app as web_app
    from web import jobs_pg

    build_preflight = build_preflight_payload(body)
    if build_preflight["blocking_checks"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "OSDeploy artifact build preflight checks failed",
                "blocking_checks": build_preflight["blocking_checks"],
            },
        )
    if build_preflight["target"].get("selected_build_mode") == "build_host_agent":
        return _queue_build_host_agent_build(body, build_preflight)
    resolved = _resolved_build_request(body)
    job_id = web_app.job_manager._generate_id()
    log_path = Path(web_app.job_manager.jobs_dir) / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    script = _APP_ROOT / "scripts" / "osdeploy_remote_build.py"
    output_dir = _APP_ROOT / "output" / "osdeploy"
    cmd = [
        sys.executable,
        str(script),
        "--job-id",
        job_id,
        "--remote",
        resolved["remote"],
        "--remote-root",
        resolved["remote_root"],
        "--repo-root",
        str(_OSDEPLOY_SOURCE_ROOT),
        "--output-dir",
        str(output_dir),
        "--arch",
        resolved["architecture"],
        "--osdeploy-version",
        resolved["osdeploy_module_version"],
        "--osdbuilder-version",
        resolved["osdbuilder_module_version"],
        "--adk-version",
        resolved["adk_version"],
        "--source-media-path",
        resolved["source_media_path"],
        "--image-name",
        resolved["image_name"],
        "--image-index",
        str(resolved["image_index"]),
        "--os-version",
        resolved["os_version"],
        "--os-edition",
        resolved["os_edition"],
        "--os-language",
        resolved["os_language"],
        "--controller-url",
        _base_url(None),
    ]
    if resolved.get("ssh_key_path"):
        cmd.extend(["--ssh-key", resolved["ssh_key_path"]])
    jobs_pg.enqueue(
        job_id=job_id,
        job_type="osdeploy_build_iso",
        playbook="osdeploy_remote_build",
        cmd=cmd,
        args=resolved,
    )
    return {"ok": True, "job_id": job_id, "job_type": "osdeploy_build_iso"}


@router.post("/artifacts/build/preflight")
def build_preflight(body: ArtifactBuildBody):
    return build_preflight_payload(body)


@router.get("/artifacts/build/defaults")
def build_defaults():
    return build_defaults_payload()


@router.post("/artifacts/{artifact_id}/publish", status_code=202)
def publish_artifact(artifact_id: str, body: ArtifactPublishBody):
    from web import app as web_app
    from web import jobs_pg

    options = proxmox_options_payload()
    node = body.node or options["defaults"]["node"]
    storage = body.storage or options["defaults"]["iso_storage"]
    if node not in options["nodes"]:
        raise HTTPException(status_code=400, detail=f"Proxmox node is unavailable: {node}")
    if storage not in options["storages"]["iso"]:
        raise HTTPException(status_code=400, detail=f"ISO storage is unavailable: {storage}")
    with _conn() as conn:
        artifact = osdeploy_pg.get_artifact(conn, artifact_id)
        if not artifact:
            raise HTTPException(status_code=404, detail="OSDeploy artifact not found")
    iso_path = Path(artifact["iso_path"])
    if not iso_path.is_file():
        raise HTTPException(
            status_code=409,
            detail="OSDeploy artifact ISO is missing from local output storage.",
        )
    target_volid = f"{storage}:iso/{iso_path.name}"
    job_id = web_app.job_manager._generate_id()
    script = _APP_ROOT / "scripts" / "osdeploy_publish_job.py"
    cmd = [
        sys.executable,
        str(script),
        "publish",
        "--job-id",
        job_id,
        "--artifact-id",
        artifact_id,
        "--node",
        node,
        "--storage",
        storage,
    ]
    jobs_pg.enqueue(
        job_id=job_id,
        job_type="osdeploy_publish_iso",
        playbook="osdeploy_publish_iso",
        cmd=cmd,
        args={
            "artifact_id": artifact_id,
            "node": node,
            "storage": storage,
            "iso_path": str(iso_path),
            "target_volid": target_volid,
        },
    )
    with _conn() as conn:
        artifact = osdeploy_pg.update_artifact_publish_job(
            conn,
            artifact_id=artifact_id,
            publish_job_id=job_id,
        )
    return {
        "ok": True,
        "job_id": job_id,
        "job_type": "osdeploy_publish_iso",
        "target_volid": target_volid,
        "artifact": enrich_artifact(artifact),
    }


def _queue_cache_job(job_type: str, action: str, args: dict) -> dict:
    from web import app as web_app

    script = _APP_ROOT / "scripts" / "osdeploy_cache_job.py"
    cmd = [sys.executable, str(script), action]
    if args.get("entry_id"):
        cmd.extend(["--entry-id", str(args["entry_id"])])
    job = web_app.job_manager.start(job_type, cmd, args=args)
    return {"ok": True, "job_id": job["id"], "job_type": job_type}


@router.get("/cache")
def cache_status():
    with _conn() as conn:
        return osdeploy_cache.payload(conn)


@router.post("/cache/catalog/refresh", status_code=202)
def refresh_cache_catalog():
    return _queue_cache_job("osdeploy_cache_refresh_catalog", "refresh", {})


def _entry_is_factory_built(entry: dict) -> bool:
    """A server_image whose source is a manual:// placeholder built by OSDeploy/OSDBuilder.

    These cannot be fetched over HTTP; warming them runs the factory on the build host
    via the agent instead of doing a urllib download.
    """
    if str(entry.get("entry_type") or "") != "server_image":
        return False
    if not str(entry.get("source_url") or "").startswith("manual://"):
        return False
    factory = str((entry.get("metadata") or {}).get("factory") or "").lower()
    return "osdbuilder" in factory or "osdeploy" in factory


def _image_index_for_edition(edition: str) -> int:
    # Windows Server install.wim "Desktop Experience" image indexes.
    if (edition or "").strip().lower().startswith("standard"):
        return 2
    return 4


def _warm_factory_entry_via_agent(entry: dict) -> dict:
    windows_version = str(entry.get("windows_version") or "").strip()
    edition = str(entry.get("edition") or osdeploy_pg.DEFAULT_OS_EDITION).strip()
    body = ArtifactBuildBody(
        build_mode="build_host_agent",
        architecture=str(entry.get("architecture") or "amd64"),
        image_name=f"{windows_version} {edition}".strip(),
        image_index=_image_index_for_edition(edition),
        os_version=windows_version,
        os_edition=edition,
        os_language=str(entry.get("language") or osdeploy_pg.DEFAULT_OS_LANGUAGE),
        # Left empty: the factory resolves the staged base media from
        # C:\BuildRoot\ProxmoxVEAutopilot\inputs\media on the build host.
        source_media_path="",
    )
    dispatched = build_artifact(body)
    work_item_id = dispatched.get("work_item_id")
    if work_item_id:
        with _conn() as conn:
            osdeploy_cache.init(conn)
            osdeploy_cache.mark_build_dispatched(conn, entry["id"], work_item_id=work_item_id)
    return {
        "ok": True,
        "job_type": "osdeploy_cache_warm_build",
        "entry_id": entry["id"],
        **dispatched,
    }


@router.post("/cache/{entry_id}/warm", status_code=202)
def warm_cache_entry(entry_id: str):
    with _conn() as conn:
        osdeploy_cache.init(conn)
        entry = osdeploy_cache.get_entry(conn, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"OSDeploy cache entry not found: {entry_id}")
    if _entry_is_factory_built(entry):
        return _warm_factory_entry_via_agent(entry)
    return _queue_cache_job("osdeploy_cache_warm", "warm", {"entry_id": entry_id})


@router.post("/cache/{entry_id}/verify", status_code=202)
def verify_cache_entry(entry_id: str):
    return _queue_cache_job("osdeploy_cache_verify", "verify", {"entry_id": entry_id})


@router.post("/cache/{entry_id}/delete", status_code=202)
def delete_cache_entry(entry_id: str):
    return _queue_cache_job("osdeploy_cache_delete", "delete", {"entry_id": entry_id})


@router.head(
    "/cache/{entry_id}/download/{file_name:path}",
    operation_id="head_osdeploy_cache_entry_download",
)
@router.get(
    "/cache/{entry_id}/download/{file_name:path}",
    operation_id="get_osdeploy_cache_entry_download",
)
def download_cache_entry(entry_id: str, file_name: str, request: Request):
    with _conn() as conn:
        entry = osdeploy_cache.get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="OSDeploy cache entry not found")
        if entry["status"] != "ready":
            raise HTTPException(status_code=409, detail="OSDeploy cache entry is not ready")
        path = Path(entry.get("local_path") or "")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="OSDeploy cache file is missing")
        if Path(file_name).name != entry["file_name"]:
            raise HTTPException(status_code=404, detail="OSDeploy cache filename mismatch")
        headers = {
            "Content-Length": str(path.stat().st_size),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
        }
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers, media_type="application/octet-stream")
        osdeploy_cache.mark_served(conn, entry_id)
    return FileResponse(path, media_type="application/octet-stream", filename=entry["file_name"], headers=headers)


@router.post("/preflight")
def preflight(body: RunCreateBody):
    return preflight_payload(body)


@router.post("/runs", status_code=201)
def create_run(body: RunCreateBody):
    if body.bubble_id:
        with _conn() as conn:
            from web import lab_bubbles_pg

            lab_bubbles_pg.init(conn)
            if not lab_bubbles_pg.get_bubble(conn, body.bubble_id):
                raise HTTPException(status_code=404, detail="Bubble not found")
    preflight = preflight_payload(body)
    if preflight["blocking_checks"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "OSDeploy blocking preflight checks failed",
                "blocking_checks": preflight["blocking_checks"],
            },
        )
    with _conn() as conn:
        try:
            run = osdeploy_pg.create_run(
                conn,
                artifact_id=body.artifact_id,
                vm_name=body.vm_name,
                node=body.node,
                iso_storage=body.iso_storage,
                storage=body.storage,
                network_bridge=body.network_bridge,
                requested_vmid=body.vmid,
                architecture=body.architecture,
                server_role=body.server_role,
                os_version=body.os_version,
                os_edition=body.os_edition,
                os_language=body.os_language,
                vm_cores=body.vm_cores,
                vm_memory_mb=body.vm_memory_mb,
                vm_disk_size_gb=body.vm_disk_size_gb,
                secure_boot=body.secure_boot,
                outbound_policy=body.outbound_policy,
                role_options=body.role_options,
            )
            if body.bubble_id:
                lab_bubbles_pg.add_asset(
                    conn,
                    body.bubble_id,
                    asset_type="vm",
                    asset_role=_bubble_asset_role(body.server_role, body.asset_role),
                    vmid=run.get("vmid") or run.get("requested_vmid"),
                    vm_uuid=run.get("vm_uuid"),
                    run_id=run["run_id"],
                    membership_state="provisioning",
                    evidence_state="run_created",
                    notes=f"OSDeploy run {run['workflow_name']}",
                    actor="osdeploy",
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "run": run}


@router.post("/bundles", status_code=201)
def create_bundle(body: RunCreateBody):
    if body.server_role != "lab_in_a_box":
        raise HTTPException(status_code=400, detail="Only lab_in_a_box bundles are supported.")
    if body.bubble_id:
        with _conn() as conn:
            from web import lab_bubbles_pg

            lab_bubbles_pg.init(conn)
            if not lab_bubbles_pg.get_bubble(conn, body.bubble_id):
                raise HTTPException(status_code=404, detail="Bubble not found")
    preflight = preflight_payload(body)
    if preflight["blocking_checks"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "OSDeploy bundle blocking preflight checks failed",
                "blocking_checks": preflight["blocking_checks"],
            },
        )
    with _conn() as conn:
        try:
            created = osdeploy_pg.create_lab_bundle(
                conn,
                artifact_id=body.artifact_id,
                vm_name=body.vm_name,
                node=body.node,
                iso_storage=body.iso_storage,
                storage=body.storage,
                network_bridge=body.network_bridge,
                architecture=body.architecture,
                os_version=body.os_version,
                os_edition=body.os_edition,
                os_language=body.os_language,
                vm_cores=body.vm_cores,
                vm_memory_mb=body.vm_memory_mb,
                vm_disk_size_gb=body.vm_disk_size_gb,
                secure_boot=body.secure_boot,
                outbound_policy=body.outbound_policy,
                role_options=body.role_options,
            )
            if body.bubble_id:
                from web import lab_bubbles_pg

                lab_bubbles_pg.init(conn)
                for child in created["children"]:
                    run = child["run"]
                    lab_bubbles_pg.add_asset(
                        conn,
                        body.bubble_id,
                        asset_type="vm",
                        asset_role=_bubble_asset_role(child["server_role"]),
                        vmid=run.get("vmid") or run.get("requested_vmid"),
                        vm_uuid=run.get("vm_uuid"),
                        run_id=run["run_id"],
                        membership_state="provisioning",
                        evidence_state="run_created",
                        notes=f"OSDeploy bundle {created['bundle']['id']}",
                        actor="osdeploy",
                    )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **created}


@router.get("/runs")
def list_runs(limit: int = 100, include_archived: bool = False):
    with _conn() as conn:
        runs = osdeploy_pg.list_runs(conn, limit=limit, include_archived=include_archived)
    return {"schema_version": 1, "runs": runs}


@router.get("/progress")
def progress(limit: int = 50, include_archived: bool = False):
    return provision_progress_payload(limit=limit, include_archived=include_archived)


@router.get("/runs/{run_id}")
def run_detail(run_id: str):
    with _conn() as conn:
        run = osdeploy_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        artifact = osdeploy_pg.get_artifact(conn, run["artifact_id"])
        events = osdeploy_pg.list_events(conn, run_id)
        readiness = osdeploy_pg.get_readiness(conn, run_id)
        steps = ts_engine_pg.list_run_steps(conn, run_id)
    return {
        "schema_version": 1,
        "run": run,
        "artifact": enrich_artifact(artifact),
        "events": events,
        "readiness": readiness,
        "v2_steps": steps,
    }


@router.post("/runs/{run_id}/provision", status_code=202)
def provision_run(run_id: str, request: Request):
    from web import app as web_app

    with _conn() as conn:
        run = osdeploy_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        artifact = osdeploy_pg.get_artifact(conn, run["artifact_id"])
        if not artifact:
            raise HTTPException(status_code=404, detail="OSDeploy artifact not found")
        if not artifact.get("proxmox_volid"):
            raise HTTPException(
                status_code=409,
                detail="OSDeploy artifact is not uploaded to Proxmox ISO storage",
            )
    extra_vars = osdeploy_provision_extra_vars(run=run, artifact=artifact, request=request)
    cmd = [
        "ansible-playbook",
        str(_APP_ROOT / "playbooks" / "provision_proxmox_osdeploy.yml"),
    ]
    for key, value in extra_vars.items():
        cmd.extend(["-e", f"{key}={value}"])
    job = web_app.job_manager.start("provision_osdeploy", cmd, args=extra_vars)
    return {"ok": True, "job_id": job["id"]}


@router.post("/runs/{run_id}/identity")
def record_identity(run_id: str, body: IdentityBody):
    with _conn() as conn:
        run = osdeploy_pg.set_run_identity(
            conn,
            run_id=run_id,
            vmid=body.vmid,
            vm_uuid=body.vm_uuid,
            mac=body.mac,
            node=body.node,
            computer_name=body.computer_name,
        )
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
    return {"ok": True, "run": run}


@router.post("/runs/{run_id}/pe/register")
def register_pe(run_id: str, body: PeRegisterBody):
    metadata = dict(body.metadata or {})
    if body.client_version:
        metadata["client_version"] = body.client_version
    with _conn() as conn:
        run = osdeploy_pg.mark_pe_registered(conn, run_id=run_id, metadata=metadata)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
    return {
        "schema_version": 1,
        "ok": True,
        "run": run,
        "run_id": run["run_id"],
        "workflow_name": run["workflow_name"],
        "bearer_token": _sign(run["run_id"], ttl_seconds=_PE_TOKEN_TTL_SECONDS),
        "package_url": f"/api/osdeploy/v1/pe/package/{run['run_id']}",
        "state": run["state"],
    }


@router.post("/pe/register")
def register_pe_by_identity(body: PeIdentityRegisterBody):
    metadata = dict(body.metadata or {})
    metadata.update({
        "vm_uuid": body.vm_uuid,
        "mac": body.mac,
        "architecture": body.architecture,
        "build_sha": body.build_sha,
    })
    if body.client_version:
        metadata["client_version"] = body.client_version
    with _conn() as conn:
        run = osdeploy_pg.find_run_by_identity(
            conn,
            vm_uuid=body.vm_uuid,
            mac=body.mac,
            architecture=body.architecture,
            build_sha=body.build_sha,
        )
        if not run:
            raise HTTPException(status_code=404, detail="matching OSDeploy run not found")
        run = osdeploy_pg.mark_pe_registered(
            conn,
            run_id=run["run_id"],
            metadata=metadata,
        )
    return {
        "schema_version": 1,
        "ok": True,
        "run": run,
        "run_id": run["run_id"],
        "workflow_name": run["workflow_name"],
        "bearer_token": _sign(run["run_id"], ttl_seconds=_PE_TOKEN_TTL_SECONDS),
        "package_url": f"/api/osdeploy/v1/pe/package/{run['run_id']}",
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
        run = osdeploy_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        artifact = osdeploy_pg.get_artifact(conn, run["artifact_id"])
        if not artifact:
            raise HTTPException(status_code=404, detail="OSDeploy artifact not found")
    return _package_response(run=run, artifact=artifact, server_base_url=_base_url(request))


@router.post("/runs/{run_id}/events")
def append_event(
    run_id: str,
    body: EventBody,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(run_id, payload)
    with _conn() as conn:
        if not osdeploy_pg.get_run(conn, run_id):
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        event = osdeploy_pg.append_event(
            conn,
            run_id=run_id,
            phase=body.phase,
            event_type=body.event_type,
            severity=body.severity,
            message=body.message,
            data=body.data,
        )
    return {"ok": True, "event": event}


@router.post("/runs/{run_id}/archive")
def archive_run(run_id: str, reason: str = ""):
    with _conn() as conn:
        run = osdeploy_pg.archive_run(conn, run_id, reason=reason)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        osdeploy_pg.append_event(
            conn,
            run_id=run_id,
            phase="controller",
            event_type="run_archived",
            message="OSDeploy run hidden from default history",
            data={"reason": reason},
        )
    return {"ok": True, "run": run}


@router.post("/runs/{run_id}/unarchive")
def unarchive_run(run_id: str):
    with _conn() as conn:
        run = osdeploy_pg.unarchive_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="OSDeploy run not found")
        osdeploy_pg.append_event(
            conn,
            run_id=run_id,
            phase="controller",
            event_type="run_unarchived",
            message="OSDeploy run restored to default history",
        )
    return {"ok": True, "run": run}


@router.post("/runs/archive-stale-failed")
def archive_stale_failed_runs(older_than_hours: int = 12):
    with _conn() as conn:
        runs = osdeploy_pg.archive_runs_by_filter(
            conn,
            state="failed",
            older_than_hours=older_than_hours,
            reason="stale failed OSDeploy run",
        )
    return {"ok": True, "archived_count": len(runs), "runs": runs}


@router.post("/runs/archive-completed-old")
def archive_completed_old_runs(older_than_hours: int = 24):
    with _conn() as conn:
        runs = osdeploy_pg.archive_runs_by_filter(
            conn,
            state="complete",
            older_than_hours=older_than_hours,
            reason="completed OSDeploy run hidden from default history",
        )
    return {"ok": True, "archived_count": len(runs), "runs": runs}
