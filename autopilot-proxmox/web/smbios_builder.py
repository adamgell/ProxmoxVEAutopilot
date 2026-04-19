"""Build SMBIOS Type 3 (System Enclosure / Chassis) binaries.

The standard QEMU ``-smbios type=3`` command-line option exposes only
the manufacturer/version/serial/asset/sku string fields — it does not
let us set the Chassis Type enum byte that WMI
``Win32_SystemEnclosure.ChassisTypes`` reads. This module produces a
minimal raw SMBIOS Type 3 structure we can feed into QEMU via
``-smbios file=<path>``, which DOES control that byte.

Output is a single SMBIOS structure: 21-byte formatted area followed by
a null-terminated string set ending in double-null. QEMU's
``-smbios file=`` reads concatenated structures in exactly this format.

Reference: DMTF SMBIOS 2.7 §7.4.
"""
from __future__ import annotations

import struct


# Fixed 16-bit handle for the Type 3 structure. Arbitrary but unused —
# SMBIOS handles only need to be unique across the structures QEMU emits.
# Proxmox/QEMU default structures use handles 0x0000..0x00FF for Type 0 / 1,
# 0x0100-range for Type 2, etc. 0x0300 is clear.
_TYPE3_HANDLE = 0x0300


def build_type3_chassis(chassis_type: int) -> bytes:
    """Return the raw bytes of a SMBIOS Type 3 structure.

    ``chassis_type`` is the 1-byte enum value from SMBIOS §7.4.1
    (e.g., 3 = Desktop, 10 = Notebook, 31 = Convertible). Must fit in
    an unsigned byte and must be nonzero.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(
            f"chassis_type must be an integer in 1..255, got {chassis_type!r}"
        )

    # Formatted area: 21 bytes (SMBIOS 2.6 layout, no SKU field).
    formatted = struct.pack(
        "<BBH"    # Type, Length, Handle
        "BB"      # Manufacturer string index, Chassis Type
        "BBB"     # Version, Serial, Asset Tag string indices
        "BBBB"    # Boot-up State, Power Supply State, Thermal State, Security Status
        "4s"      # OEM Defined (4 bytes of zero)
        "BBBB",   # Height, Power Cords, Contained Elt Count, Contained Elt Record Len
        0x03,     # Type = 3 (Chassis)
        0x15,     # Length = 21
        _TYPE3_HANDLE,
        1,        # Manufacturer index
        chassis_type,
        2,        # Version index
        3,        # Serial Number index
        4,        # Asset Tag index
        3, 3, 3, 3,  # All four states = Safe/None
        b"\x00\x00\x00\x00",  # OEM Defined
        0, 0, 0, 0,  # Height, Power Cords, Contained Elt Count, Contained Elt Record Len
    )
    assert len(formatted) == 21, f"formatted area len={len(formatted)}, expected 21"

    # String set: null-terminated strings matching the indices above, then
    # an additional null terminating the structure.  Index 4 (asset tag) is
    # an empty string ("\0"), and index 5 carries a vendor-neutral SKU
    # placeholder so that WMI / SMBIOS parsers that walk all string slots
    # find a well-formed entry rather than treating the double-null as the
    # terminator prematurely.  The structure terminator is the final "\0".
    strings = b"QEMU\x00" + b"1.0\x00" + b"0\x00" + b"\x00" + b"Default\x00" + b"\x00"

    return formatted + strings
