"""Task Engine v2 catalog metadata shared by UI and API code."""
from __future__ import annotations

from typing import Iterable


UBUNTU_COMPILE_STEP_KINDS = frozenset({
    "install_ubuntu_core",
    "create_ubuntu_user",
    "install_desktop_environment",
    "install_apt_packages",
    "remove_apt_packages",
    "install_snap_packages",
    "install_intune_portal",
    "install_edge",
    "install_mde_linux",
    "run_late_command",
    "run_firstboot_script",
})

UBUNTU_AGENT_STEP_KINDS = frozenset({
    "verify_qga_linux",
    "verify_intune_portal",
    "verify_mde_linux",
    "wait_cloud_init_complete",
    "linux_agent_heartbeat",
})

UBUNTU_STEP_KINDS = UBUNTU_COMPILE_STEP_KINDS | UBUNTU_AGENT_STEP_KINDS

WINDOWS_STEP_KINDS = frozenset({
    "capture_hash",
    "partition_disk",
    "apply_wim",
    "apply_driver_package",
    "prepare_windows_setup",
    "bake_boot_entry",
    "handoff_to_windows_setup",
    "cloudosd_preflight",
    "cloudosd_deploy_os",
    "osdeploy_preflight",
    "stage_ad_domain_join_unattend",
    "verify_ad_domain_join",
    "cloudosd_validate_offline_os",
    "stage_osd_client",
    "stage_autopilot_agent",
    "install_autopilot_agent",
    "capture_autopilot_hash",
    "wait_agent_heartbeat",
    "install_qga",
    "fix_recovery_partition",
    "verify_qga",
    "install_qga_watchdog",
    "handoff_to_oobe",
    "run_script",
    "rename_computer",
})

ANY_STEP_KINDS = frozenset({
    "install_package",
    "install_app",
    "proxmox_clone_vm",
    "apply_oem_profile",
    "set_smbios_chassis",
    "wait_guest_agent",
})

UBUNTU_PHASES = (
    ("controller", "Controller"),
    ("install", "Install"),
    ("first_boot", "First Boot"),
    ("full_os", "Full OS"),
    ("verify", "Verify"),
)

WINDOWS_PHASES = (
    ("controller", "Controller"),
    ("pe", "WinPE"),
    ("specialize", "Specialize"),
    ("full_os", "Full OS"),
    ("verify", "Verify"),
)


def target_for_step_kind(kind: str) -> str:
    if kind in UBUNTU_STEP_KINDS:
        return "ubuntu"
    if kind in WINDOWS_STEP_KINDS:
        return "windows"
    return "any"


def phases_for_target_os(target_os: str | None) -> tuple[tuple[str, str], ...]:
    return UBUNTU_PHASES if (target_os or "").lower() == "ubuntu" else WINDOWS_PHASES


def incompatible_steps(target_os: str | None, steps: Iterable[dict]) -> list[dict]:
    target = (target_os or "windows").lower()
    incompatible: list[dict] = []
    for index, step in enumerate(steps):
        if step.get("enabled") is False:
            continue
        kind = str(step.get("kind") or "").strip()
        if not kind:
            continue
        step_target = target_for_step_kind(kind)
        if step_target not in ("any", target):
            incompatible.append({
                "index": index,
                "kind": kind,
                "target_os": step_target,
                "name": step.get("name") or kind,
            })
    return incompatible


def validate_steps_for_target_os(target_os: str | None, steps: Iterable[dict]) -> None:
    bad = incompatible_steps(target_os, steps)
    if not bad:
        return
    target = target_os or "windows"
    names = ", ".join(f"{item['name']} ({item['kind']})" for item in bad)
    raise ValueError(f"{target} sequence contains incompatible enabled step(s): {names}")
