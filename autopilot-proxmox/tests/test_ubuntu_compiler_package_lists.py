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
    assert out.cloud_config["packages"] == ["curl", "git", "wget"]


def test_install_apt_packages_empty_list_emits_empty() -> None:
    out = compile_step("install_apt_packages", params={"packages": []}, credentials={})
    assert out.cloud_config["packages"] == []


def test_install_snap_packages_emits_snap_commands() -> None:
    out = compile_step(
        "install_snap_packages",
        params={"snaps": [
            {"name": "code", "classic": True},
            {"name": "postman"},
        ]},
        credentials={},
    )
    commands = out.cloud_config["snap"]["commands"]
    assert "snap install code --classic" in commands
    assert "snap install postman" in commands


def test_install_snap_without_classic_emits_plain_install() -> None:
    out = compile_step(
        "install_snap_packages",
        params={"snaps": [{"name": "postman"}]},
        credentials={},
    )
    assert out.cloud_config["snap"]["commands"] == ["snap install postman"]


def test_install_snap_empty_list_emits_nothing() -> None:
    out = compile_step(
        "install_snap_packages",
        params={"snaps": []},
        credentials={},
    )
    # Empty list => no snap block at all; nothing to merge.
    assert out.cloud_config == {}


def test_remove_apt_packages_emits_runcmd_purges() -> None:
    out = compile_step(
        "remove_apt_packages",
        params={"packages": ["libreoffice-common", "transmission-*"]},
        credentials={},
    )
    rc = out.runcmd
    assert any("apt-get purge -y libreoffice-common" in line for line in rc)
    assert any("apt-get purge -y transmission-*" in line for line in rc)
    assert any("apt-get autoremove -y" in line for line in rc)
    assert any("apt-get clean" in line for line in rc)
    # No curtin wrapping — cloud-init runs commands directly as root.
    assert all("curtin" not in line for line in rc)


def test_remove_apt_packages_empty_list_emits_nothing() -> None:
    out = compile_step("remove_apt_packages", params={"packages": []}, credentials={})
    assert out.runcmd == []
