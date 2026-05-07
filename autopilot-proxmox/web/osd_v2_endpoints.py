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

from web import osd_package
from web import ts_engine_pg, winpe_token


router = APIRouter(prefix="/osd/v2", tags=["osd-v2"])
api_router = APIRouter(prefix="/api/osd/v2", tags=["osd-v2-api"])
content_api_router = APIRouter(prefix="/api/content", tags=["content"])

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


class ContentStageBody(BaseModel):
    run_id: str
    agent_id: str
    phase: str
    status: str = Field(pattern=r"^(pending|staging|staged|failed)$")
    staging_path: Optional[str] = None
    error: Optional[str] = None


class ContentItemCreateBody(BaseModel):
    name: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    description: str = ""


class ContentVersionCreateBody(BaseModel):
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    source_uri: str = Field(min_length=1)
    size_bytes: Optional[int] = Field(default=None, ge=0)
    architecture: str = Field(default="any", min_length=1)
    target_os: str = Field(default="any", min_length=1)
    reboot_behavior: str = Field(
        default="none",
        pattern=r"^(none|optional|required|deferred)$",
    )
    conditions: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class SequenceCreateBody(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""


class SequenceStepCreateBody(BaseModel):
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    phase: str = Field(default="any", min_length=1)
    position: int = Field(ge=0)
    parent_id: Optional[str] = None
    enabled: bool = True
    condition: dict = Field(default_factory=dict)
    variables: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)
    content_refs: list[str] = Field(default_factory=list)
    continue_on_error: bool = False
    retry_count: int = Field(default=0, ge=0)
    retry_delay_seconds: int = Field(default=10, ge=0)
    timeout_seconds: Optional[int] = Field(default=None, ge=0)
    reboot_behavior: str = Field(
        default="none",
        pattern=r"^(none|optional|required|deferred)$",
    )


class SequenceRunCreateBody(BaseModel):
    resolve_content: bool = False
    deployment_target: dict = Field(default_factory=dict)
    run_variables: dict = Field(default_factory=dict)


def _database_url() -> str:
    from web import app as web_app

    try:
        return web_app._database_url()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Task Sequence Engine v2 database is not configured",
        )


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
        "phase": step["phase"],
        "attempt": step["attempt"],
        "timeout_seconds": step["timeout_seconds"],
        "retry_count": step["retry_count"],
        "retry_delay_seconds": step["retry_delay_seconds"],
        "reboot_behavior": step["reboot_behavior"],
        "params": step["resolved_params_json"],
        "content": ts_engine_pg.content_for_step(conn, step["id"]),
    }


def _manifest_response(conn, run_id: str) -> dict:
    try:
        ts_engine_pg.get_run(conn, run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "items": ts_engine_pg.list_run_manifest(conn, run_id),
    }


@router.get("/agent/package/{run_id}")
def get_v2_agent_package(
    run_id: str,
    phase: str = "full_os",
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(run_id, payload)
    with _conn() as conn:
        try:
            ts_engine_pg.get_run(conn, run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="run not found")
    try:
        files = osd_package.osd_client_files()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"OSD client package is missing files: {exc}",
        )
    agent_id = f"osd-{phase.replace('_', '')}-{run_id[:8]}"
    token = _sign(run_id)
    config = {
        "engine": "v2",
        "api_version": 2,
        "flask_base_url": "",
        "run_id": run_id,
        "agent_id": agent_id,
        "phase": phase,
        "bearer_token": token,
    }
    return {
        "schema_version": 2,
        "engine": "v2",
        "api_version": 2,
        "run_id": run_id,
        "phase": phase,
        "agent_id": agent_id,
        "bearer_token": token,
        "config_path": osd_package.CONFIG_PATH,
        "config": config,
        "files": files,
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


@router.get("/agent/content-manifest/{run_id}")
def get_agent_content_manifest(
    run_id: str,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(run_id, payload)
    with _conn() as conn:
        return _manifest_response(conn, run_id)


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


@router.post("/agent/content/{manifest_id}/stage")
def report_content_stage(
    manifest_id: str,
    body: ContentStageBody,
    payload: dict = Depends(_require_bearer),
):
    _require_run_token(body.run_id, payload)
    with _conn() as conn:
        try:
            return ts_engine_pg.mark_manifest_item_staging(
                conn,
                manifest_id=manifest_id,
                run_id=body.run_id,
                status=body.status,
                agent_id=body.agent_id,
                staging_path=body.staging_path,
                error=body.error,
            )
        except ValueError:
            raise HTTPException(status_code=404, detail="content not found")


@api_router.get("/runs/{run_id}/content-manifest")
def get_run_content_manifest(run_id: str):
    with _conn() as conn:
        return _manifest_response(conn, run_id)


@api_router.get("/runs/{run_id}/content-staging")
def get_run_content_staging(run_id: str):
    with _conn() as conn:
        try:
            ts_engine_pg.get_run(conn, run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "schema_version": 1,
            "run_id": run_id,
            "items": ts_engine_pg.list_run_content_staging(conn, run_id),
        }


@api_router.post("/sequences", status_code=201)
def create_sequence(body: SequenceCreateBody):
    with _conn() as conn:
        sequence_id = ts_engine_pg.create_sequence(
            conn,
            name=body.name,
            description=body.description,
        )
    return {
        "id": sequence_id,
        "name": body.name,
        "description": body.description,
    }


@api_router.post("/sequences/{sequence_id}/steps", status_code=201)
def create_sequence_step(sequence_id: str, body: SequenceStepCreateBody):
    with _conn() as conn:
        try:
            step_id = ts_engine_pg.add_step(
                conn,
                sequence_id=sequence_id,
                parent_id=body.parent_id,
                name=body.name,
                kind=body.kind,
                phase=body.phase,
                position=body.position,
                enabled=body.enabled,
                condition=body.condition,
                variables=body.variables,
                params=body.params,
                content_refs=body.content_refs,
                continue_on_error=body.continue_on_error,
                retry_count=body.retry_count,
                retry_delay_seconds=body.retry_delay_seconds,
                timeout_seconds=body.timeout_seconds,
                reboot_behavior=body.reboot_behavior,
            )
        except ForeignKeyViolation:
            conn.rollback()
            raise HTTPException(status_code=404, detail="sequence or parent not found")
        except UniqueViolation:
            conn.rollback()
            raise HTTPException(status_code=409, detail="step position already exists")
    return {
        "id": step_id,
        "sequence_id": sequence_id,
        "parent_id": body.parent_id,
        "name": body.name,
        "kind": body.kind,
        "phase": body.phase,
        "position": body.position,
        "enabled": body.enabled,
        "condition": body.condition,
        "variables": body.variables,
        "params": body.params,
        "content_refs": body.content_refs,
        "continue_on_error": body.continue_on_error,
        "retry_count": body.retry_count,
        "retry_delay_seconds": body.retry_delay_seconds,
        "timeout_seconds": body.timeout_seconds,
        "reboot_behavior": body.reboot_behavior,
    }


@api_router.post("/sequences/{sequence_id}/runs", status_code=201)
def create_sequence_run(sequence_id: str, body: SequenceRunCreateBody):
    with _conn() as conn:
        try:
            sequence_version_id = ts_engine_pg.compile_sequence(conn, sequence_id)
            run_id = ts_engine_pg.create_run_from_version(
                conn,
                sequence_version_id=sequence_version_id,
                deployment_target=body.deployment_target,
                run_variables=body.run_variables,
                resolve_content=body.resolve_content,
            )
            run = ts_engine_pg.get_run(conn, run_id)
            content_items = len(ts_engine_pg.list_run_manifest(conn, run_id))
        except ForeignKeyViolation:
            conn.rollback()
            raise HTTPException(status_code=404, detail="sequence not found")
        except ValueError as exc:
            conn.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
    return {
        "run_id": run_id,
        "sequence_id": sequence_id,
        "sequence_version_id": sequence_version_id,
        "state": run["state"],
        "content_items": content_items,
    }


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
                architecture=body.architecture,
                target_os=body.target_os,
                reboot_behavior=body.reboot_behavior,
                conditions=body.conditions,
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


@content_api_router.get("/manifest")
def get_global_content_manifest():
    with _conn() as conn:
        return ts_engine_pg.build_content_manifest_v1(conn)


@content_api_router.get("/items")
def list_global_content_items():
    return list_content_items()


@content_api_router.post("/items", status_code=201)
def create_global_content_item(body: ContentItemCreateBody):
    return create_content_item(body)


@content_api_router.post("/items/{item_id}/versions", status_code=201)
def create_global_content_version(item_id: str, body: ContentVersionCreateBody):
    return create_content_version(item_id, body)
