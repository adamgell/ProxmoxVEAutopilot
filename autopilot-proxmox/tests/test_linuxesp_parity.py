"""LinuxESP parity: the seeded sequence compiles to a cloud-init document
that matches the snapshot exactly."""
from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from web.ubuntu_compiler import compile_sequence


_FIXTURE = Path(__file__).parent / "fixtures" / "linuxesp-snapshot.yaml"


def test_seeded_linuxesp_sequence_matches_snapshot() -> None:
    # Match the seeded sequence shape exactly (see seed_defaults, Task 14).
    steps = [
        {"step_type": "install_ubuntu_core",
         "params": {"locale": "en_US.UTF-8", "timezone": "UTC",
                    "keyboard_layout": "us", "storage_layout": "lvm"}},
        {"step_type": "install_apt_packages",
         "params": {"packages": ["curl", "git", "wget", "gpg"]}},
        {"step_type": "install_snap_packages",
         "params": {"snaps": [
             {"name": "code", "classic": True},
             {"name": "postman"},
             {"name": "powershell", "classic": True},
         ]}},
        {"step_type": "install_intune_portal", "params": {}},
        {"step_type": "install_edge", "params": {}},
        {"step_type": "remove_apt_packages",
         "params": {"packages": ["libreoffice-common", "libreoffice*",
                                 "remmina*", "transmission*"]}},
    ]
    u, _, _, _ = compile_sequence(
        steps=steps, credentials={}, instance_id="snap-1", hostname="h"
    )
    actual = YAML(typ="safe").load(u)

    with _FIXTURE.open("r", encoding="utf-8") as fh:
        expected = YAML(typ="safe").load(fh)

    # Top-level cloud-config keys we guarantee (no autoinstall wrapper).
    for key in ("locale", "timezone", "package_update", "package_upgrade",
                "packages", "snap"):
        assert actual.get(key) == expected.get(key), f"drift in {key}"

    # runcmd must match exactly (order-sensitive).
    assert actual["runcmd"] == expected["runcmd"]
