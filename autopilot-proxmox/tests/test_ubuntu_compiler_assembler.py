"""Assembler: merge steps → cloud-init user-data + meta-data."""
from __future__ import annotations

from ruamel.yaml import YAML

from web.ubuntu_compiler import compile_sequence


_yaml = YAML(typ="safe")


def _parse(s: str) -> dict:
    return _yaml.load(s)


def test_empty_sequence_still_produces_valid_cloud_config() -> None:
    u, m, fu, fm = compile_sequence(steps=[], credentials={}, instance_id="test-1",
                                    hostname="autopilot-abc")
    # Must start with #cloud-config and parse to an (empty-ish) dict.
    assert u.lstrip().startswith("#cloud-config")
    doc = _parse(u) or {}
    # There is no autoinstall wrapper any more.
    assert "autoinstall" not in doc
    # meta-data carries instance-id
    mdoc = _parse(m)
    assert mdoc["instance-id"] == "test-1"


def test_ubuntu_core_plus_packages_merges() -> None:
    steps = [
        {"step_type": "install_ubuntu_core", "params": {}},
        {"step_type": "install_apt_packages", "params": {"packages": ["curl", "git"]}},
        {"step_type": "install_apt_packages", "params": {"packages": ["wget"]}},
    ]
    u, _, _, _ = compile_sequence(steps=steps, credentials={}, instance_id="i-1",
                                  hostname="h")
    doc = _parse(u)
    # Top-level keys, no autoinstall wrapper.
    assert doc["locale"] == "en_US.UTF-8"
    assert doc["timezone"] == "UTC"
    # install_ubuntu_core baselines qemu-guest-agent; later steps append.
    assert doc["packages"] == ["qemu-guest-agent", "curl", "git", "wget"]


def test_runcmd_concatenates_across_steps() -> None:
    steps = [
        {"step_type": "install_ubuntu_core", "params": {}},
        {"step_type": "install_intune_portal", "params": {}},
        {"step_type": "install_edge", "params": {}},
    ]
    u, _, _, _ = compile_sequence(steps=steps, credentials={}, instance_id="i-1",
                                  hostname="h")
    doc = _parse(u)
    rc = doc["runcmd"]
    assert any("intune-portal" in line for line in rc)
    assert any("microsoft-edge-stable" in line for line in rc)
    # Intune comes before Edge because steps preserve order.
    intune_idx = next(i for i, line in enumerate(rc) if "intune-portal" in line)
    edge_idx = next(i for i, line in enumerate(rc) if "microsoft-edge-stable" in line)
    assert intune_idx < edge_idx


def test_snap_commands_concatenate_across_steps() -> None:
    steps = [
        {"step_type": "install_snap_packages",
         "params": {"snaps": [{"name": "code", "classic": True}]}},
        {"step_type": "install_snap_packages",
         "params": {"snaps": [{"name": "postman"}]}},
    ]
    u, _, _, _ = compile_sequence(steps=steps, credentials={}, instance_id="i-1",
                                  hostname="h")
    doc = _parse(u)
    assert doc["snap"]["commands"] == [
        "snap install code --classic",
        "snap install postman",
    ]


def test_firstboot_cloud_init_includes_hostname_and_runcmd() -> None:
    steps = [
        {"step_type": "install_ubuntu_core", "params": {}},
        {"step_type": "run_firstboot_script", "params": {"command": "touch /tmp/ok"}},
    ]
    _, _, fu, _ = compile_sequence(steps=steps, credentials={}, instance_id="i-1",
                                   hostname="autopilot-xyz")
    doc = _parse(fu)
    # Per-clone cloud-init sets hostname and runs runcmd on first boot.
    assert doc["hostname"] == "autopilot-xyz"
    assert "touch /tmp/ok" in doc["runcmd"]
    # Per-clone cloud-init also runs the qemu-guest-agent safety install.
    joined = "\n".join(doc["runcmd"])
    assert "qemu-guest-agent" in joined


def test_disabled_steps_are_skipped() -> None:
    steps = [
        {"step_type": "install_ubuntu_core", "params": {}},
        {"step_type": "install_apt_packages", "params": {"packages": ["curl"]}, "enabled": False},
    ]
    u, _, _, _ = compile_sequence(steps=steps, credentials={}, instance_id="i-1",
                                  hostname="h")
    doc = _parse(u)
    # The disabled install_apt_packages step's "curl" must not appear.
    # install_ubuntu_core still contributes qemu-guest-agent as baseline.
    assert doc["packages"] == ["qemu-guest-agent"]
