from __future__ import annotations

import importlib.util
from pathlib import Path

from web.ubuntu_v2 import readiness_from_linux_evidence, v2_plan_steps_to_ubuntu_steps


def test_v2_plan_steps_to_ubuntu_steps_excludes_agent_verify_steps():
    steps = [
        {
            "kind": "install_ubuntu_core",
            "params_json": {"timezone": "UTC"},
            "state": "pending",
        },
        {
            "kind": "verify_qga_linux",
            "params_json": {},
            "state": "pending",
        },
        {
            "kind": "install_mde_linux",
            "params": {"mde_onboarding_credential_id": 42},
            "state": "pending",
        },
    ]

    compiled = v2_plan_steps_to_ubuntu_steps(steps)

    assert compiled == [
        {
            "step_type": "install_ubuntu_core",
            "params": {"timezone": "UTC"},
            "enabled": True,
        },
        {
            "step_type": "install_mde_linux",
            "params": {"mde_onboarding_credential_id": 42},
            "enabled": True,
        },
    ]


def test_readiness_maps_intune_portal_to_waiting_for_user_signin():
    readiness = readiness_from_linux_evidence({
        "intune_portal_version": "1.2404.12",
        "mde_health": "healthy",
    })

    assert readiness["intune"] == "waiting_for_user_signin"
    assert readiness["mde"] == "healthy"


def test_linux_agent_uses_dpkg_for_intune_portal_readiness(monkeypatch):
    agent_path = (
        Path(__file__).resolve().parents[1]
        / "files/linux-agent/autopilot_linux_agent.py"
    )
    spec = importlib.util.spec_from_file_location("autopilot_linux_agent_test", agent_path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    def fake_run(cmd, timeout=30):
        joined = " ".join(cmd)
        if "dpkg-query" in joined and "intune-portal" in joined:
            return {"ok": True, "returncode": 0, "stdout": "1.2604.13-noble", "stderr": ""}
        if "intune-portal --version" in joined:
            return {"ok": False, "returncode": 134, "stdout": "", "stderr": "Aborted"}
        if "systemctl is-active qemu-guest-agent" in joined:
            return {"ok": True, "returncode": 0, "stdout": "active", "stderr": ""}
        if "cloud-init status" in joined:
            return {"ok": True, "returncode": 0, "stdout": '{"status":"done"}', "stderr": ""}
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent, "_run", fake_run)
    monkeypatch.setattr(agent, "_primary_ipv4", lambda: "192.168.2.90")

    evidence = agent._evidence()

    assert evidence["intune_portal_version"] == "1.2604.13-noble"
    assert evidence["intune"] == "waiting_for_user_signin"


def test_ubuntu_readiness_uses_latest_event_evidence():
    from web.osd_v2_endpoints import _ubuntu_readiness_from_steps

    step_id = "step-verify-intune"
    readiness = _ubuntu_readiness_from_steps(
        steps=[
            {"id": step_id, "kind": "verify_intune_portal", "state": "done"},
            {"id": "step-heartbeat", "kind": "linux_agent_heartbeat", "state": "done"},
        ],
        events=[
            {
                "step_id": step_id,
                "event_type": "step_done",
                "data_json": {
                    "intune": "waiting_for_user_signin",
                    "intune_portal_version": "1.2604.13-noble",
                },
            },
            {
                "step_id": step_id,
                "event_type": "step_failed",
                "data_json": {"intune": "portal_not_installed"},
            },
        ],
    )

    assert readiness == {
        "intune": "waiting_for_user_signin",
        "mde": "not_configured",
        "linux_agent": "heartbeat_observed",
    }
