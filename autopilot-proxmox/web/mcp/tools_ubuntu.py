from __future__ import annotations

from typing import Any

from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}


def register(registry: ToolRegistry) -> None:
    @registry.register(
        "ubuntu_osd.compile_steps",
        "Convert OSD v2 plan steps into Ubuntu cloud-init compatible steps.",
        object_schema({"plan_steps": {"type": "array"}}, required=["plan_steps"]),
        annotations=READ,
    )
    def compile_steps(args: dict[str, Any]) -> dict[str, Any]:
        from web import ubuntu_v2

        steps = ubuntu_v2.v2_plan_steps_to_ubuntu_steps(list(args.get("plan_steps") or []))
        return {"schema_version": 1, "steps": steps}

    @registry.register(
        "ubuntu_osd.normalize_evidence",
        "Normalize Linux agent evidence into operator-facing readiness.",
        object_schema({"evidence": {"type": "object"}}, required=["evidence"]),
        annotations=READ,
    )
    def normalize_evidence(args: dict[str, Any]) -> dict[str, Any]:
        from web import ubuntu_v2

        return ubuntu_v2.readiness_from_linux_evidence(dict(args.get("evidence") or {}))

    @registry.register(
        "ubuntu_osd.queue_template_build",
        "Queue an Ubuntu OSD v2 template build.",
        object_schema(),
        annotations=MUTATE,
    )
    def queue_template_build(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "approval_required": True,
            "risk_label": "infrastructure_mutation",
            "target_summary": "Ubuntu template build queues infrastructure work",
            "proposed_arguments": args,
            "message": "Use the operator console or add this to the MCP approval executor before direct execution.",
        }
