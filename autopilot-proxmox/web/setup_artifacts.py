"""First-run artifact registry for build-host produced files."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4


APP_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = Path(
    os.environ.get("AUTOPILOT_SETUP_ARTIFACT_ROOT")
    or APP_ROOT / "output" / "setup" / "artifacts"
)
REGISTRY_PATH = ARTIFACT_ROOT / "artifact_registry.json"
_LOCK = Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(value: str) -> str:
    out = "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_", "."))
    return out.strip("._") or "artifact"


def safe_artifact_path(kind: str, filename: str) -> Path:
    target_dir = ARTIFACT_ROOT / _safe_name(kind)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = _safe_name(Path(filename).name)
    target = target_dir / safe_filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(1, 10_000):
        candidate = target_dir / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"unable to allocate artifact filename for {filename}")


def _read_registry() -> dict:
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", 1)
    data.setdefault("artifacts", [])
    return data


def _write_registry(data: dict) -> None:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def register_artifact(
    *,
    kind: str,
    source_path: Path,
    metadata: dict[str, Any] | None = None,
    producer_agent_id: str = "",
    work_item_id: str = "",
) -> dict:
    safe_kind = _safe_name(kind)
    source_path = Path(source_path)
    target_dir = ARTIFACT_ROOT / safe_kind
    target_dir.mkdir(parents=True, exist_ok=True)
    target = safe_artifact_path(safe_kind, source_path.name)
    if source_path.resolve() != target.resolve():
        with source_path.open("rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    return register_existing_artifact(
        kind=safe_kind,
        path=target,
        metadata=metadata,
        producer_agent_id=producer_agent_id,
        work_item_id=work_item_id,
    )


def register_existing_artifact(
    *,
    kind: str,
    path: Path,
    metadata: dict[str, Any] | None = None,
    producer_agent_id: str = "",
    work_item_id: str = "",
) -> dict:
    safe_kind = _safe_name(kind)
    target = Path(path)
    row = {
        "artifact_id": str(uuid4()),
        "kind": safe_kind,
        "filename": target.name,
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "metadata": metadata or {},
        "producer_agent_id": producer_agent_id,
        "work_item_id": work_item_id,
        "created_at": _now(),
        "proxmox_volid": None,
        "promoted_at": None,
    }
    with _LOCK:
        data = _read_registry()
        data["artifacts"].append(row)
        _write_registry(data)
    return row


def list_artifacts(*, kind: str | None = None) -> list[dict]:
    with _LOCK:
        rows = list(_read_registry().get("artifacts") or [])
    if kind:
        rows = [row for row in rows if row.get("kind") == kind]
    return sorted(rows, key=lambda row: row.get("created_at") or "", reverse=True)


def _agent_release_rid(row: dict) -> str:
    metadata = row.get("metadata") or {}
    value = str(metadata.get("rid") or metadata.get("runtime_identifier") or "").strip()
    if value:
        return value
    filename = str(row.get("filename") or "").lower()
    if "arm64" in filename:
        return "win-arm64"
    return "win-x64"


def _agent_release_version(row: dict) -> str:
    metadata = row.get("metadata") or {}
    value = str(metadata.get("version") or metadata.get("agent_version") or "").strip()
    if value:
        return value
    filename = str(row.get("filename") or "")
    prefix = "AutopilotAgent-"
    if filename.startswith(prefix):
        remainder = filename[len(prefix):]
        for suffix in ("-win-x64.msi", "-win-arm64.msi", ".msi"):
            if remainder.endswith(suffix):
                return remainder[: -len(suffix)]
    return ""


def _looks_like_msi(path: Path, size_bytes: int) -> bool:
    if size_bytes < 1024:
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return False
    return header.startswith(b"MZ") or header == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def latest_agent_release(*, runtime_identifier: str = "win-x64") -> dict | None:
    candidates: list[dict] = []
    for row in list_artifacts(kind="agent-msi"):
        path = Path(row.get("path") or "")
        if not path.is_file():
            continue
        size_bytes = int(row.get("size_bytes") or path.stat().st_size)
        if not _looks_like_msi(path, size_bytes):
            continue
        rid = _agent_release_rid(row)
        if rid != runtime_identifier:
            continue
        release = dict(row)
        release["path"] = str(path)
        release["runtime_identifier"] = rid
        release["version"] = _agent_release_version(row)
        release["size_bytes"] = size_bytes
        release["sha256"] = row.get("sha256") or _sha256(path)
        candidates.append(release)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.get("created_at") or "")


def mark_promoted(artifact_id: str, *, proxmox_volid: str) -> dict | None:
    with _LOCK:
        data = _read_registry()
        for row in data.get("artifacts") or []:
            if row.get("artifact_id") == artifact_id:
                row["proxmox_volid"] = proxmox_volid
                row["promoted_at"] = _now()
                _write_registry(data)
                return dict(row)
    return None


def readiness_summary() -> dict:
    rows = list_artifacts()
    promoted = [row for row in rows if row.get("proxmox_volid")]
    kinds = {row.get("kind") for row in rows}
    promoted_kinds = {row.get("kind") for row in promoted}
    return {
        "ready": bool(promoted_kinds & {"winpe-iso", "cloudosd-iso", "osdeploy-iso"}),
        "agent_msi_ready": "agent-msi" in kinds,
        "iso_ready": bool(kinds & {"winpe-iso", "cloudosd-iso", "osdeploy-iso"}),
        "promoted_iso_ready": bool(promoted_kinds & {"winpe-iso", "cloudosd-iso", "osdeploy-iso"}),
        "count": len(rows),
        "promoted_count": len(promoted),
        "kinds": sorted(kind for kind in kinds if kind),
        "promoted_kinds": sorted(kind for kind in promoted_kinds if kind),
        "artifacts": rows[:20],
    }
