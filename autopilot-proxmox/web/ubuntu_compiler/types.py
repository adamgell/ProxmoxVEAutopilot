"""Type definitions for the Ubuntu step compiler."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class UbuntuCompileError(Exception):
    """Raised when a step cannot be compiled (missing credential, bad params)."""


@dataclass
class StepOutput:
    """One step's contribution to the compiled cloud-init user-data.

    We target plain cloud-init (`#cloud-config`) running on an Ubuntu cloud
    image — not subiquity autoinstall. Each step returns:

      - `cloud_config`: dict merged into the top-level cloud-config document.
        List keys like `packages` and `users` concatenate; `snap.commands`
        concatenates; scalars overwrite.
      - `runcmd`: commands appended to the top-level `runcmd:` list in step
        order. Cloud-init runs these as root on first boot (on the installed
        system directly — no curtin wrapping).
      - `firstboot_runcmd`: commands appended to the per-clone cloud-init
        seed's `runcmd:` (unchanged semantics — this is the clone's own
        cloud-init, not the template's).
    """

    cloud_config: dict[str, Any] = field(default_factory=dict)
    runcmd: list[str] = field(default_factory=list)
    firstboot_runcmd: list[str] = field(default_factory=list)
