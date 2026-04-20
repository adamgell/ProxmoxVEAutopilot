"""install_ubuntu_core: locale, timezone, keyboard, LVM storage layout."""
from __future__ import annotations

from ..registry import register
from ..types import StepOutput


@register("install_ubuntu_core")
def compile_install_ubuntu_core(params, credentials) -> StepOutput:
    locale = params.get("locale", "en_US.UTF-8")
    timezone = params.get("timezone", "UTC")
    keyboard_layout = params.get("keyboard_layout", "us")
    storage_layout = params.get("storage_layout", "lvm")

    return StepOutput(
        autoinstall_body={
            "version": 1,
            "locale": locale,
            "timezone": timezone,
            "keyboard": {"layout": keyboard_layout},
            "storage": {"layout": {"name": storage_layout}},
            "updates": "security",
            "shutdown": "poweroff",
            "ssh": {"install-server": False},
            # qemu-guest-agent is required by Proxmox's agent/exec API.
            "packages": ["qemu-guest-agent"],
        },
        # Belt-and-suspenders: even if subiquity's `packages:` processing
        # skipped the install (sometimes happens when the install environment
        # has sporadic connectivity), install + enable the agent explicitly
        # in the target before reboot. Curtin in-target runs during the
        # install, so apt has network and can fetch from the main archive.
        late_commands=[
            "curtin in-target --target=/target -- apt-get update",
            "curtin in-target --target=/target -- apt-get install -y qemu-guest-agent",
            "curtin in-target --target=/target -- systemctl enable qemu-guest-agent",
        ],
    )
