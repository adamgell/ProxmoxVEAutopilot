"""CloudOSD controller and WinPE bridge API."""
from __future__ import annotations

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


router = APIRouter(prefix="/api/cloudosd", tags=["cloudosd"])

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
    return {
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


def _related_jobs(run_id: str) -> list[dict]:
    try:
        from web import app as web_app

        return [
            job for job in web_app.job_manager.list_jobs()
            if (job.get("args") or {}).get("cloudosd_run_id") == run_id
        ]
    except Exception:
        return []


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
            )
        artifact = cloudosd_pg.get_artifact(conn, run["artifact_id"])
        events = cloudosd_pg.list_events(conn, run_id)
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
        "events": events,
        "event_groups": cloudosd_pg.milestone_event_groups(events),
        "milestone_labels": cloudosd_pg.CLOUDOSD_MILESTONE_LABELS,
        "related_jobs": _related_jobs(run_id),
        "os_settings": cloudosd_pg.os_settings(run),
        "user_settings": cloudosd_pg.user_settings(run),
        "task": cloudosd_pg.task_settings(run),
    }


@router.get("/runs/{run_id}/events")
def list_run_events(run_id: str):
    with _conn() as conn:
        run = cloudosd_pg.get_run(conn, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="CloudOSD run not found")
        events = cloudosd_pg.list_events(conn, run_id)
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
    }


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
    extra_vars = {
        "cloudosd_run_id": run_id,
        "cloudosd_artifact_volid": artifact["proxmox_volid"],
        "autopilot_base_url": _base_url(request),
        "proxmox_node": run["node"],
        "proxmox_storage": run["storage"],
        "proxmox_bridge": run["network_bridge"],
        "vm_cores": run["vm_cores"],
        "vm_memory_mb": run["vm_memory_mb"],
        "vm_disk_size_gb": run["vm_disk_size_gb"],
        "vm_name": run.get("requested_vm_name") or run["vm_name"],
        "vm_custom_serial": run.get("expected_computer_name") or run["vm_name"],
        "hostname_pattern": run.get("expected_computer_name") or run["vm_name"],
        "tpm_enabled": run["tpm_enabled"],
        "secure_boot": run["secure_boot"],
    }
    if run.get("requested_vmid"):
        extra_vars["requested_vmid"] = run["requested_vmid"]
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
    return {"schema_version": 1, "event": event}
