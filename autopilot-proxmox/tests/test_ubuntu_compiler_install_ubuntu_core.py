"""install_ubuntu_core step compiler."""
from __future__ import annotations

import pytest

from web.ubuntu_compiler import compile_step


def test_default_params_produce_en_us_utc_lvm() -> None:
    out = compile_step("install_ubuntu_core", params={}, credentials={})
    body = out.autoinstall_body
    assert body["version"] == 1
    assert body["locale"] == "en_US.UTF-8"
    assert body["timezone"] == "UTC"
    assert body["keyboard"] == {"layout": "us"}
    assert body["storage"] == {"layout": {"name": "lvm"}}
    assert body["updates"] == "security"
    assert body["shutdown"] == "poweroff"
    # SSH server stays off by default — this is a workstation image.
    assert body["ssh"] == {"install-server": False}


def test_timezone_override() -> None:
    out = compile_step(
        "install_ubuntu_core",
        params={"timezone": "America/New_York"},
        credentials={},
    )
    assert out.autoinstall_body["timezone"] == "America/New_York"


def test_keyboard_layout_override() -> None:
    out = compile_step(
        "install_ubuntu_core",
        params={"keyboard_layout": "de"},
        credentials={},
    )
    assert out.autoinstall_body["keyboard"] == {"layout": "de"}


def test_emits_qemu_guest_agent_install_late_commands() -> None:
    """Belt-and-suspenders: explicit apt install of qemu-guest-agent in
    late-commands so Proxmox's agent API is reachable after first boot even
    if subiquity's packages: processing silently skipped the install."""
    out = compile_step("install_ubuntu_core", params={}, credentials={})
    joined = "\n".join(out.late_commands)
    assert "apt-get install -y qemu-guest-agent" in joined
    assert "systemctl enable qemu-guest-agent" in joined
    assert out.firstboot_runcmd == []
