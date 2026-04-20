"""install_ubuntu_core step compiler."""
from __future__ import annotations

import pytest

from web.ubuntu_compiler import compile_step


def test_default_params_produce_en_us_utc() -> None:
    out = compile_step("install_ubuntu_core", params={}, credentials={})
    body = out.cloud_config
    assert body["locale"] == "en_US.UTF-8"
    assert body["timezone"] == "UTC"
    # Cloud image handles keyboard/storage/ssh/updates defaults, so they
    # are intentionally absent from the compiled cloud-config.
    assert "keyboard" not in body
    assert "storage" not in body
    assert "ssh" not in body
    assert "updates" not in body
    assert "shutdown" not in body
    # apt is refreshed before the first packages install on a cloud image.
    assert body["package_update"] is True
    assert body["package_upgrade"] is False
    # qemu-guest-agent is always installed as part of the core baseline.
    assert "qemu-guest-agent" in body["packages"]


def test_timezone_override() -> None:
    out = compile_step(
        "install_ubuntu_core",
        params={"timezone": "America/New_York"},
        credentials={},
    )
    assert out.cloud_config["timezone"] == "America/New_York"


def test_keyboard_layout_param_is_accepted_but_ignored() -> None:
    # Backwards-compat: existing seeded sequences pass keyboard_layout. The
    # param is tolerated without raising but does not appear in the output.
    out = compile_step(
        "install_ubuntu_core",
        params={"keyboard_layout": "de"},
        credentials={},
    )
    assert "keyboard" not in out.cloud_config


def test_emits_qemu_guest_agent_enable_runcmd() -> None:
    """Even if `packages:` says qemu-guest-agent, the runcmd ensures the
    systemd unit is enabled + started so Proxmox's agent API works on
    cloud images that ship the package but leave it disabled."""
    out = compile_step("install_ubuntu_core", params={}, credentials={})
    joined = "\n".join(out.runcmd)
    assert "qemu-guest-agent" in joined
    assert out.firstboot_runcmd == []
