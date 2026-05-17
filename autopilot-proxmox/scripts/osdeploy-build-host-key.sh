#!/usr/bin/env bash
# Create or print the controller SSH key used by OSDeploy Windows build hosts.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOPILOT_PROXMOX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DEFAULT_HOST_KEY="${AUTOPILOT_PROXMOX_DIR}/secrets/osdeploy_devmachine_ed25519"
DEFAULT_CONTAINER_KEY="/app/secrets/osdeploy_devmachine_ed25519"

KEY_PATH="${AUTOPILOT_OSDEPLOY_BUILD_SSH_KEY:-${DEFAULT_HOST_KEY}}"
CONTAINER_KEY_PATH="${AUTOPILOT_OSDEPLOY_BUILD_SSH_CONTAINER_PATH:-${DEFAULT_CONTAINER_KEY}}"
PRINT_PUBLIC_KEY_ONLY=0

usage() {
  cat <<'USAGE'
Usage:
  osdeploy-build-host-key.sh [options]

Options:
  --key <path>                 Host-side private key path.
                               Default: ./secrets/osdeploy_devmachine_ed25519
  --container-path <path>      Path mounted inside web/builder containers.
                               Default: /app/secrets/osdeploy_devmachine_ed25519
  --print-public-key-only      Print only the public key, creating it first if needed.
  --help, -h                   Show this help.

What it does:
  1. Creates the OSDeploy build-host ed25519 key if it is missing.
  2. Prints the public key to install on the Windows OpenSSH build host.
  3. Prints the settings value for osdeploy_build_ssh_key_path.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key)
      KEY_PATH="${2:-}"; shift 2 ;;
    --container-path)
      CONTAINER_KEY_PATH="${2:-}"; shift 2 ;;
    --print-public-key-only)
      PRINT_PUBLIC_KEY_ONLY=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    --*)
      die "unknown option: $1" ;;
    *)
      die "unexpected argument: $1" ;;
  esac
done

[[ -n "${KEY_PATH}" ]] || die "key path is required"
[[ -n "${CONTAINER_KEY_PATH}" ]] || die "container path is required"

require_command ssh-keygen

mkdir -p "$(dirname "${KEY_PATH}")"
chmod 700 "$(dirname "${KEY_PATH}")"

if [[ ! -s "${KEY_PATH}" ]]; then
  ssh-keygen -q -t ed25519 -a 64 -N "" \
    -f "${KEY_PATH}" \
    -C "proxmoxveautopilot-osdeploy-build" >/dev/null
fi

[[ -s "${KEY_PATH}.pub" ]] || ssh-keygen -y -f "${KEY_PATH}" >"${KEY_PATH}.pub"
chmod 600 "${KEY_PATH}"
chmod 644 "${KEY_PATH}.pub"

if [[ "${PRINT_PUBLIC_KEY_ONLY}" == "1" ]]; then
  cat "${KEY_PATH}.pub"
  exit 0
fi

cat <<EOF
OSDeploy build-host key is ready.

Private key:
  ${KEY_PATH}

Container setting:
  osdeploy_build_ssh_key_path: ${CONTAINER_KEY_PATH}

Public key to install on the Windows OpenSSH build host:
$(cat "${KEY_PATH}.pub")

Install target:
  Add the public key above to the build user's authorized_keys file on the
  Windows build host, then set osdeploy_build_remote and
  osdeploy_build_remote_root in Settings > OSDeploy Build Host.
EOF
