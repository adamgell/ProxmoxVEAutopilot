#!/usr/bin/env python3
"""Lightweight ProxmoxVEAutopilot Linux v2 agent.

This agent intentionally uses only the Python standard library so cloud-init can
start it on a fresh Ubuntu cloud image without extra package dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def _run(cmd: list[str], timeout: int = 30) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _primary_ipv4() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return ""


def _request(config: dict, method: str, path: str, body: dict | None = None) -> dict:
    url = config["server_url"].rstrip("/") + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    token = config.get("bearer_token")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")
    if payload.get("bearer_token"):
        config["bearer_token"] = payload["bearer_token"]
    return payload


def _log(config: dict, step_id: str, stream: str, content: str) -> None:
    _request(config, "POST", f"/osd/v2/agent/step/{step_id}/logs", {
        "run_id": config["run_id"],
        "agent_id": config["agent_id"],
        "stream": stream,
        "content": content,
    })


def _result(config: dict, step_id: str, phase: str, status: str, message: str, data: dict) -> None:
    _request(config, "POST", f"/osd/v2/agent/step/{step_id}/result", {
        "run_id": config["run_id"],
        "agent_id": config["agent_id"],
        "phase": phase,
        "status": status,
        "message": message,
        "data": data,
    })


def _evidence() -> dict:
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                os_release[key] = value.strip().strip('"')
    except Exception:
        pass
    intune_pkg = _run(["bash", "-lc", "dpkg-query -W -f='${Version}' intune-portal 2>/dev/null"])
    intune_version = _run(["bash", "-lc", "intune-portal --version 2>/dev/null"])
    mdatp_health = _run(["bash", "-lc", "mdatp health --output json 2>/dev/null"])
    qga = _run(["bash", "-lc", "systemctl is-active qemu-guest-agent 2>/dev/null"])
    cloud_init = _run(["bash", "-lc", "cloud-init status --format json 2>/dev/null"])
    return {
        "hostname": socket.gethostname(),
        "primary_ipv4": _primary_ipv4(),
        "distro": os_release.get("PRETTY_NAME") or platform.platform(),
        "kernel": platform.release(),
        "qga_active": qga["stdout"] == "active",
        "qga_status": qga,
        "cloud_init_status": cloud_init,
        "intune_portal_version": intune_pkg["stdout"] or (intune_version["stdout"] if intune_version["ok"] else ""),
        "intune": "waiting_for_user_signin" if intune_pkg["ok"] else "portal_not_installed",
        "mde_health": mdatp_health["stdout"],
        "mdatp_installed": mdatp_health["ok"],
        "mde": "healthy" if mdatp_health["ok"] and "healthy" in mdatp_health["stdout"].lower() else ("installed_not_onboarded" if mdatp_health["ok"] else "mdatp_not_installed"),
    }


def _execute_action(config: dict, action: dict) -> None:
    step_id = action["step_id"]
    kind = action["kind"]
    phase = action.get("phase") or config.get("phase", "verify")
    evidence = _evidence()
    _log(config, step_id, "stdout", f"Executing {kind} on {evidence['hostname']}")
    status = "success"
    message = f"{kind} complete"
    data = dict(evidence)

    if kind == "verify_qga_linux" and not evidence["qga_active"]:
        status = "failed"
        message = "qemu-guest-agent is not active"
    elif kind == "verify_intune_portal" and evidence["intune"] == "portal_not_installed":
        status = "failed"
        message = "intune-portal is not installed"
    elif kind == "verify_mde_linux" and evidence["mde"] in ("mdatp_not_installed", "unhealthy"):
        status = "failed"
        message = "mdatp is not healthy"
    elif kind == "wait_cloud_init_complete":
        waited = _run(["bash", "-lc", "cloud-init status --wait"], timeout=int(action.get("timeout_seconds") or 900))
        data["cloud_init_wait"] = waited
        data.update(_evidence())
        if not waited["ok"]:
            status = "failed"
            message = "cloud-init did not complete successfully"
    elif kind == "run_firstboot_script":
        cmd = (action.get("params") or {}).get("command")
        if cmd:
            ran = _run(["bash", "-lc", cmd], timeout=int(action.get("timeout_seconds") or 600))
            data["script"] = ran
            status = "success" if ran["ok"] else "failed"
            message = "first-boot script complete" if ran["ok"] else "first-boot script failed"

    _result(config, step_id, phase, status, message, data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    reg = _request(config, "POST", "/osd/v2/agent/register", {
        "run_id": config["run_id"],
        "agent_id": config["agent_id"],
        "phase": config.get("phase", "verify"),
        "computer_name": socket.gethostname(),
        "capabilities": [
            "linux_agent_heartbeat",
            "wait_cloud_init_complete",
            "verify_qga_linux",
            "verify_intune_portal",
            "verify_mde_linux",
            "run_firstboot_script",
        ],
    })
    if reg.get("bearer_token"):
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    phases = ["full_os", "verify"]
    while True:
        claimed_any = False
        for phase in phases:
            payload = _request(config, "POST", "/osd/v2/agent/next", {
                "run_id": config["run_id"],
                "agent_id": config["agent_id"],
                "phase": phase,
                "batch_size": 1,
            })
            actions = payload.get("actions") or []
            if not actions:
                _request(config, "POST", "/osd/v2/agent/phase-complete", {
                    "run_id": config["run_id"],
                    "agent_id": config["agent_id"],
                    "phase": phase,
                })
                continue
            claimed_any = True
            for action in actions:
                _execute_action(config, action)
        if not claimed_any:
            _request(config, "POST", "/osd/v2/agent/phase-complete", {
                "run_id": config["run_id"],
                "agent_id": config["agent_id"],
                "phase": config.get("phase", "verify"),
            })
            if args.once:
                return 0
            time.sleep(30)
            continue
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
