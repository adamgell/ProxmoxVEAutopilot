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


def _sample_win11_spec():
    """Stable sample spec used by the renderer and golden-fixture tests.
    A fixed MAC address keeps the golden bytes reproducible; drive
    identifiers are intentionally deterministic for the same reason."""
    from web import utm_bundle as ub
    return ub.BundleSpec(
        name="test-win11",
        uuid="11111111-1111-1111-1111-111111111111",
        system=ub.SystemSpec(),
        qemu=ub.QemuSpec(),
        drives=[
            ub.DriveSpec(identifier="aaaa0001-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="Win11_25H2_English_Arm64.iso"),
            ub.DriveSpec(identifier="aaaa0002-0000-0000-0000-000000000000",
                         image_type="Disk", interface="VirtIO",
                         image_name="aaaa0002-0000-0000-0000-000000000000.qcow2"),
            ub.DriveSpec(identifier="aaaa0003-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="AUTOUNATTEND.iso"),
            ub.DriveSpec(identifier="aaaa0004-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="virtio-win.iso"),
        ],
        display=ub.DisplaySpec(),
        network=ub.NetworkSpec(mac_address="02:AA:BB:CC:DD:01"),
    )


def test_render_plist_has_required_top_level_keys():
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    for key in ("ConfigurationVersion", "Backend", "Information",
                "System", "QEMU", "Drive", "Display", "Network",
                "Input", "Sharing"):
        assert key in d, f"missing top-level key: {key}"
    assert d["ConfigurationVersion"] == 4
    assert d["Backend"] == "qemu"


def test_render_plist_uppercases_uuids():
    """UTM rejects mixed-case UUIDs; see commit 1eaa9d5."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert d["Information"]["UUID"] == "11111111-1111-1111-1111-111111111111".upper()
    for drive in d["Drive"]:
        assert drive["Identifier"] == drive["Identifier"].upper()


def test_render_plist_preserves_drive_order():
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert [dr["ImageName"] for dr in d["Drive"]] == [
        "Win11_25H2_English_Arm64.iso",
        "AAAA0002-0000-0000-0000-000000000000.qcow2",
        "AUTOUNATTEND.iso",
        "virtio-win.iso",
    ]


def test_render_plist_emits_win11_invariants():
    """Hypervisor lives under QEMU, not System — see UTM source
    Configuration/UTMQemuConfigurationQEMU.swift."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert d["System"]["Architecture"] == "aarch64"
    assert d["QEMU"]["Hypervisor"] is True
    assert d["QEMU"]["UEFIBoot"] is True
    assert d["QEMU"]["TPMDevice"] is True
    assert d["QEMU"]["RTCLocalTime"] is True


def test_render_plist_every_key_exists_in_contract():
    """Contract-based assertion — every section key we emit must appear in
    the extracted schema contract. If upstream UTM renames a key and we
    regenerate the contract, this catches any renderer drift. Note: this
    only catches *extra* keys; the E2E test catches missing required ones
    (UTM decode fails on register)."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    contract = json.loads((FIXTURES / "utm_schema_contract_v4.json").read_text())

    def _check(section_name: str, obj: dict, allowed: set[str]):
        for emitted in obj.keys():
            assert emitted in allowed, \
                f"{section_name}: emitted key '{emitted}' not in UTM contract"

    _check("Information", d["Information"],  set(contract["sections"]["Information"]))
    _check("System",      d["System"],       set(contract["sections"]["System"]))
    _check("QEMU",        d["QEMU"],         set(contract["sections"]["QEMU"]))
    _check("Input",       d["Input"],        set(contract["sections"]["Input"]))
    _check("Sharing",     d["Sharing"],      set(contract["sections"]["Sharing"]))
    _check("Display",     d["Display"][0],   set(contract["sections"]["Display"]))
    _check("Network",     d["Network"][0],   set(contract["sections"]["Network"]))
    for drive in d["Drive"]:
        _check("Drive",   drive,             set(contract["sections"]["Drive"]))


def test_render_plist_returns_bytes_when_asked():
    """render_plist_bytes() returns a plistlib-formatted XML plist."""
    from web import utm_bundle as ub
    data = ub.render_plist_bytes(_sample_win11_spec())
    assert isinstance(data, bytes)
    assert data.startswith(b'<?xml')
    assert b'<plist version="1.0">' in data
    assert b'<key>ConfigurationVersion</key>' in data


def test_render_plist_bytes_matches_golden_fixture():
    """Snapshot test — ensures we don't accidentally shift the plist bytes
    without noticing. Regenerate with:
        python -m web.utm_bundle _regenerate_golden_fixture
    (committed with a PR comment explaining the intentional change).
    """
    from web import utm_bundle as ub
    actual = ub.render_plist_bytes(_sample_win11_spec())
    expected = (FIXTURES / "win11_template_expected.plist").read_bytes()
    assert actual == expected, (
        "Rendered plist differs from golden fixture.\n"
        "If the change is intentional, regenerate the fixture via:\n"
        "    python -m web.utm_bundle _regenerate_golden_fixture\n"
        "and commit with a PR comment explaining why."
    )


def test_create_qcow2_writes_file_of_expected_size(tmp_path):
    """qemu-img create -f qcow2 <path> <size>G produces a qcow2 file. The
    file on disk is small (~200 KB) because qcow2 is sparse; the *virtual*
    size is what we assert."""
    from web import utm_bundle as ub
    disk = tmp_path / "test.qcow2"
    ub.create_qcow2(disk, virtual_size_gib=10)
    assert disk.is_file()
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(disk)],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(info.stdout)
    assert meta["virtual-size"] == 10 * 1024 ** 3
    assert meta["format"] == "qcow2"
