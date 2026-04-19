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


def test_no_late_commands_or_firstboot() -> None:
    out = compile_step("install_ubuntu_core", params={}, credentials={})
    assert out.late_commands == []
    assert out.firstboot_runcmd == []
