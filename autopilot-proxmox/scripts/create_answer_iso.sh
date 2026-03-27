#!/bin/bash
# Create a small ISO containing autounattend.xml for unattended Windows install.
# Run this on the Proxmox host. Requires genisoimage.
#
# Usage:
#   scp files/autounattend.xml root@192.168.2.200:/tmp/autounattend.xml
#   ssh root@192.168.2.200 'bash -s' < scripts/create_answer_iso.sh
#
# Or copy both files to the Proxmox host and run locally.

set -euo pipefail

ISO_STORAGE="${1:-isos}"
XML_SOURCE="${2:-/tmp/autounattend.xml}"

# Resolve the filesystem path for the ISO storage
STORAGE_PATH=$(pvesm path "${ISO_STORAGE}:iso/autounattend.iso" 2>/dev/null | sed 's|/autounattend.iso$||')
if [ -z "$STORAGE_PATH" ]; then
    echo "ERROR: Could not resolve storage path for '${ISO_STORAGE}'. Check storage name."
    exit 1
fi

if [ ! -f "$XML_SOURCE" ]; then
    echo "ERROR: autounattend.xml not found at '${XML_SOURCE}'"
    exit 1
fi

# Check for genisoimage
if ! command -v genisoimage &>/dev/null; then
    echo "Installing genisoimage..."
    apt-get update -qq && apt-get install -y -qq genisoimage
fi

# Build the ISO
TMPDIR=$(mktemp -d)
cp "$XML_SOURCE" "${TMPDIR}/autounattend.xml"

genisoimage \
    -o "${STORAGE_PATH}/autounattend.iso" \
    -J -r \
    -V "OEMDRV" \
    "$TMPDIR"

rm -rf "$TMPDIR"

echo "Created: ${STORAGE_PATH}/autounattend.iso"
echo "Proxmox reference: ${ISO_STORAGE}:iso/autounattend.iso"
