"""FastAPI router for the WinPE-driven OSD orchestrator API.

Three endpoints (spec Section 7):

    GET  /winpe/manifest/<smbios-uuid>   → per-VM manifest JSON
    GET  /winpe/content/<sha256>         → content-addressed blob
    POST /winpe/checkin                  → PE→orchestrator progress event

Auth: none (LAN-trusted). Content is sha-verified by PE-side after fetch.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from web.artifact_store import ArtifactStore


router = APIRouter(prefix="/winpe", tags=["winpe"])


def _artifact_root() -> Path:
    """Default artifact-store root: <repo-root>/var/artifacts.

    autopilot-proxmox is run from itself as the working directory in tests
    and Docker; var/artifacts lives one level up at the repo root.
    """
    return Path.cwd().parent / "var" / "artifacts"


@router.get("/content/{sha256}")
def get_content(sha256: str) -> FileResponse:
    """Stream a registered artifact (or cached per-VM blob) by sha256.

    Looks up the sha in the artifact-store index; if absent, 404. If present
    but the underlying file is gone, 410 Gone (corrupt store; operator must
    re-register the artifact).
    """
    store = ArtifactStore(_artifact_root())
    record = store.lookup(sha256)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown sha256: {sha256}")
    abs_path = store.root / record.relative_path
    if not abs_path.exists():
        raise HTTPException(status_code=410, detail=f"sha256 indexed but file missing: {record.relative_path}")
    return FileResponse(
        path=str(abs_path),
        media_type="application/octet-stream",
        filename=abs_path.name,
    )
