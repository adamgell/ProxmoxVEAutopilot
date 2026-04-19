"""install_apt_packages / install_snap_packages / remove_apt_packages."""
from __future__ import annotations

import pytest

from web.ubuntu_compiler import compile_step


def test_install_apt_packages_emits_packages_list() -> None:
    out = compile_step(
        "install_apt_packages",
        params={"packages": ["curl", "git", "wget"]},
        credentials={},
    )
    assert out.autoinstall_body["packages"] == ["curl", "git", "wget"]


def test_install_apt_packages_empty_list_emits_empty() -> None:
    out = compile_step("install_apt_packages", params={"packages": []}, credentials={})
    assert out.autoinstall_body["packages"] == []


def test_install_snap_packages_emits_snap_dicts() -> None:
    out = compile_step(
        "install_snap_packages",
        params={"snaps": [
            {"name": "code", "classic": True},
            {"name": "postman"},
        ]},
        credentials={},
    )
    snaps = out.autoinstall_body["snaps"]
    assert {"name": "code", "classic": True} in snaps
    assert {"name": "postman"} in snaps


def test_install_snap_defaults_classic_to_false_if_absent() -> None:
    out = compile_step(
        "install_snap_packages",
        params={"snaps": [{"name": "postman"}]},
        credentials={},
    )
    # We pass through as-given; absence of classic means Snap treats as strict.
    assert out.autoinstall_body["snaps"] == [{"name": "postman"}]


def test_remove_apt_packages_emits_late_command_purges() -> None:
    out = compile_step(
        "remove_apt_packages",
        params={"packages": ["libreoffice-common", "transmission-*"]},
        credentials={},
    )
    lc = out.late_commands
    # One curtin in-target line per package, plus a final autoremove + clean.
    assert any("apt-get purge -y libreoffice-common" in line for line in lc)
    assert any("apt-get purge -y transmission-*" in line for line in lc)
    assert any("apt-get autoremove -y" in line for line in lc)
    assert any("apt-get clean" in line for line in lc)


def test_remove_apt_packages_empty_list_emits_nothing() -> None:
    out = compile_step("remove_apt_packages", params={"packages": []}, credentials={})
    assert out.late_commands == []
