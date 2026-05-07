"""Task Sequence Engine v2 agent API.

The v2 API is phase-neutral: WinPE, full OS, and future recovery agents
all register, claim, log, and report results through the same protocol.
It is backed by ``ts_engine_pg`` only; the legacy SQLite OSD endpoints
stay in place while agents migrate.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import BaseModel, Field

from web import ts_engine_pg, winpe_token


router = APIRouter(prefix="/osd/v2", tags=["osd-v2"])
api_router = APIRouter(prefix="/api/osd/v2", tags=["osd-v2-api"])

_AGENT_TOKEN_TTL = 24 * 60 * 60

class AgentRegisterBody(BaseModel):
    run_id: str
    agent_id: str
    phase: str
    computer_name: Optional[str] = None
    build_sha: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)


class AgentNextBody(BaseModel):
    run_id: str
    agent_id: str
    phase: str
    batch_size: int = 1


class StepLogBody(BaseModel):
    run_id: str
    agent_id: str
    stream: str
    content: str


class StepResultBody(BaseModel):
    run_id: str
    agent_id: str
    phase: str
    status: str
    message: Optional[str] = None
    data: dict = Field(default_factory=dict)


class RebootingBody(BaseModel):
    run_id: str
    agent_id: str
    phase: str
    step_id: str
    message: Optional[str] = None


class PhaseCompleteBody(BaseModel):
    run_id: str
    agent_id: str
    phase: str


class ContentItemCreateBody(BaseModel):
    name: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    description: str = ""


class ContentVersionCreateBody(BaseModel):
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    source_uri: str = Field(min_length=1)
    size_bytes: Optional[int] = Field(default=None, ge=0)
    metadata: dict = Field(default_factory=dict)


def _database_url() -> str:
    from web import app as web_app

    dsn = web_app._ts_engine_database_url()
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="Task Sequence Engine v2 database is not configured",
        )
    return dsn


@contextmanager
def _conn():
    with ts_engine_pg.connect(_database_url()) as conn:
        yield conn


def _sign(run_id: str) -> str:
    return winpe_token.sign(run_id=run_id, ttl_seconds=_AGENT_TOKEN_TTL)


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


def _action_from_step(conn, step: dict) -> dict:
    return {
        "step_id": step["id"],
        "kind": step["kind"],
        "attempt": step["attempt"],
        "timeout_seconds": step["timeout_seconds"],
        "retry_count": step["retry_count"],
        "retry_delay_seconds": step["retry_delay_seconds"],
        "reboot_behavior": step["reboot_behavior"],
        "params": step["resolved_params_json"],
        "content": ts_engine_pg.content_for_step(conn, step["id"]),
    }


@router.post("/agent/register")
def register_agent(body: AgentRegisterBody):
    with _conn() as conn:
        try:
            run = ts_engine_pg.get_run(conn, body.run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="run not found")
        if run["state"] == "awaiting_reboot" and run["cursor_step_id"]:
            ts_engine_pg.mark_reboot_complete(
                conn,
                run_id=body.run_id,
                step_id=run["cursor_step_id"],
                agent_id=body.agent_id,
            )
    return {
        "run_id": body.run_id,
        "agent_id": body.agent_id,
        "phase": body.phase,
        "bearer_token": _sign(body.run_id),
    }


@router.post("/agent/next")
def next_action(body: AgentNextBody, payload: dict = Depends(_require_bearer)):
    _require_run_token(body.run_id, payload)
    actions = []
    batch_size = max(1, min(int(body.batch_size or 1), 10))
    with _conn() as conn:
        for _ in range(batch_size):
            step = ts_engine_pg.claim_next_step(
                conn,
                run_id=body.run_id,
                phase=body.phase,
                agent_id=body.agent_id,
            )
            if step is None:
                break
            actions.append(_action_from_step(conn, step))
    return {
        "run_id": body.run_id,
        "phase": body.phase,
        "actions": actions,
        "reboot_required": False,
        "bearer_token": _sign(body.run_id),
    }


@router.post("/agent/step/{step_id}/logs")
def post_step_logs(
    step_id: str,
    body: StepLogBody,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(body.run_id, payload)
    with _conn() as conn:
        try:
            step = ts_engine_pg.get_step(conn, step_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="step not found")
        if str(step["run_id"]) != str(body.run_id):
            raise HTTPException(status_code=403, detail="step/run mismatch")
        ts_engine_pg.append_step_log(
            conn,
            run_id=body.run_id,
            step_id=step_id,
            agent_id=body.agent_id,
            stream=body.stream,
            content=body.content,
        )
    return {"ok": True, "bearer_token": _sign(body.run_id)}


@router.post("/agent/step/{step_id}/result")
def post_step_result(
    step_id: str,
    body: StepResultBody,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(body.run_id, payload)
    with _conn() as conn:
        try:
            step = ts_engine_pg.get_step(conn, step_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="step not found")
        if str(step["run_id"]) != str(body.run_id):
            raise HTTPException(status_code=403, detail="step/run mismatch")
        if step["phase"] not in (body.phase, "any"):
            raise HTTPException(status_code=409, detail="step phase mismatch")
        try:
            updated = ts_engine_pg.complete_step(
                conn,
                run_id=body.run_id,
                step_id=step_id,
                agent_id=body.agent_id,
                status=body.status,
                message=body.message,
                data=body.data,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "step": updated,
        "bearer_token": _sign(body.run_id),
    }


@router.post("/agent/rebooting")
def post_rebooting(body: RebootingBody, payload: dict = Depends(_require_bearer)):
    _require_run_token(body.run_id, payload)
    with _conn() as conn:
        try:
            step = ts_engine_pg.complete_step(
                conn,
                run_id=body.run_id,
                step_id=body.step_id,
                agent_id=body.agent_id,
                status="reboot_required",
                message=body.message or "agent is rebooting",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "step": step, "bearer_token": _sign(body.run_id)}


@router.post("/agent/phase-complete")
def post_phase_complete(
    body: PhaseCompleteBody,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(body.run_id, payload)
    with _conn() as conn:
        steps = ts_engine_pg.list_run_steps(conn, body.run_id)
    incomplete = [
        step for step in steps
        if step["phase"] in (body.phase, "any")
        and step["state"] in ("pending", "running", "awaiting_reboot")
    ]
    return {
        "ok": True,
        "run_id": body.run_id,
        "phase": body.phase,
        "phase_complete": not incomplete,
        "incomplete": [
            {"step_id": step["id"], "kind": step["kind"], "state": step["state"]}
            for step in incomplete
        ],
        "bearer_token": _sign(body.run_id),
    }


@router.get("/content/{manifest_id}")
def get_content_manifest_item(
    manifest_id: str,
    payload: dict = Depends(_require_bearer),
):
    with _conn() as conn:
        try:
            item = ts_engine_pg.get_manifest_item(conn, manifest_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="content not found")
    _require_run_token(item["run_id"], payload)
    return item


@api_router.get("/content/items")
def list_content_items():
    with _conn() as conn:
        return {"items": ts_engine_pg.list_content_items(conn)}


@api_router.post("/content/items", status_code=201)
def create_content_item(body: ContentItemCreateBody):
    with _conn() as conn:
        try:
            item_id = ts_engine_pg.create_content_item(
                conn,
                name=body.name,
                content_type=body.content_type,
                description=body.description,
            )
        except UniqueViolation:
            conn.rollback()
            raise HTTPException(status_code=409, detail="content item already exists")
        return ts_engine_pg.get_content_item(conn, item_id)


@api_router.post("/content/items/{item_id}/versions", status_code=201)
def create_content_version(item_id: str, body: ContentVersionCreateBody):
    with _conn() as conn:
        try:
            version_id = ts_engine_pg.create_content_version(
                conn,
                content_item_id=item_id,
                version=body.version,
                sha256=body.sha256.lower(),
                source_uri=body.source_uri,
                size_bytes=body.size_bytes,
                metadata=body.metadata,
            )
        except UniqueViolation:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="content version already exists for item",
            )
        except ForeignKeyViolation:
            conn.rollback()
            raise HTTPException(status_code=404, detail="content item not found")
        return ts_engine_pg.get_content_version(conn, version_id)
