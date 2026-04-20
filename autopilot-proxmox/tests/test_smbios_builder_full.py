"""Tests for the Type 0 + Type 1 + Type 3 multi-structure SMBIOS builder.

The whole point of build_full_smbios is to make Windows on the
sysprep'd clone read HP / EliteBook / our serial / our UUID instead of
QEMU's BOCHS_/BXPC____ defaults — which it does only if the file
contains all three types and Proxmox's smbios1 line is dropped from
the VM config. These tests validate the bytes are well-formed; the
"does Windows actually read them" assertion is a live-cluster test.
"""
import struct
import uuid


def test_type0_emits_24_byte_formatted_area_with_strings():
    """SMBIOS 2.4 Type 0 formatted area is 24 bytes (header + Major/Minor
    + EC release fields). Earlier 2.0 layout was 20 bytes; we use the
    newer one because Windows expects current-spec data."""
    from web.smbios_builder import build_type0_bios
    out = build_type0_bios(vendor="HP", bios_version="1.42", release_date="03/15/2025")
    assert len(out) >= 24 + 2  # formatted + at least the double-null
    assert out[0] == 0x00     # Type
    assert out[1] == 0x18     # Length = 24
    table = out[24:]
    assert b"HP\x00" in table
    assert b"1.42\x00" in table
    assert b"03/15/2025\x00" in table
    assert out.endswith(b"\x00\x00")


def test_type1_uuid_is_little_endian_per_smbios_2_6():
    from web.smbios_builder import build_type1_system, _uuid_bytes_le
    u = "FC718DFB-B852-4D00-BA75-2FA1B07AB6CC"
    out = build_type1_system(
        manufacturer="HP", product_name="HP EliteBook 860 G11",
        serial_number="Gell-E9C0C757", uuid_str=u,
        sku="A5FF", family="EliteBook",
    )
    assert out[0] == 0x01     # Type 1
    assert out[1] == 0x1B     # Length = 27
    # UUID lives at offset 8..23 of the formatted area.
    expected = _uuid_bytes_le(u)
    assert out[8:24] == expected
    # The exact little-endian encoding for FC718DFB-B852-4D00-... starts
    # with FB 8D 71 FC (time_low reversed) — sanity check.
    assert out[8:12] == bytes.fromhex("FB8D71FC")
    # Strings present in the table.
    table = out[27:]
    assert b"HP\x00" in table
    assert b"HP EliteBook 860 G11\x00" in table
    assert b"Gell-E9C0C757\x00" in table
    assert b"A5FF\x00" in table
    assert b"EliteBook\x00" in table


def test_type3_chassis_byte_is_at_offset_5():
    from web.smbios_builder import build_type3_chassis
    out = build_type3_chassis(chassis_type=10, manufacturer="HP",
                              serial_number="Gell-E9C0C757")
    assert out[0] == 0x03     # Type 3
    assert out[1] == 0x15     # Length = 21
    assert out[5] == 10       # Chassis Type byte


def test_full_smbios_concatenates_three_structures_in_order():
    from web.smbios_builder import build_full_smbios
    out = build_full_smbios(
        manufacturer="HP", product_name="HP EliteBook 860 G11",
        family="EliteBook", sku="A5FF",
        serial_number="Gell-E9C0C757",
        uuid_str="FC718DFB-B852-4D00-BA75-2FA1B07AB6CC",
        chassis_type=10,
    )
    # Walk the file, parsing structures one by one. Each starts with
    # type/length/handle, followed by formatted bytes, then a string
    # section that ends in a single \0\0.
    pos = 0
    types_seen = []
    while pos < len(out):
        t = out[pos]
        ln = out[pos + 1]
        # Skip past formatted area.
        end_formatted = pos + ln
        # Find the string-section terminator (double-null).
        i = end_formatted
        while i < len(out) - 1:
            if out[i] == 0 and out[i + 1] == 0:
                break
            i += 1
        end_strings = i + 2  # past the double-null
        types_seen.append(t)
        pos = end_strings
    assert types_seen == [0x00, 0x01, 0x03]


def test_full_smbios_round_trips_critical_fields():
    """Spot-check that the WMI-visible fields land at their SMBIOS
    offsets — Manufacturer/Model/Serial/UUID/Chassis Type."""
    from web.smbios_builder import build_full_smbios, _uuid_bytes_le
    u = "FC718DFB-B852-4D00-BA75-2FA1B07AB6CC"
    out = build_full_smbios(
        manufacturer="HP", product_name="HP EliteBook 860 G11",
        family="EliteBook", sku="A5FF",
        serial_number="Gell-E9C0C757", uuid_str=u, chassis_type=10,
    )
    # Type 1 starts after the Type 0 structure; find it.
    idx_type1 = out.find(b"\x01\x1B")
    assert idx_type1 > 0
    type1 = out[idx_type1:idx_type1 + 27]
    assert type1[8:24] == _uuid_bytes_le(u)
    # Type 3 chassis byte = 10.
    idx_type3 = out.find(b"\x03\x15", idx_type1)
    assert idx_type3 > idx_type1
    assert out[idx_type3 + 5] == 10
    # Strings are present somewhere in the file.
    assert b"HP EliteBook 860 G11\x00" in out
    assert b"Gell-E9C0C757\x00" in out


def test_chassis_type_validation():
    from web.smbios_builder import build_type3_chassis, build_full_smbios
    import pytest
    with pytest.raises(ValueError):
        build_type3_chassis(chassis_type=0)
    with pytest.raises(ValueError):
        build_type3_chassis(chassis_type=300)
    with pytest.raises(ValueError):
        build_full_smbios(
            manufacturer="HP", product_name="P",
            serial_number="s", uuid_str=str(uuid.uuid4()).upper(),
            chassis_type=0,
        )
