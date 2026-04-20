#!/usr/bin/env python3
"""Seed per-chassis SMBIOS Type 3 binaries on a Proxmox host.

Run this ON the Proxmox node (not inside the autopilot container).
Proxmox's /upload API rejects ``content=snippets``, so these binaries
have to be dropped directly on the host filesystem.

Usage:
    python3 seed_chassis_binaries.py [TYPE ...]

With no args, seeds a common set of chassis types covering desktops,
laptops, mini-PCs, convertibles, tablets, and all-in-ones.

After running, ensure the storage exposes ``snippets`` content so the
autopilot presence check sees the files:

    pvesm set local --content backup,iso,import,vztmpl,snippets
"""
from __future__ import annotations

import os
import struct
import sys

SNIPPETS_DIR = "/var/lib/vz/snippets"
DEFAULT_TYPES = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15,
                 16, 17, 23, 24, 30, 31, 32, 35, 36)
_TYPE3_HANDLE = 0x0300


def build_type3_chassis(chassis_type: int) -> bytes:
    """Raw SMBIOS Type 3 structure with the chassis-type enum byte set."""
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(
            f"chassis_type must be an integer in 1..255, got {chassis_type!r}"
        )
    formatted = struct.pack(
        "<BBHBBBBBBBBB4sBBBB",
        0x03, 0x15, _TYPE3_HANDLE,
        1, chassis_type,
        2, 3, 4,
        3, 3, 3, 3,
        b"\x00\x00\x00\x00",
        0, 0, 0, 0,
    )
    strings = b"QEMU\x00" + b"1.0\x00" + b"0\x00" + b"\x00" + b"Default\x00" + b"\x00"
    return formatted + strings


def seed(chassis_types: list[int], snippets_dir: str = SNIPPETS_DIR) -> list[str]:
    os.makedirs(snippets_dir, exist_ok=True)
    written: list[str] = []
    for ct in chassis_types:
        path = os.path.join(snippets_dir, f"autopilot-chassis-type-{int(ct)}.bin")
        with open(path, "wb") as fh:
            fh.write(build_type3_chassis(int(ct)))
        written.append(path)
    return written


def main(argv: list[str]) -> int:
    try:
        types = [int(a) for a in argv[1:]] or list(DEFAULT_TYPES)
    except ValueError as e:
        print(f"invalid chassis type: {e}", file=sys.stderr)
        return 2
    try:
        for path in seed(types):
            print(path)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
