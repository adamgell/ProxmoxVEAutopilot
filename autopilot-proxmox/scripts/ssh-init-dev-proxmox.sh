#!/usr/bin/env bash
# Mac-side pre-init helper for fresh Proxmox VE dev nodes.
#
# It handles the first SSH trust/key-auth hop only. It does not install
# packages, change PVE config, or run the project init flow.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOPILOT_PROXMOX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${AUTOPILOT_PROXMOX_DIR}/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage:
  ssh-init-dev-proxmox.sh [alias] <host-or-ip> [options]
  ssh-init-dev-proxmox.sh --host <host-or-ip> --alias <ssh-alias> [options]

Examples:
  ./scripts/ssh-init-dev-proxmox.sh 192.168.2.55
  ./scripts/ssh-init-dev-proxmox.sh pve-dev-01 192.168.2.55
  ./scripts/ssh-init-dev-proxmox.sh --host 192.168.2.55 --alias pve-dev-01 --yes

Options:
  --host <host>            Proxmox host/IP. May also be positional.
  --alias <name>           SSH config alias. Default: pve-dev-<host>.
  --user <user>            SSH user. Default: root.
  --port <port>            SSH port. Default: 22.
  --key <path>             SSH identity. Default: ~/.ssh/proxmox_dev_ed25519.
  --config <path>          SSH config path. Default: ~/.ssh/config.
  --known-hosts <path>     known_hosts path. Default: ~/.ssh/known_hosts.
  --create-key             Create --key when missing. Default.
  --no-create-key          Fail if --key is missing.
  --install-key            Install public key into authorized_keys. Default.
  --no-install-key         Skip authorized_keys install.
  --refresh-host-key       Remove any existing host key before ssh-keyscan.
  --yes, -y                Non-interactive trust prompts.
  --help, -h               Show this help.

What it does:
  1. Creates a reusable local SSH key if needed.
  2. Scans and pins the PVE host key in known_hosts.
  3. Installs your public key on the PVE root account, if enabled.
  4. Writes an idempotent Host block into ~/.ssh/config.
  5. Verifies `ssh <alias>` works and prints next-step commands.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

log() {
  printf '[ssh-init] %s\n' "$*"
}

confirm() {
  local prompt="$1"
  if [[ "${YES}" == "1" ]]; then
    return 0
  fi
  local reply
  read -r -p "${prompt} [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

single_quote() {
  # Print a shell single-quoted version of stdin-safe text.
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

default_alias_for_host() {
  local value="$1"
  value="${value//[^[:alnum:]._-]/-}"
  value="${value//./-}"
  printf 'pve-dev-%s' "${value}"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

HOST=""
ALIAS=""
SSH_USER="root"
SSH_PORT="22"
SSH_KEY="${HOME}/.ssh/proxmox_dev_ed25519"
SSH_CONFIG="${HOME}/.ssh/config"
KNOWN_HOSTS="${HOME}/.ssh/known_hosts"
CREATE_KEY=1
INSTALL_KEY=1
REFRESH_HOST_KEY=0
YES=0
POSITIONAL=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:-}"; shift 2 ;;
    --alias)
      ALIAS="${2:-}"; shift 2 ;;
    --user)
      SSH_USER="${2:-}"; shift 2 ;;
    --port)
      SSH_PORT="${2:-}"; shift 2 ;;
    --key)
      SSH_KEY="${2:-}"; shift 2 ;;
    --config)
      SSH_CONFIG="${2:-}"; shift 2 ;;
    --known-hosts)
      KNOWN_HOSTS="${2:-}"; shift 2 ;;
    --create-key)
      CREATE_KEY=1; shift ;;
    --no-create-key)
      CREATE_KEY=0; shift ;;
    --install-key)
      INSTALL_KEY=1; shift ;;
    --no-install-key)
      INSTALL_KEY=0; shift ;;
    --refresh-host-key)
      REFRESH_HOST_KEY=1; shift ;;
    --yes|-y)
      YES=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    --*)
      die "unknown option: $1" ;;
    *)
      POSITIONAL+=("$1"); shift ;;
  esac
done

if [[ ${#POSITIONAL[@]} -eq 1 ]]; then
  HOST="${HOST:-${POSITIONAL[0]}}"
elif [[ ${#POSITIONAL[@]} -eq 2 ]]; then
  ALIAS="${ALIAS:-${POSITIONAL[0]}}"
  HOST="${HOST:-${POSITIONAL[1]}}"
elif [[ ${#POSITIONAL[@]} -gt 2 ]]; then
  die "too many positional arguments"
fi

[[ -n "${HOST}" ]] || { usage; die "host is required"; }
ALIAS="${ALIAS:-$(default_alias_for_host "${HOST}")}"

[[ "${ALIAS}" =~ ^[A-Za-z0-9._-]+$ ]] || die "alias must contain only letters, numbers, dots, underscores, and hyphens"
[[ "${SSH_PORT}" =~ ^[0-9]+$ ]] || die "port must be numeric"

require_command ssh
require_command ssh-keygen
require_command ssh-keyscan
require_command mktemp

SSH_DIR="$(dirname "${SSH_KEY}")"
mkdir -p "${SSH_DIR}" "$(dirname "${SSH_CONFIG}")" "$(dirname "${KNOWN_HOSTS}")"
chmod 700 "${SSH_DIR}"
touch "${SSH_CONFIG}" "${KNOWN_HOSTS}"
chmod 600 "${SSH_CONFIG}" "${KNOWN_HOSTS}"

if [[ ! -f "${SSH_KEY}" ]]; then
  [[ "${CREATE_KEY}" == "1" ]] || die "SSH key missing: ${SSH_KEY}"
  log "creating SSH key ${SSH_KEY}"
  ssh-keygen -t ed25519 -a 64 -f "${SSH_KEY}" -C "proxmox-dev-${ALIAS}" -N ""
fi
[[ -f "${SSH_KEY}.pub" ]] || die "public key missing: ${SSH_KEY}.pub"

scan_tmp="$(mktemp)"
config_tmp=""
cleanup() {
  rm -f "${scan_tmp}" "${scan_tmp}.old"
  if [[ -n "${config_tmp}" ]]; then
    rm -f "${config_tmp}"
  fi
}
trap cleanup EXIT

if [[ "${REFRESH_HOST_KEY}" == "1" ]]; then
  log "removing existing known_hosts entries for ${HOST}:${SSH_PORT}"
  ssh-keygen -R "${HOST}" -f "${KNOWN_HOSTS}" >/dev/null 2>&1 || true
  ssh-keygen -R "[${HOST}]:${SSH_PORT}" -f "${KNOWN_HOSTS}" >/dev/null 2>&1 || true
fi

log "scanning SSH host keys from ${HOST}:${SSH_PORT}"
if ! ssh-keyscan -T 8 -p "${SSH_PORT}" -t ed25519,ecdsa,rsa "${HOST}" > "${scan_tmp}" 2>/dev/null; then
  die "ssh-keyscan failed for ${HOST}:${SSH_PORT}"
fi
[[ -s "${scan_tmp}" ]] || die "no SSH host keys returned by ${HOST}:${SSH_PORT}"

echo
echo "Host key fingerprints for ${HOST}:${SSH_PORT}:"
ssh-keygen -lf "${scan_tmp}" || true
echo

if confirm "Trust and pin these SSH host keys"; then
  ssh-keygen -R "${HOST}" -f "${KNOWN_HOSTS}" >/dev/null 2>&1 || true
  ssh-keygen -R "[${HOST}]:${SSH_PORT}" -f "${KNOWN_HOSTS}" >/dev/null 2>&1 || true
  cat "${scan_tmp}" >> "${KNOWN_HOSTS}"
  chmod 600 "${KNOWN_HOSTS}"
else
  die "host key trust was not confirmed"
fi

if [[ "${INSTALL_KEY}" == "1" ]]; then
  pub_key="$(cat "${SSH_KEY}.pub")"
  quoted_pub_key="$(single_quote "${pub_key}")"
  log "installing public key on ${SSH_USER}@${HOST}; enter the PVE password if prompted"
  ssh -p "${SSH_PORT}" \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" \
    -o StrictHostKeyChecking=yes \
    -i "${SSH_KEY}" \
    "${SSH_USER}@${HOST}" \
    "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; grep -qxF ${quoted_pub_key} ~/.ssh/authorized_keys || printf '%s\n' ${quoted_pub_key} >> ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys"
fi

marker_begin="# >>> proxmoxveautopilot ${ALIAS}"
marker_end="# <<< proxmoxveautopilot ${ALIAS}"
config_tmp="$(mktemp)"
awk -v begin="${marker_begin}" -v end="${marker_end}" '
  $0 == begin { skip = 1; next }
  $0 == end { skip = 0; next }
  skip != 1 { print }
' "${SSH_CONFIG}" > "${config_tmp}"
{
  printf '%s\n' "${marker_begin}"
  printf 'Host %s\n' "${ALIAS}"
  printf '  HostName %s\n' "${HOST}"
  printf '  User %s\n' "${SSH_USER}"
  printf '  Port %s\n' "${SSH_PORT}"
  printf '  IdentityFile %s\n' "${SSH_KEY}"
  printf '  IdentitiesOnly yes\n'
  printf '  StrictHostKeyChecking yes\n'
  printf '  UserKnownHostsFile %s\n' "${KNOWN_HOSTS}"
  printf '%s\n' "${marker_end}"
} >> "${config_tmp}"
mv "${config_tmp}" "${SSH_CONFIG}"
config_tmp=""
chmod 600 "${SSH_CONFIG}"

log "verifying SSH alias ${ALIAS}"
ssh -F "${SSH_CONFIG}" "${ALIAS}" 'printf "connected "; hostname; command -v pveversion >/dev/null 2>&1 && pveversion || true'

cat <<EOF

Ready.

Connect:
  ssh ${ALIAS}

Copy files to the node:
  ssh ${ALIAS} 'mkdir -p /opt/ProxmoxVEAutopilot'
  rsync -a --delete --no-owner --no-group \\
    --exclude 'autopilot-proxmox/.env' \\
    --exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml' \\
    --exclude 'autopilot-proxmox/secrets/' \\
    --exclude 'autopilot-proxmox/output/' \\
    '${REPO_ROOT}/' ${ALIAS}:/opt/ProxmoxVEAutopilot/
  ssh ${ALIAS} 'chown -R root:root /opt/ProxmoxVEAutopilot'

When the PVE init entrypoint is present, run:
  ssh ${ALIAS} 'bash /opt/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume'
EOF
