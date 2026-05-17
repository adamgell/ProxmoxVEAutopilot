from __future__ import annotations

import asyncio
from typing import Any

from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def register(registry: ToolRegistry) -> None:
    @registry.register("setup.get_readiness", "Get first-run setup readiness.", annotations=READ)
    def get_readiness(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return web_app._setup_readiness()

    @registry.register("setup.get_state", "Get sanitized first-run setup state.", annotations=READ)
    def get_state(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return web_app._public_setup_state()

    @registry.register("setup.get_media", "Get first-run setup media readiness.", annotations=READ)
    def get_media(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return web_app._setup_readiness()["media"]

    @registry.register("setup.get_build_host", "Get first-run build-host readiness.", annotations=READ)
    def get_build_host(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return web_app._setup_readiness()["build_host"]

    @registry.register("setup.list_build_host_workloads", "List build-host agent workload work items.", annotations=READ)
    def list_build_host_workloads(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg
        from web import app as web_app
        from web import db_pg

        build_host = web_app._setup_readiness()["build_host"]
        agent_id = build_host.get("expected_agent_id")
        if not agent_id:
            return {"build_host": build_host, "work_items": []}
        with db_pg.connection(web_app._database_url()) as conn:
            agent_telemetry_pg.init(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM agent_work_items
                WHERE agent_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (agent_id, int(args.get("limit") or 100)),
            ).fetchall()
        return {"build_host": build_host, "work_items": [dict(row) for row in rows]}

    @registry.register("setup.list_artifacts", "List first-run setup artifacts.", annotations=READ)
    def list_artifacts(args: dict[str, Any]) -> dict[str, Any]:
        from web import setup_artifacts

        return {
            "schema_version": 1,
            "summary": setup_artifacts.readiness_summary(),
            "artifacts": setup_artifacts.list_artifacts(),
        }

    @registry.register(
        "setup.queue_build_host_seed_iso",
        "Generate and upload the build-host seed ISO.",
        object_schema({"vmid": {"type": "integer"}}, required=["vmid"]),
        annotations=MUTATE,
    )
    async def queue_build_host_seed_iso(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.setup_build_host_seed_iso(web_app._BuildHostSeedIsoBody(**args)))

    @registry.register(
        "setup.create_build_host_vm",
        "Create the first-run Windows build-host VM.",
        object_schema(),
        annotations=MUTATE,
    )
    async def create_build_host_vm(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.setup_create_build_host_vm(web_app._BuildHostVmBody(**args)))

    @registry.register(
        "setup.repair_build_host_agent",
        "Repair the first-run build-host agent configuration.",
        object_schema(),
        annotations=MUTATE,
    )
    async def repair_build_host_agent(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.setup_repair_build_host_agent(web_app._BuildHostRepairBody(**args)))

    @registry.register(
        "setup.queue_build_host_workloads",
        "Queue build-host workloads for agent MSI, WinPE, CloudOSD, OSDeploy, and publishing.",
        object_schema(),
        annotations=MUTATE,
    )
    async def queue_build_host_workloads(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.setup_queue_build_host_workloads(web_app._BuildHostWorkloadsBody(**args)))

    @registry.register(
        "setup.promote_artifacts",
        "Promote setup-produced ISO artifacts into Proxmox ISO storage.",
        object_schema(),
        annotations=MUTATE,
    )
    async def promote_artifacts(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.setup_promote_artifacts(web_app._PromoteSetupArtifactsBody(**args)))
