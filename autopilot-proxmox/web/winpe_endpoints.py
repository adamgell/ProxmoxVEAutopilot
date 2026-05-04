"""FastAPI router for the WinPE phase-0 agent.

All endpoints prefixed /winpe/. Routes:
  POST /winpe/run/<id>/identity        Ansible writes vmid + vm_uuid post-clone
  POST /winpe/register                 Agent registers, gets actions + token
  GET  /winpe/sequence/<run_id>        Re-fetch action list (idempotent)
  GET  /winpe/autopilot-config/<run_id>  Per-run JSON payload
  GET  /winpe/unattend/<run_id>        Per-run post_winpe unattend XML
  POST /winpe/step/<step_id>/result    Step state telemetry, refreshes token
  POST /winpe/done                     Detach ide2+sata0, mark awaiting_specialize

Token secret: AUTOPILOT_WINPE_TOKEN_SECRET env var.
Identity-endpoint client allowlist: AUTOPILOT_WINPE_IDENTITY_ALLOWLIST
(comma-separated hostnames or IPs; matched against request.client.host).
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from web import sequences_db, winpe_token


router = APIRouter(prefix="/winpe", tags=["winpe"])


class IdentityBody(BaseModel):
    vmid: int
    vm_uuid: str


def _identity_allowlist() -> set[str]:
    raw = os.environ.get("AUTOPILOT_WINPE_IDENTITY_ALLOWLIST", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _check_identity_caller(request: Request) -> None:
    allow = _identity_allowlist()
    if not allow:
        return
    client = request.client.host if request.client else ""
    if client not in allow:
        raise HTTPException(status_code=403, detail="caller not allowed")


def _db_path() -> str:
    from web import app as web_app
    return web_app.SEQUENCES_DB


@router.post("/run/{run_id}/identity")
def post_identity(run_id: int, body: IdentityBody, request: Request):
    _check_identity_caller(request)
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["state"] == "queued":
        sequences_db.set_provisioning_run_identity(
            db, run_id=run_id, vmid=body.vmid, vm_uuid=body.vm_uuid,
        )
    # Already past queued: idempotent no-op (rerun-safe from Ansible)
    return {"ok": True}


class RegisterBody(BaseModel):
    vm_uuid: str
    mac: str
    build_sha: str


_REGISTER_TOKEN_TTL = 60 * 60  # 60 minutes


def _build_actions_for_run(db: str, run_id: int, sequence_id: int) -> list[dict]:
    """Compile WinPE actions, persist as pending steps, return action dicts
    augmented with the assigned step_id."""
    from web import sequence_compiler
    seq = sequences_db.get_sequence(db, sequence_id)
    phase = sequence_compiler.compile_winpe(seq)
    out = []
    for action in phase.actions:
        step = sequences_db.append_run_step(
            db, run_id=run_id, phase="winpe",
            kind=action["kind"], params=action["params"],
        )
        out.append({
            "step_id": step["id"],
            "kind": action["kind"],
            "params": action["params"],
        })
    return out


@router.post("/register")
def post_register(body: RegisterBody):
    db = _db_path()
    run = sequences_db.find_run_by_uuid_state(
        db, vm_uuid=body.vm_uuid, state="awaiting_winpe",
    )
    if run is None:
        # Distinguish 404 (no such uuid at all) from 409 (uuid exists, wrong state)
        with __import__("sqlite3").connect(db) as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT state FROM provisioning_runs WHERE vm_uuid=? "
                "ORDER BY id DESC LIMIT 1", (body.vm_uuid,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no run for vm_uuid")
        raise HTTPException(
            status_code=409,
            detail=f"run state is {row['state']!r}, expected awaiting_winpe",
        )

    # Idempotency: if steps already exist (re-registration), reuse them.
    existing = sequences_db.list_run_steps(db, run_id=run["id"])
    if existing:
        actions = [
            {"step_id": s["id"], "kind": s["kind"],
             "params": __import__("json").loads(s["params_json"])}
            for s in existing
        ]
    else:
        actions = _build_actions_for_run(db, run["id"], run["sequence_id"])

    token = winpe_token.sign(
        run_id=run["id"], ttl_seconds=_REGISTER_TOKEN_TTL,
    )
    return {
        "run_id": run["id"],
        "bearer_token": token,
        "actions": actions,
    }


def _require_bearer_for_run(run_id: int,
                            authorization: Optional[str] = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = winpe_token.verify(token)
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    if int(payload["run_id"]) != int(run_id):
        raise HTTPException(status_code=403, detail="token/run mismatch")
    return int(payload["run_id"])


@router.get("/sequence/{run_id}")
def get_sequence(run_id: int,
                 _: int = Depends(_require_bearer_for_run)):
    db = _db_path()
    steps = sequences_db.list_run_steps(db, run_id=run_id)
    return {
        "run_id": run_id,
        "actions": [
            {"step_id": s["id"], "kind": s["kind"],
             "params": __import__("json").loads(s["params_json"])}
            for s in steps
        ],
    }


def _resolve_autopilot_config_path():
    """Resolve AutopilotConfigurationFile.json. Mirrors what
    roles/autopilot_inject reads from autopilot_config_path. _load_vars
    returns the raw YAML, so the typical inventory value
    "{{ playbook_dir }}/../files/AutopilotConfigurationFile.json" is
    a literal Jinja string here; treat that as 'use default'."""
    from pathlib import Path
    from web import app as web_app
    cfg = web_app._load_vars()
    p = cfg.get("autopilot_config_path") or ""
    if p and "{{" not in p and "{%" not in p:
        return Path(p)
    base = Path(__file__).resolve().parent.parent
    return base / "files" / "AutopilotConfigurationFile.json"


def _credential_resolver_for_run():
    """Build a credential resolver matching the existing /api/jobs/provision
    pattern. Local-admin / domain-join steps need this to compile."""
    from web import app as web_app
    def _resolve(cid: int):
        rec = sequences_db.get_credential(_db_path(), web_app._cipher(), cid)
        return rec["payload"] if rec else None
    return _resolve


@router.get("/unattend/{run_id}")
def get_unattend(run_id: int,
                 _: int = Depends(_require_bearer_for_run)):
    from web import sequence_compiler, unattend_renderer
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    seq = sequences_db.get_sequence(db, run["sequence_id"])
    try:
        compiled = sequence_compiler.compile(
            seq, resolve_credential=_credential_resolver_for_run(),
        )
    except sequence_compiler.CompilerError as e:
        raise HTTPException(status_code=400, detail=f"compile failed: {e}")
    xml = unattend_renderer.render_unattend(
        compiled, phase_layout="post_winpe",
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/autopilot-config/{run_id}")
def get_autopilot_config(run_id: int,
                         _: int = Depends(_require_bearer_for_run)):
    from web import sequence_compiler
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    seq = sequences_db.get_sequence(db, run["sequence_id"])
    phase = sequence_compiler.compile_winpe(seq)
    if not phase.autopilot_enabled:
        raise HTTPException(status_code=404, detail="autopilot not enabled")
    path = _resolve_autopilot_config_path()
    if not path.is_file():
        raise HTTPException(
            status_code=500,
            detail=(
                f"autopilot enabled but {path} is missing; "
                "operator must populate AutopilotConfigurationFile.json"
            ),
        )
    return Response(content=path.read_bytes(),
                    media_type="application/json")


class StepResultBody(BaseModel):
    state: str
    error: Optional[str] = None
    stdout_tail: Optional[str] = None
    stderr_tail: Optional[str] = None
    elapsed_seconds: Optional[float] = None


def _require_bearer_token(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    try:
        return winpe_token.verify(
            authorization.removeprefix("Bearer ").strip()
        )
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/step/{step_id}/result")
def post_step_result(step_id: int, body: StepResultBody,
                     payload: dict = Depends(_require_bearer_token)):
    db = _db_path()
    step = sequences_db.get_run_step(db, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    if int(step["run_id"]) != int(payload["run_id"]):
        raise HTTPException(status_code=403, detail="token/run mismatch")

    if body.state == "running":
        sequences_db.update_run_step_state(
            db, step_id=step_id, state="running",
        )
    elif body.state == "ok":
        sequences_db.update_run_step_state(
            db, step_id=step_id, state="ok",
        )
    elif body.state == "error":
        sequences_db.update_run_step_state(
            db, step_id=step_id, state="error", error=body.error or "",
        )
        sequences_db.update_provisioning_run_state(
            db, run_id=int(step["run_id"]), state="failed",
            last_error=f"step {step['kind']}: {body.error or 'unknown'}",
        )
    else:
        raise HTTPException(status_code=400, detail=f"bad state: {body.state}")

    new_token = winpe_token.sign(
        run_id=int(payload["run_id"]), ttl_seconds=_REGISTER_TOKEN_TTL,
    )
    return {"ok": True, "bearer_token": new_token}


def _proxmox_detach_and_set_boot(*, vmid: int, slots: list[str],
                                 set_boot_order: str) -> None:
    """Detach disks and set boot order via Proxmox API.

    Reuses web.app._proxmox_api_put (line 1294), which constructs the
    URL + auth header from primitive vault fields. We deliberately do
    NOT read proxmox_api_base / proxmox_api_auth_header from
    _load_proxmox_config -- those values are Jinja strings in vars.yml
    that _load_vars never renders.

    One PUT carries delete= and boot= together; Proxmox accepts both
    keys in the same form body (same shape as
    roles/cleanup_answer_media.yml uses).
    """
    from web import app as web_app
    cfg = web_app._load_proxmox_config()
    node = cfg.get("proxmox_node") or "pve"
    body = {
        "delete": ",".join(slots),
        "boot": set_boot_order,
    }
    web_app._proxmox_api_put(
        f"/nodes/{node}/qemu/{vmid}/config", data=body,
    )


@router.post("/done")
def post_done(payload: dict = Depends(_require_bearer_token)):
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, int(payload["run_id"]))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["vmid"] is None:
        raise HTTPException(
            status_code=409, detail="run identity not set"
        )
    if run["state"] == "awaiting_winpe":
        _proxmox_detach_and_set_boot(
            vmid=int(run["vmid"]),
            slots=["ide2", "sata0"],
            set_boot_order="order=scsi0",
        )
        sequences_db.update_provisioning_run_state(
            db, run_id=int(run["id"]),
            state="awaiting_specialize",
        )
    return {"ok": True}
