"""install_apt_packages / install_snap_packages / remove_apt_packages.

All run on the live installed system via cloud-init (no curtin in-target) —
the cloud image has already booted by the time runcmd/packages fire.
"""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput


@register("install_apt_packages")
def compile_install_apt_packages(params, credentials) -> StepOutput:
    packages = list(params.get("packages", []))
    return StepOutput(cloud_config={"packages": packages})


@register("install_snap_packages")
def compile_install_snap_packages(params, credentials) -> StepOutput:
    """Translate our `{name, classic}` snap list into cloud-init's
    `snap.commands` shape (each entry is a shell command string)."""
    snaps = list(params.get("snaps", []))
    commands: list[str] = []
    for s in snaps:
        cmd = ["snap", "install", s["name"]]
        if s.get("classic"):
            cmd.append("--classic")
        commands.append(" ".join(cmd))
    if not commands:
        return StepOutput()
    return StepOutput(cloud_config={"snap": {"commands": commands}})


@register("remove_apt_packages")
def compile_remove_apt_packages(params, credentials) -> StepOutput:
    packages = list(params.get("packages", []))
    if not packages:
        return StepOutput()
    cmds = [f"apt-get purge -y {pkg}" for pkg in packages]
    cmds.append("apt-get autoremove -y")
    cmds.append("apt-get clean")
    return StepOutput(runcmd=cmds)
