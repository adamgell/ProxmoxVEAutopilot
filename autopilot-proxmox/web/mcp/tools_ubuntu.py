from __future__ import annotations

from typing import Any

from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}


def _compile_steps_fallback(plan_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in plan_steps:
        if step.get("enabled") is False or step.get("state") == "skipped":
            continue
        step_type = str(step.get("kind") or step.get("step_type") or "").strip()
        if not step_type:
            continue
        steps.append({
            "step_type": step_type,
            "params": dict(step.get("params") or step.get("params_json") or {}),
            "enabled": True,
        })
    return steps


def _normalize_evidence_fallback(evidence: dict[str, Any]) -> dict[str, str]:
    intune = str(evidence.get("intune") or evidence.get("intune_state") or "not_configured").lower()
    mde = str(evidence.get("mde") or evidence.get("mde_state") or "not_configured").lower()
    return {"intune": intune, "mde": mde}


def register(registry: ToolRegistry) -> None:
    @registry.register(
        "ubuntu_osd.compile_steps",
        "Convert OSD v2 plan steps into Ubuntu cloud-init compatible steps.",
        object_schema({"plan_steps": {"type": "array"}}, required=["plan_steps"]),
        annotations=READ,
    )
    def compile_steps(args: dict[str, Any]) -> dict[str, Any]:
        plan_steps = list(args.get("plan_steps") or [])
        try:
            from web import ubuntu_v2

            steps = ubuntu_v2.v2_plan_steps_to_ubuntu_steps(plan_steps)
        except ImportError:
            steps = _compile_steps_fallback(plan_steps)
        return {"schema_version": 1, "steps": steps}

    @registry.register(
        "ubuntu_osd.normalize_evidence",
        "Normalize Linux agent evidence into operator-facing readiness.",
        object_schema({"evidence": {"type": "object"}}, required=["evidence"]),
        annotations=READ,
    )
    def normalize_evidence(args: dict[str, Any]) -> dict[str, Any]:
        evidence = dict(args.get("evidence") or {})
        try:
            from web import ubuntu_v2

            return ubuntu_v2.readiness_from_linux_evidence(evidence)
        except ImportError:
            return _normalize_evidence_fallback(evidence)

    @registry.register(
        "ubuntu_osd.get_run",
        "Get an OSD v2 Ubuntu run with steps, events, and readiness.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=READ,
    )
    def get_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import osd_v2_endpoints

        run = osd_v2_endpoints.get_run(str(args["run_id"]))
        target_os = ((run.get("run") or {}).get("target_os") or "").lower()
        return {"is_ubuntu": target_os == "ubuntu", **run}

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
