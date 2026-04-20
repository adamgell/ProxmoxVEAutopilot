"""Build SMBIOS binaries.

Two usage modes:

1. :func:`build_type3_chassis` — single Type 3 (System Enclosure) structure
   with a configurable ``Chassis Type`` enum byte. Used for the legacy
   per-chassis-type pre-seeded snippet binaries (kept for backward
   compatibility with older templates).

2. :func:`build_full_smbios` — Type 0 (BIOS) + Type 1 (System) + Type 3
   (Chassis) in a single file. The per-VM replacement for Proxmox's
   ``smbios1`` config line PLUS the Type 3 chassis binary. Used when a
   provision needs to override chassis_type, because QEMU's CLI
   ``-smbios type=3`` option does not accept ``chassis_type`` in any
   released version (see GitLab qemu-project/qemu#2769), and a
   ``-smbios file=<type-3-only>`` has been observed to drop QEMU's
   auto-generated Type 0 and Type 1 — the resulting VM reports
   Manufacturer=BOCHS_ / Model=BXPC____ / empty serial / random UUID
   in WMI and Intune. By putting Type 0, 1, AND 3 in the file, the
   file owns those structures and QEMU fills in the rest (Type 2 /
   Type 4 / memory types) from its defaults.

Reference: DMTF SMBIOS 3.7 (or 2.6+ where noted), section numbers
called out inline.
"""
from __future__ import annotations

import struct
import uuid as _uuid
from typing import Optional

# Fixed handles — SMBIOS handles just need to be unique across the
# structures emitted together. Proxmox/QEMU default structures sit in
# 0x0000–0x00FF for Type 0/1, 0x0100-range for Type 2, etc. Keep a
# clear band above those.
_TYPE0_HANDLE = 0x0000
_TYPE1_HANDLE = 0x0100
_TYPE3_HANDLE = 0x0300


def _string_table(strings: list) -> bytes:
    """Build the SMBIOS unformatted string section for one structure.

    Each string is null-terminated. The section ends with an extra
    null byte (making the last string's terminator + the section
    terminator a double-null). If the list is empty, the section is
    just two null bytes.
    """
    if not strings:
        return b"\x00\x00"
    out = b""
    for s in strings:
        # Empty strings are legal but need to be emitted as a single
        # null (= lone terminator) — but index-referenced empty strings
        # shouldn't normally be used; we substitute " " if the caller
        # passed "" so the string table stays non-ambiguous.
        if s == "":
            s = " "
        out += s.encode("utf-8") + b"\x00"
    out += b"\x00"  # terminate the string section
    return out


def _str_index(strings: list, value: Optional[str]) -> int:
    """Append ``value`` to ``strings`` (deduping) and return its 1-based
    SMBIOS string index. Returns 0 when ``value`` is None or empty —
    SMBIOS uses 0 to mean "no string."
    """
    if value is None or value == "":
        return 0
    if value in strings:
        return strings.index(value) + 1
    strings.append(value)
    return len(strings)


def build_type0_bios(*, vendor: str = "Autopilot",
                     bios_version: str = "1.0",
                     release_date: str = "01/01/2026") -> bytes:
    """Type 0 — BIOS Information (SMBIOS 2.4+, 20-byte formatted area).

    Mirrors what a physical OEM BIOS reports. Windows Win32_BIOS reads
    Manufacturer/SMBIOSBIOSVersion/ReleaseDate from this structure.
    """
    strings: list = []
    s_vendor = _str_index(strings, vendor)
    s_version = _str_index(strings, bios_version)
    s_date = _str_index(strings, release_date)

    formatted = struct.pack(
        "<BBH"     # Type, Length, Handle
        "BBHB"     # Vendor, Version, Start Addr Segment, Release Date idx
        "B"        # ROM Size (encoded: (size_in_64KB / 64) - 1; 0xFF = extended)
        "Q"        # BIOS Characteristics (8 bytes of flags)
        "BB"       # BIOS Characteristics Extension (2 bytes)
        "BBBB",    # Major Release, Minor Release, EC Major, EC Minor
        0x00,      # Type = 0
        0x18,      # Length = 24 bytes (SMBIOS 2.4 with Major/Minor + EC)
        _TYPE0_HANDLE,
        s_vendor,
        s_version,
        0xE800,    # Start Addr Segment — conventional BIOS base segment
        s_date,
        0x00,      # ROM Size = 64 KB (minimal, QEMU doesn't care)
        0x0000_0000_0008_0000,  # Characteristics: Bit 19 "UEFI is supported"
        0x03, 0x03,  # Extension: UEFI boot + Target content distribution
        0x01, 0x00,  # BIOS major=1, minor=0
        0x00, 0x00,  # EC release not applicable
    )
    assert len(formatted) == 24
    return formatted + _string_table(strings)


def _uuid_bytes_le(uuid_str: str) -> bytes:
    """Encode a UUID for the SMBIOS Type 1 UUID field per spec 2.6+.

    The first three components (time_low, time_mid, time_hi_and_version)
    are little-endian; the last two (clock_seq + node) are big-endian.
    Python's :attr:`uuid.UUID.bytes_le` does exactly this.
    """
    return _uuid.UUID(uuid_str).bytes_le


def build_type1_system(*, manufacturer: str, product_name: str,
                       version: str = "",
                       serial_number: str,
                       uuid_str: str,
                       sku: str = "",
                       family: str = "") -> bytes:
    """Type 1 — System Information (SMBIOS 2.4+, 27-byte formatted area).

    WMI maps:
      Win32_ComputerSystem.Manufacturer  ← Manufacturer
      Win32_ComputerSystem.Model         ← ProductName
      Win32_ComputerSystemProduct.*      ← all fields
      Win32_BIOS.SerialNumber            ← SerialNumber
    """
    strings: list = []
    s_mfr = _str_index(strings, manufacturer)
    s_prod = _str_index(strings, product_name)
    s_ver = _str_index(strings, version)
    s_ser = _str_index(strings, serial_number)
    s_sku = _str_index(strings, sku)
    s_fam = _str_index(strings, family)

    formatted = struct.pack(
        "<BBH"      # Type, Length, Handle
        "BBBB"      # Manufacturer, Product, Version, Serial
        "16s"       # UUID
        "B"         # Wake-up Type
        "BB",       # SKU Number, Family
        0x01,       # Type = 1
        0x1B,       # Length = 27 bytes
        _TYPE1_HANDLE,
        s_mfr, s_prod, s_ver, s_ser,
        _uuid_bytes_le(uuid_str),
        0x06,       # Wake-up Type = Power Switch
        s_sku, s_fam,
    )
    assert len(formatted) == 27
    return formatted + _string_table(strings)


def build_type3_chassis(*,
                       chassis_type: int,
                       manufacturer: str = "QEMU",
                       serial_number: str = "",
                       version: str = "",
                       asset_tag: str = "",
                       sku: str = "") -> bytes:
    """Type 3 — System Enclosure / Chassis (SMBIOS 2.6, 21-byte formatted).

    ``chassis_type`` is the enum byte from §7.4.1 — 3=Desktop, 10=Notebook,
    31=Convertible, etc. Must fit in an unsigned byte and be nonzero.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(
            f"chassis_type must be 1..255, got {chassis_type!r}"
        )
    strings: list = []
    s_mfr = _str_index(strings, manufacturer)
    s_ver = _str_index(strings, version)
    s_ser = _str_index(strings, serial_number)
    s_asset = _str_index(strings, asset_tag)
    # SKU placeholder so a walker doesn't trip on the terminator early.
    # Not referenced by any index; purely padding in the string set.
    if sku:
        _str_index(strings, sku)
    else:
        _str_index(strings, "Default")

    formatted = struct.pack(
        "<BBH"      # Type, Length, Handle
        "BB"        # Manufacturer, Chassis Type
        "BBB"       # Version, Serial, Asset Tag
        "BBBB"      # Boot-up State, Power Supply, Thermal, Security Status
        "4s"        # OEM Defined
        "BBBB",     # Height, NumPowerCords, ContainedEltCount, ContainedEltRecLen
        0x03,       # Type = 3
        0x15,       # Length = 21 bytes
        _TYPE3_HANDLE,
        s_mfr, chassis_type,
        s_ver, s_ser, s_asset,
        3, 3, 3, 3,   # Safe / None for all states
        b"\x00\x00\x00\x00",
        0, 0, 0, 0,
    )
    assert len(formatted) == 21
    return formatted + _string_table(strings)


def build_full_smbios(*,
                     manufacturer: str,
                     product_name: str,
                     family: str = "",
                     sku: str = "",
                     version: str = "",
                     serial_number: str,
                     uuid_str: str,
                     chassis_type: int,
                     bios_vendor: Optional[str] = None,
                     bios_version: str = "1.0",
                     bios_release_date: str = "01/01/2026") -> bytes:
    """Assemble a per-VM SMBIOS file with Type 0 + Type 1 + Type 3.

    This is the payload for QEMU's ``-smbios file=<path>``. When the file
    contains these three types together, QEMU does NOT emit its Bochs /
    BXPC defaults for those types — the file owns them. Type 2 (Board)
    and Type 4+ (CPU, memory) come from QEMU's defaults; we don't touch
    them because they're auto-populated per the VM's hardware.

    ``bios_vendor`` defaults to ``manufacturer`` if unset (mirrors how
    real hardware presents — same vendor for system and BIOS).
    """
    return (
        build_type0_bios(
            vendor=bios_vendor or manufacturer,
            bios_version=bios_version,
            release_date=bios_release_date,
        )
        + build_type1_system(
            manufacturer=manufacturer,
            product_name=product_name,
            version=version,
            serial_number=serial_number,
            uuid_str=uuid_str,
            sku=sku,
            family=family,
        )
        + build_type3_chassis(
            chassis_type=chassis_type,
            manufacturer=manufacturer,
            serial_number=serial_number,
            sku=sku,
        )
    )
