"""Type definitions for the Ubuntu step compiler."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class UbuntuCompileError(Exception):
    """Raised when a step cannot be compiled (missing credential, bad params)."""


@dataclass
class StepOutput:
    """One step's contribution to the compiled autoinstall + cloud-init."""

    # Dict merged into the autoinstall: root (keys overwrite; list values like
    # packages / snaps are concatenated with any prior step's contribution).
    autoinstall_body: dict[str, Any] = field(default_factory=dict)
    # Appended to autoinstall.late-commands in step order.
    late_commands: list[str] = field(default_factory=list)
    # Appended to the per-clone cloud-init runcmd in step order.
    firstboot_runcmd: list[str] = field(default_factory=list)
