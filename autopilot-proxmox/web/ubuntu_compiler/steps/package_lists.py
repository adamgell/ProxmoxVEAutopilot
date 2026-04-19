"""install_apt_packages / install_snap_packages / remove_apt_packages."""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput

_CURTIN = "curtin in-target --target=/target --"


@register("install_apt_packages")
def compile_install_apt_packages(params, credentials) -> StepOutput:
    packages = list(params.get("packages", []))
    return StepOutput(autoinstall_body={"packages": packages})


@register("install_snap_packages")
def compile_install_snap_packages(params, credentials) -> StepOutput:
    snaps = list(params.get("snaps", []))
    return StepOutput(autoinstall_body={"snaps": snaps})


@register("remove_apt_packages")
def compile_remove_apt_packages(params, credentials) -> StepOutput:
    packages = list(params.get("packages", []))
    if not packages:
        return StepOutput()
    cmds = [f"{_CURTIN} apt-get purge -y {pkg}" for pkg in packages]
    cmds.append(f"{_CURTIN} apt-get autoremove -y")
    cmds.append(f"{_CURTIN} apt-get clean")
    return StepOutput(late_commands=cmds)
