from __future__ import annotations

import asyncio
from typing import Any

from . import mcp_pg
from .registry import redact
from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def _approval(tool_name: str, args: dict[str, Any], risk: str = "sensitive_mutation") -> dict[str, Any]:
    safe_args = redact(args)
    target = str(
        args.get("target")
        or args.get("vmid")
        or args.get("credential_id")
        or args.get("seq_id")
        or args.get("sequence_id")
        or tool_name
    )
    row = mcp_pg.create_approval(
        tool_name=tool_name,
        arguments=safe_args,
        target_summary=target,
        risk_label=risk,
    )
    return {
        "approval_required": True,
        "approval_id": row["approval_id"],
        "tool_name": tool_name,
        "risk_label": risk,
        "target_summary": target,
        "proposed_arguments": safe_args,
        "approval": row,
        "message": "This MCP surface requires two-step approval before execution.",
    }


def register(registry: ToolRegistry) -> None:
    @registry.register("pve_autopilot.get_cockpit_summary", "Get operator cockpit summary.", annotations=READ)
    async def get_cockpit_summary(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.api_cockpit_summary())

    @registry.register(
        "pve_autopilot.list_jobs",
        "List jobs.",
        object_schema({"limit": {"type": "integer"}}),
        annotations=READ,
    )
    async def list_jobs(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        jobs = await _maybe_await(web_app.api_list_jobs())
        if isinstance(jobs, list) and args.get("limit"):
            jobs = jobs[: int(args["limit"])]
        return {"jobs": jobs}

    @registry.register(
        "pve_autopilot.get_job",
        "Get job detail and log.",
        object_schema({"job_id": {"type": "string"}}, required=["job_id"]),
        annotations=READ,
    )
    async def get_job(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.api_get_job(str(args["job_id"])))

    @registry.register("pve_autopilot.list_vms", "List Proxmox Autopilot VMs.", annotations=READ)
    async def list_vms(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        cache, cache_age = await web_app._get_vms_payload()
        return {
            "rows": list(cache.get("data") or []),
            "cache_age_seconds": None if cache_age == float("inf") else cache_age,
            "refreshing": bool(cache.get("refreshing")),
        }

    @registry.register(
        "pve_autopilot.get_vm",
        "Get one VM from the Autopilot VM inventory.",
        object_schema({"vmid": {"type": "integer"}}, required=["vmid"]),
        annotations=READ,
    )
    async def get_vm(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        vmid = int(args["vmid"])
        cache, _ = await web_app._get_vms_payload()
        rows = [row for row in list(cache.get("data") or []) if int(row.get("vmid") or 0) == vmid]
        return {"vm": rows[0] if rows else None}

    @registry.register(
        "pve_autopilot.get_vm_console_status",
        "Get VM console/status JSON.",
        object_schema({"vmid": {"type": "integer"}}, required=["vmid"]),
        annotations=READ,
    )
    async def get_vm_console_status(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.vm_status_json(int(args["vmid"])))

    @registry.register("pve_autopilot.list_sequences", "List task sequences.", annotations=READ)
    def list_sequences(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return {"sequences": web_app.api_sequences_list()}

    @registry.register(
        "pve_autopilot.get_sequence",
        "Get task sequence detail.",
        object_schema({"seq_id": {"type": "integer"}}, required=["seq_id"]),
        annotations=READ,
    )
    def get_sequence(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return web_app.api_sequences_get(int(args["seq_id"]))

    @registry.register("pve_autopilot.list_credentials", "List credentials metadata.", annotations=READ)
    def list_credentials(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return {"credentials": web_app.api_credentials_list(type=args.get("type"))}

    @registry.register("pve_autopilot.get_monitoring_status", "Get monitoring deployment summary.", annotations=READ)
    async def get_monitoring_status(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.api_monitoring_deployments_summary())

    @registry.register("pve_autopilot.get_settings_summary", "Get redacted settings summary.", annotations=READ)
    def get_settings_summary(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        cfg = web_app._load_vars()
        safe = {
            key: value
            for key, value in cfg.items()
            if not any(token in key.lower() for token in ("password", "secret", "token", "key"))
        }
        return {"settings": safe, "redacted": True}

    @registry.register("pve_autopilot.get_service_health", "Get runtime service health.", annotations=READ)
    async def get_service_health(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        services = await _maybe_await(web_app.api_services())
        runtime = await _maybe_await(web_app.api_monitoring_runtime_services())
        return {"services": services, "runtime": runtime}

    @registry.register(
        "pve_autopilot.list_approvals",
        "List pending or historical MCP action approvals.",
        object_schema({"status": {"type": "string"}, "limit": {"type": "integer", "default": 100}}),
        annotations=READ,
    )
    def list_approvals(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "approvals": mcp_pg.list_approvals(
                status=args.get("status"),
                limit=int(args.get("limit") or 100),
            )
        }

    @registry.register(
        "pve_autopilot.get_approval",
        "Get one MCP action approval.",
        object_schema({"approval_id": {"type": "string"}}, required=["approval_id"]),
        annotations=READ,
    )
    def get_approval(args: dict[str, Any]) -> dict[str, Any]:
        approval = mcp_pg.get_approval(str(args["approval_id"]))
        return {"approval": approval, "found": bool(approval)}

    @registry.register(
        "pve_autopilot.reject_action",
        "Reject a pending MCP action approval.",
        object_schema(
            {"approval_id": {"type": "string"}, "reason": {"type": "string"}},
            required=["approval_id"],
        ),
        annotations=MUTATE,
    )
    def reject_action(args: dict[str, Any]) -> dict[str, Any]:
        approval = mcp_pg.reject_approval(
            str(args["approval_id"]),
            reason=str(args.get("reason") or ""),
        )
        return {"ok": bool(approval), "approval": approval}

    @registry.register(
        "pve_autopilot.approve_action",
        "Approve and execute a pending MCP action approval when an executor is registered.",
        object_schema({"approval_id": {"type": "string"}}, required=["approval_id"]),
        annotations=MUTATE,
    )
    def approve_action(args: dict[str, Any]) -> dict[str, Any]:
        approval = mcp_pg.approve_approval(str(args["approval_id"]), executors={})
        return {"ok": bool(approval), "approval": approval}

    @registry.register("pve_autopilot.refresh_vms", "Refresh VM cache.", annotations=MUTATE)
    async def refresh_vms(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        return await _maybe_await(web_app.api_vms_refresh())

    @registry.register("pve_autopilot.start_monitoring_sweep", "Start monitoring sweep and refresh VM cache.", annotations=MUTATE)
    async def start_monitoring_sweep(args: dict[str, Any]) -> dict[str, Any]:
        from web import app as web_app

        await web_app._run_monitor_sweep_and_refresh_vms_cache()
        return {"ok": True}

    for name in (
        "queue_provision_job",
        "queue_template_build",
        "resume_template_build",
        "queue_hash_capture",
        "sync_cloud_devices",
    ):
        registry.register(
            f"pve_autopilot.{name}",
            f"Queue {name.replace('_', ' ')} through the existing operator flow.",
            object_schema(),
            annotations=MUTATE,
        )(lambda args, tool=name: _approval(f"pve_autopilot.{tool}", args, risk="operator_mutation"))

    for name in (
        "delete_vm",
        "stop_vm",
        "reset_vm",
        "rename_vm",
        "send_console_key",
        "type_console_text",
        "create_credential",
        "update_credential",
        "delete_credential",
        "write_settings",
        "bootstrap_proxmox_permissions",
        "self_update",
        "delete_cloud_device",
        "delete_sequence",
        "overwrite_sequence",
    ):
        registry.register(
            f"pve_autopilot.{name}",
            f"Approval-gated {name.replace('_', ' ')}.",
            object_schema(),
            annotations=DESTRUCTIVE,
        )(lambda args, tool=name: _approval(f"pve_autopilot.{tool}", args, risk="destructive_or_sensitive"))
