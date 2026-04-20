"""install_desktop_environment step compiler."""
from __future__ import annotations

import pytest

from web.ubuntu_compiler import compile_step, UbuntuCompileError


def test_default_flavor_is_ubuntu_desktop() -> None:
    out = compile_step("install_desktop_environment", params={}, credentials={})
    assert "ubuntu-desktop" in out.cloud_config["packages"]
    # Boot into GUI on first start.
    assert any("graphical.target" in line for line in out.runcmd)


def test_explicit_flavor_accepted() -> None:
    out = compile_step(
        "install_desktop_environment",
        params={"flavor": "xubuntu-desktop"},
        credentials={},
    )
    assert out.cloud_config["packages"] == ["xubuntu-desktop"]


def test_unknown_flavor_raises() -> None:
    with pytest.raises(UbuntuCompileError):
        compile_step(
            "install_desktop_environment",
            params={"flavor": "not-a-real-desktop"},
            credentials={},
        )


def test_desktop_packages_concatenate_with_apt_packages() -> None:
    """The assembler should concatenate packages from install_ubuntu_core,
    install_apt_packages, and install_desktop_environment into a single list."""
    from web.ubuntu_compiler import compile_sequence

    steps = [
        {"step_type": "install_ubuntu_core", "params": {}},
        {"step_type": "install_apt_packages", "params": {"packages": ["git"]}},
        {"step_type": "install_desktop_environment", "params": {}},
    ]
    u, _, _, _ = compile_sequence(
        steps=steps, credentials={}, instance_id="t", hostname="t"
    )
    from ruamel.yaml import YAML
    doc = YAML(typ="safe").load(u)
    pkgs = doc["packages"]
    assert "qemu-guest-agent" in pkgs  # from install_ubuntu_core
    assert "git" in pkgs                # from install_apt_packages
    assert "ubuntu-desktop" in pkgs     # from install_desktop_environment
