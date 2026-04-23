"""Tests for web.utm_bundle — UTM .utm bundle generator and runtime control.

Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md
"""
import json
import pathlib
import subprocess
import sys


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_cli_build_echoes_spec_on_stdout(tmp_path):
    """The `build` CLI reads a spec JSON from stdin and echoes the UUID it
    received on stdout as JSON. This proves the Ansible↔Python handoff shape
    before any bundle-writing logic exists.
    """
    spec = {"name": "test", "uuid": "00000000-0000-0000-0000-000000000000"}
    result = subprocess.run(
        [sys.executable, "-m", "web.utm_bundle", "build",
         "--spec", "-", "--out", str(tmp_path / "test.utm")],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(result.stdout)
    assert out["uuid"] == spec["uuid"]


def test_schema_contract_has_required_sections():
    """The generated UTM schema contract lists PascalCase keys per section
    and known enum values. If upstream UTM renames a key we emit, the
    renderer tests will fail; this test just confirms the contract file
    itself has the shape we expect."""
    contract = json.loads((FIXTURES / "utm_schema_contract_v4.json").read_text())
    assert contract["ConfigurationVersion"] == 4
    for section in ("System", "QEMU", "Drive", "Display", "Network", "Information"):
        assert section in contract["sections"], f"missing section: {section}"
        assert isinstance(contract["sections"][section], list)
        assert len(contract["sections"][section]) > 0
    # Enum domains used by the renderer
    for enum_name in ("QEMUDriveInterface", "QEMUDriveImageType",
                      "QEMUArchitecture"):
        assert enum_name in contract["enums"]
        assert isinstance(contract["enums"][enum_name], list)
        assert len(contract["enums"][enum_name]) > 0


def test_bundle_spec_win11_template_has_four_drives():
    """Win11 ARM64 template bundle has: installer CD (USB), system qcow2
    (VirtIO), answer ISO CD (USB), virtio-win CD (USB) — in that order.
    Order matters: UTM assigns bootindex=N by drive-array position.
    Note: UTM's schema has no 'External' key — removable-ness is inferred
    from ImageType=CD at decode time."""
    from web import utm_bundle as ub
    spec = ub.BundleSpec(
        name="test-win11",
        uuid="11111111-1111-1111-1111-111111111111",
        system=ub.SystemSpec(),
        qemu=ub.QemuSpec(),
        drives=[
            ub.DriveSpec(identifier="AAAA0001-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="Win11_25H2_English_Arm64.iso"),
            ub.DriveSpec(identifier="AAAA0002-0000-0000-0000-000000000000",
                         image_type="Disk", interface="VirtIO",
                         image_name="AAAA0002-0000-0000-0000-000000000000.qcow2"),
            ub.DriveSpec(identifier="AAAA0003-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="AUTOUNATTEND.iso"),
            ub.DriveSpec(identifier="AAAA0004-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="virtio-win.iso"),
        ],
        display=ub.DisplaySpec(),
        network=ub.NetworkSpec(),
    )
    assert len(spec.drives) == 4
    assert spec.drives[0].image_name.endswith(".iso")
    assert spec.drives[1].image_type == "Disk"
    assert spec.drives[1].interface == "VirtIO"


def test_qemu_spec_defaults_for_windows():
    """Windows 11 ARM64 requires TPM and wants local-time RTC; UEFI boot."""
    from web import utm_bundle as ub
    q = ub.QemuSpec()
    assert q.uefi_boot is True
    assert q.tpm_device is True
    assert q.rtc_local_time is True
    assert q.rng_device is True
    assert q.balloon_device is False


def test_system_spec_defaults_are_arm64_virt_hvf():
    from web import utm_bundle as ub
    s = ub.SystemSpec()
    assert s.architecture == "aarch64"
    assert s.target == "virt"
    assert s.use_hypervisor is True
    assert s.memory_mib == 8192
    assert s.cpu_count == 4
