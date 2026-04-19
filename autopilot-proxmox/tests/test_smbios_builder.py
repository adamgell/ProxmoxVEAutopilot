"""Tests for web.smbios_builder — produces SMBIOS Type 3 (chassis) binary.

Reference: DMTF SMBIOS 2.7 spec §7.4 (System Enclosure or Chassis).
"""
import pytest


def test_build_type3_returns_bytes():
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert isinstance(out, bytes)
    assert len(out) > 0


def test_build_type3_structure_header():
    """First 4 bytes: type=3, length=0x15, handle=0x0300 little-endian."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[0] == 0x03          # Type field
    assert out[1] == 0x15          # Formatted-area length (21 bytes)
    assert out[2:4] == b"\x00\x03" # Handle 0x0300 little-endian


def test_build_type3_chassis_byte_at_offset_5():
    """The chassis type enum lives at byte 5 of the formatted area.
    This is the ONLY byte WMI Win32_SystemEnclosure.ChassisTypes reads
    that this builder controls — every other byte defaults to 'safe'
    or 'unspecified' values."""
    from web import smbios_builder
    for chassis in (3, 8, 9, 10, 14, 15, 30, 31, 32, 35):
        out = smbios_builder.build_type3_chassis(chassis_type=chassis)
        assert out[5] == chassis, f"chassis={chassis} not at byte 5"


def test_build_type3_string_indices_point_into_string_section():
    """Bytes 4, 6, 7, 8 are string-set indices for manufacturer,
    version, serial, asset tag. 1-based. Zero means 'no string'."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[4] == 1   # manufacturer string index
    assert out[6] == 2   # version string index
    assert out[7] == 3   # serial string index
    assert out[8] == 4   # asset tag string index


def test_build_type3_states_and_security():
    """Bytes 9-12: Boot-up / Power Supply / Thermal / Security — all
    set to 3 = 'Safe'/'None' which is the SMBIOS-standard value for
    'not reporting'."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[9] == 3
    assert out[10] == 3
    assert out[11] == 3
    assert out[12] == 3


def test_build_type3_oem_defined_and_trailing_fields():
    """Bytes 13-16: OEM Defined (4 bytes, 0).
    Byte 17: Height (0 = unspecified).
    Byte 18: Number of Power Cords (0 = unspecified).
    Bytes 19-20: Contained Element Count + Record Length (both 0)."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    assert out[13:17] == b"\x00\x00\x00\x00"
    assert out[17] == 0
    assert out[18] == 0
    assert out[19] == 0
    assert out[20] == 0


def test_build_type3_strings_section_double_null_terminated():
    """After the 21-byte formatted area: a set of null-terminated
    strings referenced by the string indices, then an additional null
    terminating the structure (so the section ends with \\0\\0)."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    strings = out[21:]
    # Must end with double-null
    assert strings.endswith(b"\x00\x00")
    # Must contain exactly 4 null-terminated strings (manufacturer,
    # version, serial, asset tag) + terminating null.
    non_empty = [s for s in strings.rstrip(b"\x00").split(b"\x00") if s]
    assert len(non_empty) == 4


def test_build_type3_default_string_values():
    """Default strings are deliberately vendor-neutral so the Type 3
    output merges cleanly with the Type 1 data Proxmox already sets
    (manufacturer/product come from type=1 which QEMU emits separately)."""
    from web import smbios_builder
    out = smbios_builder.build_type3_chassis(chassis_type=10)
    strings = out[21:].rstrip(b"\x00").split(b"\x00")
    # Order matches index 1..4
    assert strings[0] == b"QEMU"
    assert strings[1] == b"1.0"
    assert strings[2] == b"0"
    assert strings[3] == b""  # asset tag intentionally empty


def test_build_type3_rejects_invalid_chassis_type():
    from web import smbios_builder
    with pytest.raises(ValueError):
        smbios_builder.build_type3_chassis(chassis_type=0)
    with pytest.raises(ValueError):
        smbios_builder.build_type3_chassis(chassis_type=256)
    with pytest.raises(ValueError):
        smbios_builder.build_type3_chassis(chassis_type=-1)


def test_build_type3_total_length_is_deterministic():
    """Same input → same bytes. No randomness, no timestamps."""
    from web import smbios_builder
    a = smbios_builder.build_type3_chassis(chassis_type=10)
    b = smbios_builder.build_type3_chassis(chassis_type=10)
    assert a == b
