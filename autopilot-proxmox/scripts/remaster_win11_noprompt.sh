#!/usr/bin/env bash
#
# remaster_win11_noprompt.sh
#
# One-shot re-master of a Windows 11 ARM64 install ISO that swaps the
# bootloader for Microsoft's no-prompt deployment variants. The output
# ISO boots Setup directly with no "Press any key to boot from CD or
# DVD..." countdown and no BootMgr keypress at all.
#
# Why this exists: UTM 4.7.5's AAVMF either (a) drops to the EDK2 EFI
# shell on first boot because USB-CLASS Boot#### expansion does not
# work, then the keystroke fallback types the loader path manually, OR
# (b) auto-launches `\efi\boot\bootaa64.efi` via UEFI removable-media
# fallback. Both paths land at Microsoft's BootMgr, which on the stock
# Win11 install media chains through `cdboot.efi` (10s "Press any key"
# countdown) before booting Setup. Replacing the boot files with the
# `_noprompt` siblings Microsoft already ships at
# `\efi\microsoft\boot\` skips the countdown entirely.
#
# Why byte-patch instead of unpack/repack: Win11 ARM64 install media is
# UDF-only with install.wim >4 GiB. macOS hdiutil makehybrid lacks the
# `-eltorito-platform 0xef` flag needed for UEFI El Torito; xorriso
# 1.5.8's `-as mkisofs` emulator rejects `-udf`. Both repack paths
# fail in different ways. The reliable approach is to copy the ISO
# byte-for-byte and overwrite just the two boot file extents in place,
# leaving the UDF/ISO9660 directory structure and the El Torito boot
# catalog untouched.
#
# Usage:
#   ./remaster_win11_noprompt.sh <input.iso> [output.iso]
#
# Default output is the input path with `_noprompt` inserted before
# the .iso extension. Idempotent: skips re-mastering if the output is
# newer than the input AND already exists.
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

# Idempotency: if output exists and is newer than input AND has a
# patched bootaa64.efi, skip. The byte-patch test below catches the
# case where a prior failed run left an unpatched copy at the output.
if [[ -f "$OUTPUT_ISO" && "$OUTPUT_ISO" -nt "$INPUT_ISO" ]]; then
    echo "==> Output ISO exists and is newer than input; verifying patch..."
    if python3 - "$OUTPUT_ISO" <<'PYEOF'
import sys
import pycdlib

iso = pycdlib.PyCdlib()
iso.open(sys.argv[1])
part_start = iso.udf_main_descs.partitions[0].part_start_location
rec = iso.get_record(udf_path='/efi/boot/bootaa64.efi')
ad = rec.alloc_descs[0]
off = (part_start + ad.log_block_num) * 2048
iso.close()
with open(sys.argv[1], 'rb') as f:
    f.seek(off)
    head = f.read(96)
# cdboot_noprompt.efi has a "CDBOOT_N" or similar marker in the PE
# header's debug/version strings. Cheaper signal: cdboot_noprompt.efi
# (~968 KiB) is much smaller than bootaa64.efi (~3 MiB), so the
# slot's tail bytes will be 0x00 padding when patched.
with open(sys.argv[1], 'rb') as f:
    f.seek(off + 968096)  # one byte past end of cdboot_noprompt
    tail = f.read(64)
patched = head[:2] == b'MZ' and tail == b'\x00' * 64
sys.exit(0 if patched else 1)
PYEOF
    then
        echo "ALREADY DONE: $OUTPUT_ISO is patched; skipping re-master."
        echo "Delete the output ISO to force a rebuild."
        exit 0
    else
        echo "    output exists but is NOT patched; rebuilding."
    fi
fi

if ! python3 -c "import pycdlib" >/dev/null 2>&1; then
    echo "pycdlib not found; installing via pip3..."
    pip3 install --user pycdlib
fi

echo "==> Probing source ISO layout"
PROBE_OUT="$(python3 - "$INPUT_ISO" <<'PYEOF'
import sys
import pycdlib

iso = pycdlib.PyCdlib()
iso.open(sys.argv[1])
part_start = iso.udf_main_descs.partitions[0].part_start_location

def probe(path):
    rec = iso.get_record(udf_path=path)
    ad = rec.alloc_descs[0]
    return ad.log_block_num, rec.get_data_length()

paths = {
    'BOOTAA64':  '/efi/boot/bootaa64.efi',
    'EFISYS':    '/efi/microsoft/boot/efisys.bin',
    'CDBOOT_NP': '/efi/microsoft/boot/cdboot_noprompt.efi',
    'EFISYS_NP': '/efi/microsoft/boot/efisys_noprompt.bin',
}
print(f'PART_START={part_start}')
for key, path in paths.items():
    lbn, length = probe(path)
    print(f'{key}_LBN={lbn}')
    print(f'{key}_LEN={length}')
iso.close()
PYEOF
)"

# Surface variables: BOOTAA64_LBN, BOOTAA64_LEN, EFISYS_LBN, EFISYS_LEN,
# CDBOOT_NP_LBN, CDBOOT_NP_LEN, EFISYS_NP_LBN, EFISYS_NP_LEN, PART_START
eval "$PROBE_OUT"

echo "    partition start LBA: $PART_START"
echo "    bootaa64.efi        : LBN=$BOOTAA64_LBN  size=$BOOTAA64_LEN"
echo "    efisys.bin          : LBN=$EFISYS_LBN  size=$EFISYS_LEN"
echo "    cdboot_noprompt.efi : LBN=$CDBOOT_NP_LBN  size=$CDBOOT_NP_LEN"
echo "    efisys_noprompt.bin : LBN=$EFISYS_NP_LBN  size=$EFISYS_NP_LEN"

if [[ "$EFISYS_NP_LEN" != "$EFISYS_LEN" ]]; then
    echo "ERROR: efisys_noprompt.bin ($EFISYS_NP_LEN) and efisys.bin ($EFISYS_LEN) must be the same size for in-place swap" >&2
    exit 6
fi
if (( CDBOOT_NP_LEN > BOOTAA64_LEN )); then
    echo "ERROR: cdboot_noprompt.efi ($CDBOOT_NP_LEN) larger than bootaa64.efi slot ($BOOTAA64_LEN); cannot patch in place" >&2
    exit 7
fi

echo "==> Copying $INPUT_ISO -> $OUTPUT_ISO"
TMP_ISO="${OUTPUT_ISO}.tmp.$$"
cleanup() {
    rm -f "$TMP_ISO"
}
trap cleanup EXIT
cp "$INPUT_ISO" "$TMP_ISO"

echo "==> Patching boot file extents in place"
SECTOR=2048
SRC_CDBOOT_OFF=$(( (PART_START + CDBOOT_NP_LBN) * SECTOR ))
SRC_EFISYS_NP_OFF=$(( (PART_START + EFISYS_NP_LBN) * SECTOR ))
DST_BOOTAA64_OFF=$(( (PART_START + BOOTAA64_LBN) * SECTOR ))
DST_EFISYS_OFF=$(( (PART_START + EFISYS_LBN) * SECTOR ))

python3 - <<PYEOF
import sys
ISO = "$TMP_ISO"
SRC_CDBOOT_OFF = $SRC_CDBOOT_OFF
SRC_EFISYS_NP_OFF = $SRC_EFISYS_NP_OFF
DST_BOOTAA64_OFF = $DST_BOOTAA64_OFF
DST_EFISYS_OFF = $DST_EFISYS_OFF
CDBOOT_NP_LEN = $CDBOOT_NP_LEN
EFISYS_NP_LEN = $EFISYS_NP_LEN
BOOTAA64_LEN = $BOOTAA64_LEN

with open(ISO, 'r+b') as f:
    f.seek(SRC_CDBOOT_OFF)
    cdboot = f.read(CDBOOT_NP_LEN)
    if cdboot[:2] != b'MZ':
        sys.exit('source cdboot_noprompt.efi missing MZ header; ISO layout unexpected')
    f.seek(SRC_EFISYS_NP_OFF)
    efisys_np = f.read(EFISYS_NP_LEN)
    if efisys_np[:2] != b'\xeb\x3c':
        sys.exit('source efisys_noprompt.bin missing FAT boot signature; ISO layout unexpected')

    # Pad cdboot_noprompt.efi to bootaa64.efi slot size with NUL.
    # PE loaders ignore trailing bytes past the headers' SizeOfImage.
    padded = cdboot + b'\x00' * (BOOTAA64_LEN - CDBOOT_NP_LEN)
    f.seek(DST_BOOTAA64_OFF)
    f.write(padded)

    # efisys.bin and efisys_noprompt.bin are byte-equal in size, so
    # this is a clean overlay with no padding needed.
    f.seek(DST_EFISYS_OFF)
    f.write(efisys_np)

print('OK: patched bootaa64.efi and efisys.bin in place')
PYEOF

echo "==> Verifying patch"
python3 - "$TMP_ISO" <<'PYEOF'
import sys
import pycdlib

iso = pycdlib.PyCdlib()
iso.open(sys.argv[1])
part_start = iso.udf_main_descs.partitions[0].part_start_location

def head(path, n=8):
    rec = iso.get_record(udf_path=path)
    ad = rec.alloc_descs[0]
    off = (part_start + ad.log_block_num) * 2048
    with open(sys.argv[1], 'rb') as f:
        f.seek(off)
        return f.read(n)

# Both bootaa64.efi (now cdboot_noprompt.efi data) and
# cdboot_noprompt.efi proper start with MZ. Quick sanity check.
b = head('/efi/boot/bootaa64.efi')
e = head('/efi/microsoft/boot/efisys.bin')
iso.close()
if b[:2] != b'MZ':
    sys.exit(f'bootaa64.efi missing MZ after patch: {b!r}')
if e[:2] != b'\xeb\x3c':
    sys.exit(f'efisys.bin missing FAT signature after patch: {e!r}')
print(f'  bootaa64.efi head: {b.hex()}')
print(f'  efisys.bin   head: {e.hex()}')
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
