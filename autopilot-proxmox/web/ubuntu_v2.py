"""Ubuntu helpers for Task Engine v2.

The implementation keeps Ubuntu on the current cloud-image + NoCloud compiler
substrate. v2 owns sequence intent and run evidence; this module adapts v2
steps to the existing Ubuntu cloud-init compiler.
"""
from __future__ import annotations

from typing import Any

from web.osd_v2_catalog import UBUNTU_COMPILE_STEP_KINDS

INTUNE_READINESS_STATES = frozenset({
    "not_configured",
    "portal_not_installed",
    "portal_installed",
    "waiting_for_user_signin",
    "enrolled",
    "error",
})

MDE_READINESS_STATES = frozenset({
    "not_configured",
    "mdatp_not_installed",
    "installed_not_onboarded",
    "healthy",
    "unhealthy",
    "error",
})


def v2_plan_steps_to_ubuntu_steps(plan_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return compiler-compatible Ubuntu steps from v2 plan steps.

    Verification and Linux-agent steps are deliberately excluded from cloud-init
    generation; the Linux agent claims them after first boot.
    """
    ubuntu_steps: list[dict[str, Any]] = []
    for step in plan_steps:
        if step.get("enabled") is False or step.get("state") == "skipped":
            continue
        kind = str(step.get("kind") or "").strip()
        if kind not in UBUNTU_COMPILE_STEP_KINDS:
            continue
        ubuntu_steps.append({
            "step_type": kind,
            "params": dict(step.get("params") or step.get("params_json") or {}),
            "enabled": True,
        })
    return ubuntu_steps


def readiness_from_linux_evidence(data: dict[str, Any] | None) -> dict[str, str]:
    """Normalize Linux agent evidence into operator-facing readiness states."""
    payload = data or {}
    intune = str(payload.get("intune") or payload.get("intune_state") or "").lower()
    mde = str(payload.get("mde") or payload.get("mde_state") or "").lower()

    if intune not in INTUNE_READINESS_STATES:
        if payload.get("intune_portal_version"):
            intune = "waiting_for_user_signin"
        elif payload.get("intune_error"):
            intune = "error"
        else:
            intune = "not_configured"

    if mde not in MDE_READINESS_STATES:
        health = str(payload.get("mde_health") or "").lower()
        if health in ("true", "healthy", "ok"):
            mde = "healthy"
        elif payload.get("mdatp_installed"):
            mde = "installed_not_onboarded"
        elif payload.get("mde_error"):
            mde = "error"
        else:
            mde = "not_configured"

    return {"intune": intune, "mde": mde}
