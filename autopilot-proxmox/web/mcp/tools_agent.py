from __future__ import annotations

from typing import Any

from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}


DIAGNOSTICS = {
    "system_summary",
    "services",
    "event_logs",
    "network",
    "dns",
    "domain_join",
    "entra_join",
    "intune_mdm",
    "qga",
    "autopilot_agent_logs",
    "autopilot_hash_capture_state",
}


def _conn():
    from web import db_pg

    return db_pg.connection()


def register(registry: ToolRegistry) -> None:
    @registry.register("autopilot_agent.list_agents", "List AutopilotAgent devices.", annotations=READ)
    def list_agents(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            return {"schema_version": 1, "agents": agent_telemetry_pg.latest_agents(conn)}

    @registry.register(
        "autopilot_agent.get_agent",
        "Get one AutopilotAgent device.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations=READ,
    )
    def get_agent(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            return {"agent": agent_telemetry_pg.get_device(conn, str(args["agent_id"]))}

    @registry.register(
        "autopilot_agent.get_latest_heartbeat",
        "Get latest AutopilotAgent heartbeat.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations=READ,
    )
    def get_latest_heartbeat(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            return {"heartbeat": agent_telemetry_pg.latest_for_agent(conn, str(args["agent_id"]))}

    @registry.register(
        "autopilot_agent.list_events",
        "List AutopilotAgent events.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations=READ,
    )
    def list_events(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            return {"events": agent_telemetry_pg.list_events(conn, str(args["agent_id"]))}

    @registry.register(
        "autopilot_agent.list_work_items",
        "List AutopilotAgent work items.",
        object_schema({"status": {"type": "string"}, "limit": {"type": "integer"}}),
        annotations=READ,
    )
    def list_work_items(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            return {
                "work_items": agent_telemetry_pg.list_work_items(
                    conn,
                    status=args.get("status"),
                    limit=int(args.get("limit") or 200),
                )
            }

    @registry.register("autopilot_agent.list_bootstrap_approvals", "List bootstrap approvals.", annotations=READ)
    def list_bootstrap_approvals(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            rows = conn.execute(
                """
                SELECT *
                FROM agent_bootstrap_approvals
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (int(args.get("limit") or 100),),
            ).fetchall()
            return {"approvals": [dict(row) for row in rows]}

    @registry.register(
        "autopilot_agent.approve_bootstrap",
        "Approve an AutopilotAgent bootstrap request.",
        object_schema({"approval_id": {"type": "string"}}, required=["approval_id"]),
        annotations=MUTATE,
    )
    def approve_bootstrap(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            row = agent_telemetry_pg.approve_bootstrap_approval(conn, str(args["approval_id"]))
            return {"ok": bool(row), "approval": row}

    @registry.register(
        "autopilot_agent.revoke_agent",
        "Revoke an AutopilotAgent device.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations={"destructiveHint": True, "readOnlyHint": False},
    )
    def revoke_agent(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            agent_telemetry_pg.revoke_agent(conn, str(args["agent_id"]))
            return {"ok": True, "agent_id": str(args["agent_id"])}

    @registry.register(
        "autopilot_agent.queue_hash_capture",
        "Queue AutopilotAgent hash capture.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations=MUTATE,
    )
    def queue_hash_capture(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            row = agent_telemetry_pg.create_work_item(
                conn,
                agent_id=str(args["agent_id"]),
                kind="capture_autopilot_hash",
                request={"source": "mcp", **args},
                vmid=args.get("vmid"),
            )
            return {"ok": True, "work_item": row}

    @registry.register(
        "autopilot_agent.run_diagnostic",
        "Queue a curated AutopilotAgent diagnostic work item.",
        object_schema(
            {
                "agent_id": {"type": "string"},
                "diagnostic": {"type": "string", "enum": sorted(DIAGNOSTICS)},
                "parameters": {"type": "object"},
            },
            required=["agent_id", "diagnostic"],
        ),
        annotations=MUTATE,
    )
    def run_diagnostic(args: dict[str, Any]) -> dict[str, Any]:
        from web import agent_telemetry_pg

        diagnostic = str(args["diagnostic"])
        if diagnostic not in DIAGNOSTICS:
            raise ValueError(f"unsupported diagnostic: {diagnostic}")
        with _conn() as conn:
            agent_telemetry_pg.init(conn)
            row = agent_telemetry_pg.create_work_item(
                conn,
                agent_id=str(args["agent_id"]),
                kind="run_diagnostic",
                request={
                    "diagnostic": diagnostic,
                    "parameters": dict(args.get("parameters") or {}),
                    "timeout_seconds": min(int(args.get("timeout_seconds") or 120), 300),
                    "output_limit_bytes": min(int(args.get("output_limit_bytes") or 131072), 524288),
                    "source": "mcp",
                },
                vmid=args.get("vmid"),
            )
            return {"ok": True, "work_item": row}
