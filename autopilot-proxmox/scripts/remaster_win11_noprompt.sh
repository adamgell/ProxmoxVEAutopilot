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
# Usage:
#   ./remaster_win11_noprompt.sh <input.iso> [output.iso]
#
# Default output is the input path with `_noprompt` inserted before
# the .iso extension. Idempotent: skips re-mastering if the output is
# newer than the input AND already exists.
#
# Requirements:
#   - macOS with hdiutil (preinstalled)
#   - xorriso (auto-installed via Homebrew if missing)
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

# Idempotency: if output exists and is newer than input, skip.
if [[ -f "$OUTPUT_ISO" && "$OUTPUT_ISO" -nt "$INPUT_ISO" ]]; then
    echo "ALREADY DONE: $OUTPUT_ISO is newer than $INPUT_ISO; skipping re-master."
    echo "Delete the output ISO to force a rebuild."
    exit 0
fi

if ! command -v xorriso >/dev/null 2>&1; then
    echo "xorriso not found; installing via Homebrew..."
    if ! command -v brew >/dev/null 2>&1; then
        echo "ERROR: Homebrew not installed. Install brew first or install xorriso manually." >&2
        exit 3
    fi
    brew install xorriso
fi

# Stage the ISO to a writable directory. We need ~8 GiB free for a
# Win11 ARM64 ISO. Use a temp dir on the same volume as the output to
# minimize copy churn at xorriso time.
STAGE_BASE="$(dirname "$OUTPUT_ISO")"
STAGE_DIR="$(mktemp -d "${STAGE_BASE}/win11-noprompt-stage.XXXXXX")"
MOUNT_POINT=""

cleanup() {
    if [[ -n "$MOUNT_POINT" && -d "$MOUNT_POINT" ]]; then
        hdiutil detach "$MOUNT_POINT" >/dev/null 2>&1 || true
    fi
    rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

echo "==> Mounting $INPUT_ISO read-only"
MOUNT_INFO="$(hdiutil attach -nobrowse -readonly -plist "$INPUT_ISO")"
MOUNT_POINT="$(echo "$MOUNT_INFO" | sed -n 's|.*<string>\(/Volumes/[^<]*\)</string>.*|\1|p' | head -1)"
if [[ -z "$MOUNT_POINT" || ! -d "$MOUNT_POINT" ]]; then
    echo "ERROR: failed to mount $INPUT_ISO" >&2
    exit 4
fi

# Capture the volume label so the re-mastered ISO carries it forward.
# Setup auto-detects answer media partly by ID; preserving the label
# avoids surprising downstream tooling that filters by it.
VOL_LABEL="$(basename "$MOUNT_POINT")"
echo "    volume label: $VOL_LABEL"

echo "==> Staging ISO contents to $STAGE_DIR"
# rsync preserves mode + symlinks; CD-ROMs are plain files but rsync
# is faster than cp -R for large trees and gives a progress indicator.
rsync -a --info=progress2 "$MOUNT_POINT/" "$STAGE_DIR/"

echo "==> Verifying _noprompt boot files are present"
NOPROMPT_CDBOOT="$STAGE_DIR/efi/microsoft/boot/cdboot_noprompt.efi"
NOPROMPT_EFISYS="$STAGE_DIR/efi/microsoft/boot/efisys_noprompt.bin"
TARGET_BOOTAA64="$STAGE_DIR/efi/boot/bootaa64.efi"
TARGET_EFISYS="$STAGE_DIR/efi/microsoft/boot/efisys.bin"

for f in "$NOPROMPT_CDBOOT" "$NOPROMPT_EFISYS" "$TARGET_BOOTAA64" "$TARGET_EFISYS"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: expected file missing in source ISO: ${f#$STAGE_DIR/}" >&2
        echo "       (this script targets Microsoft Win11 ARM64 install ISOs)" >&2
        exit 5
    fi
done

echo "==> Swapping bootloaders"
# 1. UEFI removable-media fallback path: \efi\boot\bootaa64.efi.
#    Replace with cdboot_noprompt.efi so AAVMF auto-launching this
#    file does not show the prompt.
chmod u+w "$TARGET_BOOTAA64"
cp -f "$NOPROMPT_CDBOOT" "$TARGET_BOOTAA64"

# 2. El Torito UEFI boot image: \efi\microsoft\boot\efisys.bin.
#    Replace with efisys_noprompt.bin so any El Torito path also
#    skips the prompt.
chmod u+w "$TARGET_EFISYS"
cp -f "$NOPROMPT_EFISYS" "$TARGET_EFISYS"

echo "    bootaa64.efi: $(shasum -a 256 "$TARGET_BOOTAA64" | cut -d' ' -f1)"
echo "    efisys.bin  : $(shasum -a 256 "$TARGET_EFISYS"   | cut -d' ' -f1)"

echo "==> Repacking ISO with xorriso"
# UEFI-only El Torito layout matching what Microsoft's mediacreator
# emits for Win11 ARM64 install media. The `-no-emul-boot` flag plus
# -eltorito-platform 0xef makes the image a UEFI boot entry; the
# system area / GPT bits keep it bootable on real hardware too.
xorriso \
    -as mkisofs \
    -iso-level 4 \
    -V "$VOL_LABEL" \
    -udf \
    -allow-limited-size \
    -no-emul-boot \
    -eltorito-platform efi \
    -eltorito-boot efi/microsoft/boot/efisys.bin \
    -boot-load-size 8 \
    -no-emul-boot \
    -isohybrid-gpt-basdat \
    -o "$OUTPUT_ISO" \
    "$STAGE_DIR"

echo "==> Done"
echo "    output : $OUTPUT_ISO"
echo "    size   : $(du -h "$OUTPUT_ISO" | cut -f1)"
echo "    sha256 : $(shasum -a 256 "$OUTPUT_ISO" | cut -d' ' -f1)"
echo
echo "Next: point the playbook at the new ISO. Either rename to keep"
echo "the original filename, or pass:"
echo "    -e utm_iso_name=$(basename "$OUTPUT_ISO")"
