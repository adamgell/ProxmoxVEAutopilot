#!/usr/bin/env bash
# Publish a WinPE ISO to Proxmox ISO storage over SSH and update inventory.
#
# Default target is pve2, because the cluster-wide API endpoint at
# 192.168.2.200 lands on pve1 where the `isos` storage is disabled.
#
# Example:
#   scripts/publish_winpe_iso.sh \
#     --source 'Adam.Gell@10.211.55.6:/F:/BuildRoot/outputs/winpe-autopilot-amd64-9079a01b111797f6.iso'
#
# Local source example:
#   scripts/publish_winpe_iso.sh --source ./winpe-autopilot-amd64-test.iso

set -euo pipefail

PVE_SSH="root@192.168.2.48"
ISO_STORAGE="isos"
SOURCE=""
REMOTE_NAME=""
UPDATE_INVENTORY=1
DELETE_OLD=0
DRY_RUN=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VARS_FILE="${APP_DIR}/inventory/group_vars/all/vars.yml"

usage() {
  cat <<EOF
Usage: $(basename "$0") --source <local-or-scp-source> [options]

Options:
  --source <path>         Local path or scp-style source, e.g.
                          user@host:/F:/BuildRoot/outputs/winpe.iso
  --pve <ssh-target>      Proxmox SSH target. Default: ${PVE_SSH}
  --storage <name>        Proxmox ISO storage. Default: ${ISO_STORAGE}
  --remote-name <name>    Destination ISO filename. Default: source basename
  --vars-file <path>      vars.yml to update. Default: ${VARS_FILE}
  --no-inventory          Upload only; do not update proxmox_winpe_iso
  --delete-old            Remove other winpe-autopilot-*.iso files in storage
  --dry-run               Print planned actions without copying/updating
  -h, --help              Show this help
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="${2:-}"; shift 2 ;;
    --pve)
      PVE_SSH="${2:-}"; shift 2 ;;
    --storage)
      ISO_STORAGE="${2:-}"; shift 2 ;;
    --remote-name)
      REMOTE_NAME="${2:-}"; shift 2 ;;
    --vars-file)
      VARS_FILE="${2:-}"; shift 2 ;;
    --no-inventory)
      UPDATE_INVENTORY=0; shift ;;
    --delete-old)
      DELETE_OLD=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      die "unknown argument: $1" ;;
  esac
done

[[ -n "${SOURCE}" ]] || die "--source is required"
[[ -n "${PVE_SSH}" ]] || die "--pve cannot be empty"
[[ -n "${ISO_STORAGE}" ]] || die "--storage cannot be empty"

if [[ -z "${REMOTE_NAME}" ]]; then
  REMOTE_NAME="${SOURCE##*/}"
fi

[[ "${REMOTE_NAME}" =~ ^[A-Za-z0-9._-]+\.iso$ ]] || \
  die "--remote-name must be a simple .iso filename: ${REMOTE_NAME}"

VOLID="${ISO_STORAGE}:iso/${REMOTE_NAME}"

TMPDIR="$(mktemp -d)"
cleanup() {
  rm -rf "${TMPDIR}"
}
trap cleanup EXIT

LOCAL_ISO="${TMPDIR}/${REMOTE_NAME}"

echo "Source:      ${SOURCE}"
echo "PVE target:  ${PVE_SSH}"
echo "Storage:     ${ISO_STORAGE}"
echo "VolID:       ${VOLID}"
echo "vars.yml:    ${VARS_FILE}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "DRY RUN: would stage source locally, copy to Proxmox, and update inventory."
  exit 0
fi

if [[ "${SOURCE}" == *:* && ! -f "${SOURCE}" ]]; then
  scp "${SOURCE}" "${LOCAL_ISO}"
else
  [[ -f "${SOURCE}" ]] || die "source ISO not found: ${SOURCE}"
  cp "${SOURCE}" "${LOCAL_ISO}"
fi

[[ -s "${LOCAL_ISO}" ]] || die "staged ISO is empty: ${LOCAL_ISO}"
LOCAL_SHA="$(shasum -a 256 "${LOCAL_ISO}" | awk '{print $1}')"
LOCAL_SIZE="$(wc -c < "${LOCAL_ISO}" | tr -d ' ')"
echo "Staged:      ${LOCAL_ISO}"
echo "Size:        ${LOCAL_SIZE} bytes"
echo "SHA256:      ${LOCAL_SHA}"

REMOTE_PATH="$(
  ssh "${PVE_SSH}" "set -euo pipefail; pvesm path '${VOLID}'"
)"
[[ -n "${REMOTE_PATH}" ]] || die "could not resolve ${VOLID} on ${PVE_SSH}"
REMOTE_DIR="${REMOTE_PATH%/*}"
REMOTE_TMP="/tmp/${REMOTE_NAME}.$$"

echo "Remote path: ${REMOTE_PATH}"
scp "${LOCAL_ISO}" "${PVE_SSH}:${REMOTE_TMP}"
ssh "${PVE_SSH}" "set -euo pipefail
  test -s '${REMOTE_TMP}'
  mkdir -p '${REMOTE_DIR}'
  install -m 0644 '${REMOTE_TMP}' '${REMOTE_PATH}'
  rm -f '${REMOTE_TMP}'
  sha256sum '${REMOTE_PATH}'
"

if [[ "${DELETE_OLD}" -eq 1 ]]; then
  ssh "${PVE_SSH}" "set -euo pipefail
    find '${REMOTE_DIR}' -maxdepth 1 -type f -name 'winpe-autopilot-*.iso' ! -name '${REMOTE_NAME}' -print -delete
  "
fi

if [[ "${UPDATE_INVENTORY}" -eq 1 ]]; then
  [[ -f "${VARS_FILE}" ]] || die "vars.yml not found: ${VARS_FILE}"
  python3 - "${VARS_FILE}" "${VOLID}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
volid = sys.argv[2]
lines = path.read_text().splitlines()
out = []
replaced = False
pattern = re.compile(r"^(\s*proxmox_winpe_iso\s*:\s*).*$")
for line in lines:
    match = pattern.match(line)
    if match:
        out.append(f'{match.group(1)}"{volid}"')
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f'proxmox_winpe_iso: "{volid}"')
path.write_text("\n".join(out) + "\n")
print(f"Updated {path}: proxmox_winpe_iso={volid}")
PY
fi

echo "Published ${VOLID}"
