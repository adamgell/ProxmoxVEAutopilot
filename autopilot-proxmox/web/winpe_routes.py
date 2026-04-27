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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from web.artifact_store import ArtifactStore
from web.winpe_checkin_db import Checkin, WinpeCheckinDb
from web.winpe_manifest_renderer import render_manifest, RendererError
from web.winpe_targets_db import UnknownVmError, WinpeTargetsDb


class _CheckinIn(BaseModel):
    vmUuid: str
    stepId: str
    status: str = Field(pattern=r"^(starting|ok|error)$")
    timestamp: str
    durationSec: float = 0.0
    logTail: str = ""
    errorMessage: str | None = None
    extra: dict = Field(default_factory=dict)


class _HwidIn(BaseModel):
    vmUuid: str
    serial: str = ""
    hardwareHash: str = ""
    manufacturer: str = ""
    model: str = ""
    timestamp: str = ""


router = APIRouter(prefix="/winpe", tags=["winpe"])


@router.get("/deploy", response_class=HTMLResponse)
def deploy_dashboard() -> HTMLResponse:
    """Dev dashboard for monitoring deployments."""
    html_path = Path(__file__).parent / "static" / "deploy-dashboard.html"
    return HTMLResponse(content=html_path.read_text())


def _artifact_root() -> Path:
    """Default artifact-store root: <repo-root>/var/artifacts.

    autopilot-proxmox is run from itself as the working directory in tests
    and Docker; var/artifacts lives one level up at the repo root.
    """
    return Path.cwd().parent / "var" / "artifacts"


@router.get("/manifest/{vm_uuid}")
def get_manifest(vm_uuid: str) -> JSONResponse:
    """Render the per-VM task manifest. 404 if unknown; 503 if install.wim missing."""
    root = _artifact_root()
    db = WinpeTargetsDb(root / "index.db")
    target = db.lookup(vm_uuid)
    if target is None:
        raise HTTPException(status_code=404, detail=f"unknown vm_uuid: {vm_uuid}")

    store = ArtifactStore(root)
    try:
        manifest = render_manifest(target, store)
    except RendererError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        db.touch_last_manifest_at(vm_uuid)
    except UnknownVmError:
        # Race: target was deleted between lookup and touch. Manifest is still valid.
        pass

    return JSONResponse(content=manifest)


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


@router.post("/checkin", status_code=204, response_model=None, response_class=Response)
def post_checkin(payload: _CheckinIn) -> None:
    root = _artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    db = WinpeCheckinDb(root / "checkins.db")
    db.record(Checkin(
        vm_uuid=payload.vmUuid,
        step_id=payload.stepId,
        status=payload.status,
        timestamp=payload.timestamp,
        duration_sec=payload.durationSec,
        log_tail=payload.logTail,
        error_message=payload.errorMessage,
        extra=payload.extra,
    ))
    return None


@router.get("/dashboard/events/{vm_uuid}")
def get_dashboard_events(vm_uuid: str, since: str = "") -> JSONResponse:
    """Recent checkins for the dev dashboard. Polls every 1s from the UI."""
    import sqlite3
    root = _artifact_root()
    db_path = root / "checkins.db"
    if not db_path.exists():
        return JSONResponse(content={"events": [], "heartbeat": None})

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT step_id, status, timestamp, duration_sec, log_tail, error_message, extra_json FROM winpe_checkins WHERE vm_uuid = ?"
        params: list = [vm_uuid]
        if since:
            query += " AND timestamp > ?"
            params.append(since)
        query += " ORDER BY rowid DESC LIMIT 100"
        rows = conn.execute(query, params).fetchall()

    events = [dict(r) for r in reversed(rows)]
    heartbeat = None
    for e in reversed(events):
        if e["step_id"] == "heartbeat":
            heartbeat = e
    non_hb = [e for e in events if e["step_id"] != "heartbeat"]
    return JSONResponse(content={"events": non_hb, "heartbeat": heartbeat})


@router.get("/dashboard/targets")
def get_dashboard_targets() -> JSONResponse:
    """List all registered targets with their latest status."""
    import sqlite3, json
    root = _artifact_root()
    targets = []

    # Targets
    tdb = root / "index.db"
    if tdb.exists():
        db = WinpeTargetsDb(tdb)
        for uuid in db.list_uuids():
            t = db.lookup(uuid)
            if t:
                targets.append({"vmUuid": t.vm_uuid, "label": t.template_id, "params": t.params})

    # Hwid status
    hdb = root / "hwid.db"
    hwids = {}
    if hdb.exists():
        with sqlite3.connect(hdb) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT * FROM hwid").fetchall():
                hwids[row["vm_uuid"]] = dict(row)

    for t in targets:
        h = hwids.get(t["vmUuid"])
        t["hwid"] = {"collected": bool(h), "hashLen": len(h["hardware_hash"]) if h else 0} if h else None

    return JSONResponse(content={"targets": targets})


@router.post("/hwid", status_code=204, response_model=None, response_class=Response)
def post_hwid(payload: _HwidIn) -> None:
    """Receive Autopilot hardware hash from a freshly-deployed machine."""
    import json, sqlite3
    root = _artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "hwid.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS hwid (
            vm_uuid TEXT PRIMARY KEY,
            serial TEXT NOT NULL,
            hardware_hash TEXT NOT NULL,
            manufacturer TEXT NOT NULL,
            model TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )""")
        conn.execute(
            "INSERT OR REPLACE INTO hwid (vm_uuid, serial, hardware_hash, manufacturer, model, timestamp, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload.vmUuid, payload.serial, payload.hardwareHash,
             payload.manufacturer, payload.model, payload.timestamp,
             json.dumps(payload.model_dump())),
        )
    return None
