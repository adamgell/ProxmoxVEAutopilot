"""Tests for web.utm_bundle - UTM .utm bundle generator and runtime control.

Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md
"""
import json
import pathlib
import subprocess
import sys


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


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
    (VirtIO), answer ISO CD (USB), virtio-win CD (USB) - in that order.
    Order matters: UTM assigns bootindex=N by drive-array position.
    Note: UTM's schema has no 'External' key - removable-ness is inferred
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
    assert d["Backend"] == "QEMU"  # UTMBackend rawValue; UTM rejects any other case


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
    """Hypervisor lives under QEMU, not System - see UTM source
    Configuration/UTMQemuConfigurationQEMU.swift."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert d["System"]["Architecture"] == "aarch64"
    assert d["QEMU"]["Hypervisor"] is True
    assert d["QEMU"]["UEFIBoot"] is True
    assert d["QEMU"]["TPMDevice"] is True
    assert d["QEMU"]["RTCLocalTime"] is True


def test_render_plist_every_key_exists_in_contract():
    """Contract-based assertion - every section key we emit must appear in
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
    """Snapshot test - ensures we don't accidentally shift the plist bytes
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


def _touch(path, size=1024):
    """Create a dummy file of the given byte size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * size)


def test_write_bundle_creates_expected_layout(tmp_path):
    from web import utm_bundle as ub

    # Stage fake ISOs and an efi_vars.fd source
    iso_dir = tmp_path / "isos"
    installer_iso = iso_dir / "Win11.iso"
    answer_iso    = iso_dir / "AUTOUNATTEND.iso"
    virtio_iso    = iso_dir / "virtio-win.iso"
    _touch(installer_iso)
    _touch(answer_iso)
    _touch(virtio_iso)
    efi_src = tmp_path / "efi-source.fd"
    _touch(efi_src)

    spec = _sample_win11_spec()
    bundle = tmp_path / "test-win11.utm"

    result = ub.write_bundle(
        spec,
        bundle_path=bundle,
        disk_size_gib=10,
        efi_vars_source=efi_src,
        iso_sources={
            "Win11_25H2_English_Arm64.iso": installer_iso,
            "AUTOUNATTEND.iso":             answer_iso,
            "virtio-win.iso":               virtio_iso,
        },
    )

    assert (bundle / "config.plist").is_file()
    assert (bundle / "Data").is_dir()
    assert (bundle / "Data" / "Win11_25H2_English_Arm64.iso").is_file()
    assert (bundle / "Data" / "AUTOUNATTEND.iso").is_file()
    assert (bundle / "Data" / "virtio-win.iso").is_file()
    assert (bundle / "Data" / "efi_vars.fd").is_file()
    # System disk uses the VirtIO drive's identifier as filename
    disk_filename = spec.drives[1].identifier.upper() + ".qcow2"
    assert (bundle / "Data" / disk_filename).is_file()

    # Return summary
    assert result["uuid"] == spec.uuid.upper()
    assert pathlib.Path(result["bundle_path"]) == bundle
    assert set(result["drive_uuids"]) == {d.identifier.upper() for d in spec.drives}


def test_write_bundle_plist_matches_renderer(tmp_path):
    """Bytes written to config.plist match render_plist_bytes exactly."""
    from web import utm_bundle as ub
    efi_src = tmp_path / "efi.fd"; _touch(efi_src)
    spec = _sample_win11_spec()
    bundle = tmp_path / "b.utm"
    ub.write_bundle(spec, bundle_path=bundle, disk_size_gib=10,
                    efi_vars_source=efi_src, iso_sources={})
    assert (bundle / "config.plist").read_bytes() == ub.render_plist_bytes(spec)


from unittest.mock import patch, MagicMock


def test_utmctl_register_opens_bundle_and_finds_uuid_in_list():
    """UTM 4.7.5's utmctl has no `register` subcommand - we register via
    `open -a UTM <bundle>` and then poll `utmctl list` to find the UUID
    UTM reports for that bundle name."""
    from web import utm_bundle as ub

    expected_uuid = "AAAA1111-2222-3333-4444-555555555555"
    list_stdout = (
        "UUID                                 Status   Name\n"
        "11111111-2222-3333-4444-555555555555 stopped  other-vm\n"
        f"{expected_uuid} stopped  smoke-vm\n"
    )

    def fake_run(argv, *args, **kwargs):
        if argv[:2] == ["/usr/bin/open", "-a"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv == ["/Applications/UTM.app/Contents/MacOS/utmctl", "list"]:
            return MagicMock(returncode=0, stdout=list_stdout, stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    with patch("web.utm_bundle.subprocess.run", side_effect=fake_run) as run:
        client = ub.UtmctlClient()
        uuid = client.register(pathlib.Path("/tmp/smoke-vm.utm"))

    # First call: open -a UTM <bundle>
    assert run.call_args_list[0].args[0] == [
        "/usr/bin/open", "-a", "UTM", "/tmp/smoke-vm.utm",
    ]
    # Second call: utmctl list (polling loop; succeeds on first poll here)
    assert run.call_args_list[1].args[0] == [
        "/Applications/UTM.app/Contents/MacOS/utmctl", "list",
    ]
    assert uuid == expected_uuid


def test_utmctl_register_raises_when_bundle_never_appears():
    """Register times out cleanly if `utmctl list` never shows the bundle."""
    from web import utm_bundle as ub

    def fake_run(argv, *args, **kwargs):
        if argv[:2] == ["/usr/bin/open", "-a"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[-1] == "list":
            return MagicMock(returncode=0, stdout="UUID Status Name\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    # Use tiny sleep to keep the test fast, small attempt count.
    with patch("web.utm_bundle.subprocess.run", side_effect=fake_run), \
         patch("web.utm_bundle.time.sleep"):
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="did not register"):
            ub.UtmctlClient().register(
                pathlib.Path("/tmp/never-there.utm"),
                poll_attempts=3, poll_delay=0.0,
            )


def test_utmctl_start_invokes_start_subcommand():
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake) as run:
        ub.UtmctlClient().start("AAAA1111-2222-3333-4444-555555555555")
    args, _ = run.call_args
    assert args[0][-2:] == ["start", "AAAA1111-2222-3333-4444-555555555555"]


def test_utmctl_status_returns_state_string():
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="started\n", stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake):
        state = ub.UtmctlClient().status("AAAA1111-2222-3333-4444-555555555555")
    assert state == "started"


def test_utmctl_delete_invokes_delete_subcommand():
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake) as run:
        ub.UtmctlClient().delete("AAAA1111-2222-3333-4444-555555555555")
    args, _ = run.call_args
    assert "delete" in args[0]


_UTM_STOCK_VARS = pathlib.Path(
    "/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd"
)


def test_prepare_efi_vars_writes_boot0000_entry(tmp_path):
    """prepare_efi_vars() bakes a Boot0000 + BootOrder entry into a copy
    of UTM's stock edk2-arm-secure-vars.fd so AAVMF boots straight into
    the autounattend ISO instead of dropping to the EFI shell.

    Skipped on hosts without UTM installed (CI, contributor laptops). The
    helper itself is exercised by the bundle integration test below; this
    test specifically asserts the post-write varstore contains the
    expected NVRAM variables (Boot0000 with our FilePath, BootOrder
    starting with 0x0000)."""
    import pytest as _pytest
    if not _UTM_STOCK_VARS.is_file():
        _pytest.skip("UTM not installed; no stock edk2-arm-secure-vars.fd")
    try:
        from virt.firmware.varstore import autodetect  # noqa: F401
    except ImportError:
        _pytest.skip("virt-firmware not installed")

    from web import utm_bundle as ub
    seed = tmp_path / "efi_vars.fd"
    import shutil as _shutil
    _shutil.copyfile(_UTM_STOCK_VARS, seed)

    ok = ub.prepare_efi_vars(seed)
    assert ok is True

    # Re-read the modified varstore and assert Boot0000 + BootOrder shape.
    from virt.firmware.varstore import autodetect as _ad
    varstore = _ad.open_varstore(str(seed))
    assert varstore is not None
    varlist = varstore.get_varlist()

    boot0000 = varlist.get("Boot0000")
    assert boot0000 is not None, "Boot0000 not present after prepare_efi_vars"
    boot0000_repr = boot0000.fmt_boot_entry()
    # Default boot target is cdboot_noprompt.efi (no "Press any key to
    # boot from CD" prompt), not bootaa64.efi (which chains through
    # BootMgr's prompt). Both files exist on the Win11 ARM64 install
    # ISO under their respective standard paths.
    assert "cdboot_noprompt.efi" in boot0000_repr.lower(), (
        f"Boot0000 devpath does not target cdboot_noprompt.efi: {boot0000_repr}"
    )

    # The device path must start with a USB Class wildcard node, not an
    # orphan FilePath. AAVMF rejects orphan-FilePath load options
    # (verified empirically on UTM 4.7.5: VM drops to EFI shell because
    # BdsExpandShortFormDevicePath cannot resolve a path that begins at
    # MEDIA without a preceding device handle). The USB Class node has
    # type=0x03 (Messaging), subtype=0x0F (USB Class), 11 bytes total.
    import struct as _struct
    raw_lo = bytes(boot0000.data)
    _attrs, fp_len = _struct.unpack_from("<IH", raw_lo, 0)
    # Skip past the 4-byte attrs + 2-byte fp_len + UCS-16 description
    _i = 6
    while True:
        _ch = _struct.unpack_from("<H", raw_lo, _i)[0]
        _i += 2
        if _ch == 0:
            break
    fpl = raw_lo[_i:_i + fp_len]
    first_node_type, first_node_subtype, first_node_len = (
        _struct.unpack_from("<BBH", fpl, 0)
    )
    assert (first_node_type, first_node_subtype) == (0x03, 0x0F), (
        f"Boot0000 first node must be USB-CLASS (0x03/0x0F), got "
        f"0x{first_node_type:02x}/0x{first_node_subtype:02x}. AAVMF will "
        f"reject orphan FilePath device paths."
    )
    assert first_node_len == 11, (
        f"USB Class node must be 11 bytes per UEFI 2.10 sec 10.3.5.10, "
        f"got {first_node_len}"
    )
    # Body: VendorId(2) ProductId(2) DeviceClass(1) SubClass(1) Protocol(1).
    vid, pid, dev_class, dev_sub, dev_proto = _struct.unpack_from(
        "<HHBBB", fpl, 4
    )
    assert (vid, pid) == (0xFFFF, 0xFFFF), (
        f"USB Class wildcard requires VID/PID = 0xFFFF/0xFFFF, got "
        f"0x{vid:04x}/0x{pid:04x}"
    )
    assert (dev_class, dev_sub, dev_proto) == (0x08, 0x06, 0x50), (
        f"USB Class must be Mass-Storage / SCSI-Transparent / Bulk-Only "
        f"(0x08/0x06/0x50) to match QEMU usb-storage devices, got "
        f"0x{dev_class:02x}/0x{dev_sub:02x}/0x{dev_proto:02x}"
    )

    boot_order = varlist.get("BootOrder")
    assert boot_order is not None, "BootOrder missing after prepare_efi_vars"
    raw = bytes(boot_order.data)
    assert len(raw) >= 2, "BootOrder has no entries"
    first_index = int.from_bytes(raw[:2], "little")
    assert first_index == 0x0000, (
        f"BootOrder must start with 0x0000 (our entry), got 0x{first_index:04X}"
    )


def test_prepare_efi_vars_returns_false_on_invalid_stub(tmp_path):
    """A 1024-byte zero blob is not a real EDK2 varstore; helper returns
    False rather than raising so write_bundle can fall back gracefully
    (legacy keystroke path) without crashing the orchestrator."""
    from web import utm_bundle as ub
    stub = tmp_path / "stub.fd"
    stub.write_bytes(b"\x00" * 1024)
    assert ub.prepare_efi_vars(stub) is False


def test_cli_build_writes_bundle(tmp_path):
    """Feed a full spec JSON to the CLI; bundle directory and files exist."""
    efi_src = tmp_path / "efi.fd"; _touch(efi_src)
    installer = tmp_path / "Win11.iso"; _touch(installer)
    spec_payload = {
        "name": "test-cli",
        "uuid": "22222222-2222-2222-2222-222222222222",
        "system": {},
        "qemu": {},
        "display": {},
        "network": {},
        "drives": [
            {"identifier": "aaaa0001-0000-0000-0000-000000000000",
             "image_type": "CD", "interface": "USB",
             "image_name": "Win11.iso"},
            {"identifier": "aaaa0002-0000-0000-0000-000000000000",
             "image_type": "Disk", "interface": "VirtIO",
             "image_name": "aaaa0002-0000-0000-0000-000000000000.qcow2"},
        ],
        "disk_size_gib": 5,
        "efi_vars_source": str(efi_src),
        "iso_sources": {"Win11.iso": str(installer)},
        "register": False,  # don't hit real UTM
    }
    bundle = tmp_path / "test-cli.utm"
    result = subprocess.run(
        [sys.executable, "-m", "web.utm_bundle", "build",
         "--spec", "-", "--out", str(bundle)],
        input=json.dumps(spec_payload),
        capture_output=True, text=True, check=True,
    )
    out = json.loads(result.stdout)
    assert out["uuid"] == "22222222-2222-2222-2222-222222222222"
    assert (bundle / "config.plist").is_file()
    assert (bundle / "Data" / "Win11.iso").is_file()
