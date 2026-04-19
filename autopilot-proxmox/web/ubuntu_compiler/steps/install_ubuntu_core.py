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
        },
    )
