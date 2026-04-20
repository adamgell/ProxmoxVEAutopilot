"""install_desktop_environment step compiler."""
from __future__ import annotations

import pytest

from web.ubuntu_compiler import compile_step, UbuntuCompileError


def test_default_flavor_is_ubuntu_desktop() -> None:
    out = compile_step("install_desktop_environment", params={}, credentials={})
    joined = "\n".join(out.runcmd)
    # Install via runcmd (not cloud-init packages: — unreliable for huge
    # metapackages). Also sets graphical target so VM boots to GUI.
    assert "apt-get install -y ubuntu-desktop" in joined
    assert "graphical.target" in joined
    # No packages list — intentionally not using cloud-init packages module.
    assert out.cloud_config == {}


def test_explicit_flavor_accepted() -> None:
    out = compile_step(
        "install_desktop_environment",
        params={"flavor": "xubuntu-desktop"},
        credentials={},
    )
    assert any("apt-get install -y xubuntu-desktop" in line for line in out.runcmd)


def test_unknown_flavor_raises() -> None:
    with pytest.raises(UbuntuCompileError):
        compile_step(
            "install_desktop_environment",
            params={"flavor": "not-a-real-desktop"},
            credentials={},
        )


def test_desktop_runcmd_concatenates_with_other_steps() -> None:
    """Other steps' runcmd + packages still flow into the cloud-config; the
    desktop install is appended as runcmd entries in step order."""
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
    # ubuntu-desktop is in runcmd, not packages.
    assert "ubuntu-desktop" not in pkgs
    joined_runcmd = "\n".join(doc["runcmd"])
    assert "apt-get install -y ubuntu-desktop" in joined_runcmd
