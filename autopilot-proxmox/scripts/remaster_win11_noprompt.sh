#!/usr/bin/env bash
#
# remaster_win11_noprompt.sh
#
# One-shot re-master of a Windows 11 ARM64 install ISO that swaps the
# El Torito UEFI boot image for Microsoft's `efisys_noprompt.bin`,
# producing an ISO that boots Setup directly with no "Press any key
# to boot from CD or DVD..." countdown.
#
# How Microsoft makes a no-prompt ISO (per 2Pint DeployR's PEPrep.ps1
# and the Windows ADK docs): they pass `efisys_noprompt.bin` (instead
# of `efisys.bin`) as the El Torito UEFI boot sector when running
# `oscdimg.exe`. That swap is the *only* difference between a stock
# install ISO and a no-prompt install ISO.
#
# This script does the equivalent without rebuilding the whole ISO:
# locate the byte range AAVMF actually reads when booting via El
# Torito UEFI (load_lba pulled from the El Torito Boot Catalog), and
# overwrite it in place with `efisys_noprompt.bin`.
#
# Why we don't touch `\efi\boot\bootaa64.efi`: on Win11 ARM64 install
# media that file is `bootmgfw.efi` (Boot Manager, ~3 MiB), not
# `cdboot.efi`. BootMgr alone never shows the "Press any key" prompt;
# the prompt only comes from `cdboot.efi`, which lives inside the
# El Torito FAT image. Replacing bootmgfw.efi was a previous error
# in this script.
#
# Why we don't touch the UDF-visible `\efi\microsoft\boot\efisys.bin`:
# AAVMF reads from the El Torito Boot Catalog's load_lba, not from
# the UDF tree. The UDF copy is reachable via mount -t udf but is
# never executed at boot. (Patching it does nothing, which is how the
# previous version of this script appeared to "succeed" while still
# producing a prompt.)
#
# Usage:
#   ./remaster_win11_noprompt.sh <input.iso> [output.iso]
#
# Default output path inserts `_noprompt` before the .iso extension.
# Idempotent: skips re-mastering when the output already has the
# no-prompt boot image at the El Torito LBA.
#
# Requirements:
#   - python3 with pycdlib (auto-installed via pip if missing)
#
# Tested against Microsoft's `Win11_25H2_English_Arm64_v2.iso`.

set -euo pipefail

INPUT_ISO="${1:?usage: $0 <input.iso> [output.iso]}"
DEFAULT_OUTPUT="${INPUT_ISO%.iso}_noprompt.iso"
OUTPUT_ISO="${2:-$DEFAULT_OUTPUT}"

if [[ ! -f "$INPUT_ISO" ]]; then
    echo "ERROR: input ISO not found: $INPUT_ISO" >&2
    exit 2
fi

if ! python3 -c "import pycdlib" >/dev/null 2>&1; then
    echo "pycdlib not found; installing via pip3..."
    pip3 install --user pycdlib
fi

echo "==> Probing source ISO layout (El Torito + efisys_noprompt extents)"
PROBE_OUT="$(python3 - "$INPUT_ISO" <<'PYEOF'
import struct
import sys

import pycdlib

iso_path = sys.argv[1]

# Boot Record Volume Descriptor lives at LBA 17. Pull boot catalog LBA.
with open(iso_path, 'rb') as f:
    f.seek(17 * 2048)
    brvd = f.read(2048)
    if brvd[0:7] != b'\x00CD001\x01':
        sys.exit('ISO has no Boot Record Volume Descriptor at LBA 17')
    cat_lba = struct.unpack('<I', brvd[71:75])[0]

    # Read boot catalog. Validation Entry [0..32) + Initial/Default
    # Entry [32..64). El Torito Initial Entry layout:
    #   byte  0 : 0x88 = bootable, 0x00 = not bootable
    #   byte  1 : boot media (0 = no emulation)
    #   bytes 6-7 : sector count (in 512-byte virtual sectors)
    #   bytes 8-11: load_lba (in 2048-byte ISO sectors)
    f.seek(cat_lba * 2048)
    cat = f.read(64)
    if cat[0] != 0x01 or cat[1] != 0xEF:
        sys.exit('boot catalog validation entry missing or non-EFI '
                 f'(type=0x{cat[0]:02x} platform=0x{cat[1]:02x})')
    boot_entry = cat[32:64]
    if boot_entry[0] != 0x88:
        sys.exit('UEFI boot entry is not bootable (Initial Entry indicator != 0x88)')
    sector_count_512 = struct.unpack('<H', boot_entry[6:8])[0]
    load_lba = struct.unpack('<I', boot_entry[8:12])[0]
    boot_image_bytes = sector_count_512 * 512

# Locate efisys_noprompt.bin in the UDF tree to grab its byte content
iso = pycdlib.PyCdlib()
iso.open(iso_path)
part_start = iso.udf_main_descs.partitions[0].part_start_location

def file_extent(udf_path):
    rec = iso.get_record(udf_path=udf_path)
    ad = rec.alloc_descs[0]
    return ad.log_block_num, rec.get_data_length()

np_lbn, np_len = file_extent('/efi/microsoft/boot/efisys_noprompt.bin')
iso.close()

print(f'PART_START={part_start}')
print(f'EL_TORITO_LBA={load_lba}')
print(f'EL_TORITO_LEN={boot_image_bytes}')
print(f'EFISYS_NP_LBN={np_lbn}')
print(f'EFISYS_NP_LEN={np_len}')
PYEOF
)"

# Variables surfaced: PART_START, EL_TORITO_LBA, EL_TORITO_LEN,
# EFISYS_NP_LBN, EFISYS_NP_LEN
eval "$PROBE_OUT"

EL_TORITO_OFF=$(( EL_TORITO_LBA * 2048 ))
EFISYS_NP_OFF=$(( (PART_START + EFISYS_NP_LBN) * 2048 ))

echo "    El Torito UEFI boot image:"
echo "      LBA               : $EL_TORITO_LBA"
echo "      byte offset       : $EL_TORITO_OFF (0x$(printf '%x' $EL_TORITO_OFF))"
echo "      length            : $EL_TORITO_LEN bytes"
echo "    efisys_noprompt.bin (source data):"
echo "      partition start   : $PART_START"
echo "      log block number  : $EFISYS_NP_LBN"
echo "      byte offset       : $EFISYS_NP_OFF (0x$(printf '%x' $EFISYS_NP_OFF))"
echo "      length            : $EFISYS_NP_LEN bytes"

if [[ "$EFISYS_NP_LEN" != "$EL_TORITO_LEN" ]]; then
    echo "ERROR: efisys_noprompt.bin ($EFISYS_NP_LEN) and El Torito boot image ($EL_TORITO_LEN) must match in size for in-place swap" >&2
    exit 6
fi

# Idempotency: hash the El Torito boot image in input vs in any
# existing output. If they already differ AND the output's image
# matches the source's efisys_noprompt.bin, we're done.
if [[ -f "$OUTPUT_ISO" && "$OUTPUT_ISO" -nt "$INPUT_ISO" ]]; then
    echo "==> Output ISO exists and is newer than input; verifying patch..."
    if python3 - "$INPUT_ISO" "$OUTPUT_ISO" "$EL_TORITO_OFF" "$EL_TORITO_LEN" "$EFISYS_NP_OFF" <<'PYEOF'
import hashlib
import sys

src_iso, dst_iso, el_off, el_len, np_off = sys.argv[1:6]
el_off, el_len, np_off = int(el_off), int(el_len), int(np_off)

def hash_range(p, off, length):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        f.seek(off)
        h.update(f.read(length))
    return h.hexdigest()

src_np = hash_range(src_iso, np_off, el_len)   # source efisys_noprompt
dst_el = hash_range(dst_iso, el_off, el_len)   # output El Torito image
sys.exit(0 if src_np == dst_el else 1)
PYEOF
    then
        echo "ALREADY DONE: $OUTPUT_ISO El Torito image matches efisys_noprompt.bin; skipping."
        echo "Delete the output ISO to force a rebuild."
        exit 0
    else
        echo "    output exists but El Torito image is NOT patched; rebuilding."
    fi
fi

echo "==> Copying $INPUT_ISO -> $OUTPUT_ISO"
TMP_ISO="${OUTPUT_ISO}.tmp.$$"
cleanup() { rm -f "$TMP_ISO"; }
trap cleanup EXIT
cp "$INPUT_ISO" "$TMP_ISO"

echo "==> Patching El Torito UEFI boot image with efisys_noprompt.bin"
python3 - "$TMP_ISO" "$EL_TORITO_OFF" "$EL_TORITO_LEN" "$EFISYS_NP_OFF" <<'PYEOF'
import sys

iso, el_off, el_len, np_off = sys.argv[1:5]
el_off, el_len, np_off = int(el_off), int(el_len), int(np_off)

with open(iso, 'r+b') as f:
    f.seek(np_off)
    payload = f.read(el_len)
    if payload[:2] != b'\xeb\x3c':
        sys.exit('source efisys_noprompt.bin missing FAT boot signature 0xEB 0x3C')

    f.seek(el_off)
    existing = f.read(el_len)
    if existing[:2] != b'\xeb\x3c':
        sys.exit('El Torito target missing FAT boot signature; LBA computation wrong')

    f.seek(el_off)
    f.write(payload)

print('OK: El Torito UEFI boot image overwritten')
PYEOF

echo "==> Verifying patch"
python3 - "$TMP_ISO" "$EL_TORITO_OFF" "$EL_TORITO_LEN" "$EFISYS_NP_OFF" <<'PYEOF'
import hashlib
import sys

iso, el_off, el_len, np_off = sys.argv[1:5]
el_off, el_len, np_off = int(el_off), int(el_len), int(np_off)

def sha(off):
    with open(iso, 'rb') as f:
        f.seek(off)
        return hashlib.sha256(f.read(el_len)).hexdigest()

el_sha = sha(el_off)
np_sha = sha(np_off)
print(f'  El Torito @ 0x{el_off:x}      sha256={el_sha}')
print(f'  efisys_noprompt @ 0x{np_off:x} sha256={np_sha}')
if el_sha != np_sha:
    sys.exit('FAIL: El Torito image hash != efisys_noprompt.bin hash')
print('OK: El Torito image == efisys_noprompt.bin')
PYEOF

mv "$TMP_ISO" "$OUTPUT_ISO"
trap - EXIT

echo "==> Done"
echo "    output : $OUTPUT_ISO"
echo "    size   : $(du -h "$OUTPUT_ISO" | cut -f1)"
echo "    sha256 : $(shasum -a 256 "$OUTPUT_ISO" | cut -d' ' -f1)"
echo
echo "Next: point the playbook at the new ISO. Either rename to keep"
echo "the original filename, or pass:"
echo "    -e utm_iso_name=$(basename "$OUTPUT_ISO")"
