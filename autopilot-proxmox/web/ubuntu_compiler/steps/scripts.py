"""run_late_command / run_firstboot_script: arbitrary shell steps."""
from __future__ import annotations

import shlex

from ..registry import register
from ..types import StepOutput, UbuntuCompileError


@register("run_late_command")
def compile_run_late_command(params, credentials) -> StepOutput:
    """A "late command" in the cloud-init world is just a runcmd entry. We
    keep the step-type name for backwards-compat with seeded sequences."""
    cmd = params.get("command")
    if not cmd:
        raise UbuntuCompileError("run_late_command: params.command is required")
    # Wrap in sh -c so multi-line / shell-y commands behave uniformly.
    return StepOutput(runcmd=[f"sh -c {shlex.quote(cmd)}"])


@register("run_firstboot_script")
def compile_run_firstboot_script(params, credentials) -> StepOutput:
    cmd = params.get("command")
    if not cmd:
        raise UbuntuCompileError("run_firstboot_script: params.command is required")
    # Multi-line commands: wrap in sh -c so each runcmd entry is one logical unit.
    if "\n" in cmd:
        wrapped = f"sh -c {shlex.quote(cmd)}"
    else:
        wrapped = cmd
    return StepOutput(firstboot_runcmd=[wrapped])
