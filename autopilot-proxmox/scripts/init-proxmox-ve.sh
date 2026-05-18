#!/usr/bin/env bash
# First-run Proxmox VE entrypoint for ProxmoxVEAutopilot.
#
# Runs on the PVE shell. This script keeps the hypervisor clean: it configures
# PVE API access, creates/boots the Ubuntu controller VM, transfers source and
# secrets, and then hands runtime/bootstrap work to the controller.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${APP_DIR}/.." && pwd)"
SETUP_DIR="${APP_DIR}/output/setup"
STATE_FILE="${SETUP_DIR}/foundation_state.json"
ENV_FILE="${APP_DIR}/.env"
VARS_FILE="${APP_DIR}/inventory/group_vars/all/vars.yml"
VAULT_FILE="${APP_DIR}/inventory/group_vars/all/vault.yml"
SECRETS_DIR="${APP_DIR}/secrets"
MIGRATION_DIR="${SETUP_DIR}/migration"
PVE_RUNTIME_DIR="${PVE_RUNTIME_DIR:-/root/.local/share/proxmoxveautopilot}"

API_USER="${API_USER:-autopilot@pve}"
API_TOKEN_NAME="${API_TOKEN_NAME:-ansible}"
API_TOKEN_ID="${API_USER}!${API_TOKEN_NAME}"
ROLE_NAME="${ROLE_NAME:-AutopilotProvisioner}"
CONTROLLER_NAME="${CONTROLLER_NAME:-autopilot-controller-01}"
CONTROLLER_USER="${CONTROLLER_USER:-autopilot}"
BUILDHOST_NAME="${BUILDHOST_NAME:-autopilot-buildhost-01}"
CONTROLLER_MEMORY_MB="${CONTROLLER_MEMORY_MB:-8192}"
CONTROLLER_CORES="${CONTROLLER_CORES:-4}"
CONTROLLER_DISK_GB="${CONTROLLER_DISK_GB:-128}"
CONTROLLER_START="${CONTROLLER_START:-1}"
CONTROLLER_REMOTE_ROOT="${CONTROLLER_REMOTE_ROOT:-/opt/ProxmoxVEAutopilot}"
CONTROLLER_REMOTE_APP="${CONTROLLER_REMOTE_ROOT}/autopilot-proxmox"
CONTROLLER_SSH_KEY="${CONTROLLER_SSH_KEY:-${PVE_RUNTIME_DIR}/controller-bootstrap-ed25519}"
CONTROLLER_KNOWN_HOSTS="${CONTROLLER_KNOWN_HOSTS:-/root/.ssh/known_hosts}"
PVE_ROOT_SSH_KEY="${PVE_ROOT_SSH_KEY:-${PVE_RUNTIME_DIR}/pve-root-ed25519}"
UBUNTU_CLOUD_IMAGE_URL="${UBUNTU_CLOUD_IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
UBUNTU_CLOUD_IMAGE_PATH="${SETUP_DIR}/images/noble-server-cloudimg-amd64.img"

PHASE="all"
RESUME=0
WAIT_FOR_MEDIA=0
WINDOWS_ISO_URL=""
DOWNLOAD_WINDOWS=0
WINDOWS_ISO_LANGUAGE="${WINDOWS_ISO_LANGUAGE:-English}"
WINDOWS_ISO_LOCALE="${WINDOWS_ISO_LOCALE:-en-us}"
WINDOWS_ISO_EDITION_ID="${WINDOWS_ISO_EDITION_ID:-3321}"
DOWNLOAD_VIRTIO=0
NODE=""
ISO_STORAGE=""
NON_INTERACTIVE=0
CONTROLLER_IP=""
CONTROLLER_CIDR="24"
CONTROLLER_GATEWAY=""
CONTROLLER_DNS=""
CONTROLLER_VMID=""
CONTROLLER_STORAGE=""
CONTROLLER_BRIDGE=""
RESET_MEDIA=0

PRIVILEGES="VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.CDROM,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Audit,VM.PowerMgmt,VM.Console,VM.Snapshot,VM.Snapshot.Rollback,VM.GuestAgent.Audit,VM.GuestAgent.FileRead,VM.GuestAgent.FileWrite,VM.GuestAgent.FileSystemMgmt,VM.GuestAgent.Unrestricted,Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use"

usage() {
  cat <<'USAGE'
Usage:
  init-proxmox-ve.sh [options]

Options:
  --phase foundation|bootstrap|operational|runtime-config|reset-dev-lab|all
  --resume
  --wait-for-media
  --download-windows
  --windows-iso-language <language>
  --windows-iso-url <official-direct-url>
  --download-virtio
  --node <pve-node>
  --iso-storage <storage>
  --controller-ip <ip>
  --controller-cidr <prefix-length>
  --controller-gateway <ip>
  --controller-dns <ip>
  --controller-vmid <vmid>
  --controller-storage <storage>
  --controller-bridge <bridge>
  --reset-media
  --non-interactive
  --help

Typical first run on a disposable dev PVE box:
  bash scripts/init-proxmox-ve.sh --phase foundation --resume

Bootstrap with assisted official media downloads:
  bash scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-windows --download-virtio

Static controller example:
  bash scripts/init-proxmox-ve.sh --phase foundation --controller-ip 192.168.2.181 --controller-gateway 192.168.2.1 --controller-dns 192.168.2.1

Clean disposable dev lab and generated media:
  bash scripts/init-proxmox-ve.sh --phase reset-dev-lab --reset-media --non-interactive
USAGE
}

log() {
  printf '[pve-init] %s\n' "$*" >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_root() {
  [[ "$(id -u)" == "0" ]] || die "run this script as root on the Proxmox VE host"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

state_set() {
  local key="$1"
  local value_json="$2"
  mkdir -p "${SETUP_DIR}"
  python3 - "${STATE_FILE}" "${key}" "${value_json}" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = json.loads(sys.argv[3])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {"schema_version": 1}
data[key] = value
data["updated_at"] = datetime.now(timezone.utc).isoformat()
path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_name, path)
finally:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
PY
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

state_bool() {
  state_set "$1" "$2"
}

state_text() {
  state_set "$1" "$(json_string "$2")"
}

state_value() {
  python3 - "${STATE_FILE}" "$1" <<'PY'
import json
import sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    data = {}
value = data.get(sys.argv[2], "")
print(value if isinstance(value, str) else "")
PY
}

vars_value() {
  python3 - "${VARS_FILE}" "$1" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
if not path.exists():
    raise SystemExit
match = re.search(rf"^{re.escape(key)}:\s*[\"']?([^\"'\n#]+)", path.read_text(encoding="utf-8"), re.M)
if match:
    print(match.group(1).strip())
PY
}

host_ip() {
  ip -4 -o addr show scope global \
    | awk '$2 ~ /^vmbr/ {split($4,a,"/"); selected=a[1]; exit} !fallback {fallback=$4} END {if (selected) print selected; else if (fallback) {split(fallback,a,"/"); print a[1]}}'
}

detect_node() {
  if [[ -n "${NODE}" ]]; then
    printf '%s' "${NODE}"
  else
    hostname
  fi
}

detect_iso_storage() {
  if [[ -n "${ISO_STORAGE}" ]]; then
    printf '%s' "${ISO_STORAGE}"
    return
  fi
  pvesm status --content iso 2>/dev/null | awk 'NR > 1 {print $1; exit}'
}

list_iso_storages() {
  if [[ -n "${ISO_STORAGE}" ]]; then
    printf '%s\n' "${ISO_STORAGE}"
    return
  fi
  pvesm status --content iso 2>/dev/null | awk 'NR > 1 {print $1}'
}

detect_disk_storage() {
  if [[ -n "${CONTROLLER_STORAGE}" ]]; then
    printf '%s' "${CONTROLLER_STORAGE}"
    return
  fi
  pvesm status --content images 2>/dev/null \
    | awk 'NR > 1 && $1 ~ /zfs|local-zfs/ {print $1; exit} NR > 1 && !first {first=$1} END {if (first) print first}'
}

detect_bridge() {
  if [[ -n "${CONTROLLER_BRIDGE}" ]]; then
    printf '%s' "${CONTROLLER_BRIDGE}"
    return
  fi
  ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2; exit}'
}

detect_cloudosd_blank_template_vmid() {
  qm list 2>/dev/null | awk '$2 == "autopilot-cloudosd-blank-template" { print $1; exit }'
}

detect_osdeploy_blank_template_vmid() {
  local configured
  configured="$(vars_value osdeploy_blank_template_vmid || true)"
  if [[ "${configured}" =~ ^[0-9]+$ ]] && qm config "${configured}" >/dev/null 2>&1; then
    printf '%s' "${configured}"
    return
  fi
  qm list 2>/dev/null | awk '$2 == "autopilot-osdeploy-blank-template" { print $1; exit }'
}

storage_path() {
  local storage="$1"
  awk -v storage="${storage}" '
    $1 ~ /:$/ { in_block = ($2 == storage); next }
    in_block && $1 == "path" { print $2; exit }
  ' /etc/pve/storage.cfg
}

iso_dir_for_storage() {
  local storage="$1"
  local base
  base="$(storage_path "${storage}")"
  if [[ -z "${base}" && "${storage}" == "local" ]]; then
    base="/var/lib/vz"
  fi
  [[ -n "${base}" ]] || die "could not resolve path for ISO storage ${storage}"
  printf '%s/template/iso' "${base}"
}

repair_clock() {
  log "checking clock and NTP"
  timedatectl set-ntp true >/dev/null 2>&1 || true
  systemctl restart chrony >/dev/null 2>&1 || true
  chronyc -a burst 4/4 >/dev/null 2>&1 || true
  chronyc -a makestep >/dev/null 2>&1 || true

  if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo no)" != "yes" ]]; then
    log "NTP is not synchronized yet; using Debian HTTP Date header as a one-time clock seed"
    local http_date
    http_date="$(python3 - <<'PY' || true
from urllib.request import Request, urlopen
req = Request("http://deb.debian.org/debian/", method="HEAD")
with urlopen(req, timeout=10) as resp:
    print(resp.headers.get("Date", ""))
PY
)"
    if [[ -n "${http_date}" ]]; then
      date -u -s "${http_date}" >/dev/null 2>&1 || true
    fi
  fi

  state_text clock "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}

repair_apt_sources() {
  log "configuring Proxmox no-subscription apt sources for dev"
  mkdir -p /etc/apt/sources.list.d/autopilot-disabled
  cat >/etc/apt/apt.conf.d/99autopilot-force-ipv4 <<'EOF'
Acquire::ForceIPv4 "true";
EOF
  shopt -s nullglob
  local file
  for file in /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do
    if grep -q "enterprise.proxmox.com" "${file}"; then
      mv -f "${file}" "/etc/apt/sources.list.d/autopilot-disabled/$(basename "${file}").disabled-by-autopilot"
      log "disabled enterprise source ${file}"
    fi
  done
  shopt -u nullglob

  cat >/etc/apt/sources.list.d/pve-no-subscription.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

  cat >/etc/apt/sources.list.d/ceph-no-subscription.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/ceph-squid
Suites: trixie
Components: no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

  apt-get update
  state_bool apt_ready true
}

install_host_essentials() {
  log "installing PVE bootstrap essentials"
  local packages=(
    ca-certificates
    curl
    git
    jq
    openssh-client
    openssl
    python3
    rsync
    genisoimage
    xorriso
  )
  apt-get install -y "${packages[@]}"
  state_bool pve_foundation_ready true
  state_bool pve_host_clean_ready true
}

secret_file_value() {
  local path="$1"
  if [[ ! -s "${path}" ]]; then
    openssl rand -hex 48 >"${path}"
    chmod 600 "${path}"
  fi
  tr -d '\n' <"${path}"
}

sha256_text() {
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$1"
}

ensure_runtime_secrets() {
  log "creating controller runtime secrets"
  mkdir -p "${SECRETS_DIR}" "${SETUP_DIR}" "${APP_DIR}/output" "${APP_DIR}/jobs"
  chmod 700 "${SECRETS_DIR}"

  local postgres_password mcp_token fleet_token fleet_hash base_url controller_ip
  postgres_password="$(secret_file_value "${SECRETS_DIR}/postgres-password")"
  mcp_token="$(secret_file_value "${SECRETS_DIR}/mcp-token")"
  fleet_token="$(secret_file_value "${SECRETS_DIR}/fleet-bootstrap-token")"
  fleet_hash="$(sha256_text "${fleet_token}")"
  controller_ip="${CONTROLLER_IP:-$(state_value controller_ip)}"
  if [[ -n "${controller_ip}" ]]; then
    base_url="http://${controller_ip}:5000"
  else
    base_url="http://autopilot-controller-01:5000"
  fi

  cat >"${ENV_FILE}" <<EOF
AUTOPILOT_POSTGRES_PASSWORD=${postgres_password}
AUTOPILOT_MCP_TOKEN=${mcp_token}
AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256=${fleet_hash}
AUTOPILOT_BASE_URL=${base_url}
AUTOPILOT_AUTH_MODE=local
EOF
  chmod 600 "${ENV_FILE}"
  state_bool secrets_ready true
  state_text controller_auth_mode "local"
  state_text base_url "${base_url}"
}

ensure_pve_token_and_vault() {
  log "creating Proxmox API user/token and vault.yml"
  pveum user add "${API_USER}" --comment "Autopilot provisioning" >/dev/null 2>&1 \
    || pveum user modify "${API_USER}" --comment "Autopilot provisioning" >/dev/null 2>&1 \
    || true

  local existing_secret token_secret token_json winpe_secret token_exists token_valid
  existing_secret="$(python3 - "${VAULT_FILE}" <<'PY' || true
import re
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit
text = path.read_text(encoding="utf-8")
match = re.search(r"^vault_proxmox_api_token_secret:\s*[\"']?([^\"'\n]+)", text, re.M)
if match:
    value = match.group(1).strip()
    if value and value != "YOUR-TOKEN-SECRET-HERE":
        print(value)
PY
)"

  token_exists=0
  if pveum user token list "${API_USER}" --output-format json 2>/dev/null \
    | python3 -c '
import json
import sys
name = sys.argv[1]
full = sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
rows = data if isinstance(data, list) else data.get("data", [])
for row in rows:
    if row.get("tokenid") == name or row.get("full-tokenid") == full:
        raise SystemExit(0)
raise SystemExit(1)
' "${API_TOKEN_NAME}" "${API_TOKEN_ID}"; then
    token_exists=1
  fi

  token_valid=0
  if [[ -n "${existing_secret}" && "${token_exists}" == "1" ]]; then
    if curl -fsSk \
      -H "Authorization: PVEAPIToken=${API_TOKEN_ID}=${existing_secret}" \
      "https://127.0.0.1:8006/api2/json/version" >/dev/null 2>&1; then
      token_valid=1
    fi
  fi

  if [[ -n "${existing_secret}" && "${token_exists}" == "1" && "${token_valid}" == "1" ]]; then
    token_secret="${existing_secret}"
    log "using existing token secret from vault.yml"
  else
    if [[ -n "${existing_secret}" && "${token_exists}" == "1" ]]; then
      log "vault token secret exists but PVE rejected it; rotating dev token"
    elif [[ -n "${existing_secret}" ]]; then
      log "vault token secret exists but PVE token is missing; recreating dev token"
    fi
    pveum user token remove "${API_USER}" "${API_TOKEN_NAME}" >/dev/null 2>&1 || true
    token_json="$(pveum user token add "${API_USER}" "${API_TOKEN_NAME}" \
      --privsep=0 \
      --comment "ProxmoxVEAutopilot dev automation" \
      --output-format json)"
    token_secret="$(printf '%s' "${token_json}" | python3 -c '
import json
import sys
data = json.load(sys.stdin)
for key in ("value", "secret"):
    if data.get(key):
        print(data[key])
        raise SystemExit
if isinstance(data.get("data"), dict):
    for key in ("value", "secret"):
        if data["data"].get(key):
            print(data["data"][key])
            raise SystemExit
raise SystemExit("token secret not found in pveum output")
'
)"
  fi

  winpe_secret="$(secret_file_value "${SECRETS_DIR}/winpe-token-secret")"
  mkdir -p "$(dirname "${VAULT_FILE}")"
  cat >"${VAULT_FILE}" <<EOF
---
vault_proxmox_api_token_id: "${API_TOKEN_ID}"
vault_proxmox_api_token_secret: "${token_secret}"
vault_proxmox_root_username: "root@pam"
vault_proxmox_root_password: ""
vault_entra_app_id: ""
vault_entra_tenant_id: ""
vault_entra_app_secret: ""
vault_autopilot_winpe_token_secret: "${winpe_secret}"
EOF
  chmod 600 "${VAULT_FILE}"
  state_bool pve_token_ready true
}

repair_pve_permissions() {
  log "repairing Proxmox role, ACLs, snippets, and chassis binaries"
  local node iso_storage disk_storage snippet_storage
  node="$(detect_node)"
  iso_storage="$(detect_iso_storage)"
  disk_storage="$(detect_disk_storage)"
  snippet_storage="local"

  [[ -n "${iso_storage}" ]] || die "no ISO-capable storage found"
  [[ -n "${disk_storage}" ]] || die "no VM image storage found"

  if pveum role add "${ROLE_NAME}" -privs "${PRIVILEGES}" >/dev/null 2>&1; then
    log "created role ${ROLE_NAME}"
  else
    pveum role modify "${ROLE_NAME}" -privs "${PRIVILEGES}" >/dev/null
  fi
  pveum acl modify / -user "${API_USER}" -role "${ROLE_NAME}" >/dev/null
  for storage in "${iso_storage}" "${disk_storage}" "${snippet_storage}"; do
    [[ -n "${storage}" ]] || continue
    pveum acl modify "/storage/${storage}" -user "${API_USER}" -role "${ROLE_NAME}" >/dev/null
  done

  local current next
  current="$(awk -v storage="${snippet_storage}" '
    $1 ~ /:$/ { in_block = ($2 == storage); next }
    in_block && $1 == "content" { print $2; exit }
  ' /etc/pve/storage.cfg)"
  case ",${current}," in
    *,snippets,*) next="${current}" ;;
    *) next="${current:+${current},}snippets" ;;
  esac
  pvesm set "${snippet_storage}" --content "${next}" >/dev/null
  python3 "${SCRIPT_DIR}/seed_chassis_binaries.py" >/dev/null

  state_bool pve_permissions_ready true
  state_text pve_node "${node}"
  state_text pve_iso_storage "${iso_storage}"
  state_text pve_disk_storage "${disk_storage}"
}

update_vars_yml() {
  log "writing PVE API defaults into vars.yml"
  local node iso_storage disk_storage bridge ip cloudosd_template_vmid osdeploy_template_vmid
  node="$(detect_node)"
  iso_storage="$(detect_iso_storage)"
  disk_storage="$(detect_disk_storage)"
  bridge="$(detect_bridge)"
  ip="$(host_ip)"
  cloudosd_template_vmid="$(detect_cloudosd_blank_template_vmid)"
  osdeploy_template_vmid="$(detect_osdeploy_blank_template_vmid)"
  [[ -n "${iso_storage}" ]] || die "no ISO-capable storage found"
  [[ -n "${disk_storage}" ]] || die "no VM image storage found"
  [[ -n "${bridge}" ]] || die "no network bridge found"
  [[ -n "${ip}" ]] || die "could not detect PVE LAN IP"

  PVE_INIT_NODE="${node}" \
  PVE_INIT_HOST="${ip}" \
  PVE_INIT_ISO_STORAGE="${iso_storage}" \
  PVE_INIT_DISK_STORAGE="${disk_storage}" \
  PVE_INIT_BRIDGE="${bridge}" \
  PVE_INIT_CLOUDOSD_TEMPLATE_VMID="${cloudosd_template_vmid}" \
  PVE_INIT_OSDEPLOY_TEMPLATE_VMID="${osdeploy_template_vmid}" \
  python3 - "${VARS_FILE}" <<'PY'
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "hypervisor_type": "proxmox",
    "proxmox_host": os.environ["PVE_INIT_HOST"],
    "proxmox_port": 8006,
    "proxmox_api_base": "https://{{ proxmox_host }}:{{ proxmox_port }}/api2/json",
    "proxmox_api_auth_header": "PVEAPIToken={{ vault_proxmox_api_token_id }}={{ vault_proxmox_api_token_secret }}",
    "proxmox_node": os.environ["PVE_INIT_NODE"],
    "proxmox_validate_certs": False,
    "proxmox_storage": os.environ["PVE_INIT_DISK_STORAGE"],
    "proxmox_iso_storage": os.environ["PVE_INIT_ISO_STORAGE"],
    "proxmox_bridge": os.environ["PVE_INIT_BRIDGE"],
    "proxmox_root_ssh_key_path": "/app/secrets/pve-root-ed25519",
    "proxmox_vlan_tag": None,
}
cloudosd_template_vmid = os.environ.get("PVE_INIT_CLOUDOSD_TEMPLATE_VMID", "").strip()
if cloudosd_template_vmid:
    updates["cloudosd_blank_template_vmid"] = int(cloudosd_template_vmid)
osdeploy_template_vmid = os.environ.get("PVE_INIT_OSDEPLOY_TEMPLATE_VMID", "").strip()
if osdeploy_template_vmid:
    updates["osdeploy_blank_template_vmid"] = int(osdeploy_template_vmid)

def yaml_value(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

seen = set()
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["---"]
out = []
for line in lines:
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
    if match and match.group(1) in updates:
        key = match.group(1)
        out.append(f"{key}: {yaml_value(updates[key])}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}: {yaml_value(value)}")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  state_bool repo_config_ready true
  state_text pve_bridge "${bridge}"
  state_text pve_host_ip "${ip}"
}

ensure_pve_root_ssh_key() {
  log "repairing controller-to-PVE root SSH key"
  mkdir -p "${PVE_RUNTIME_DIR}" "${SECRETS_DIR}" /root/.ssh
  chmod 700 "${PVE_RUNTIME_DIR}" "${SECRETS_DIR}" /root/.ssh
  if [[ ! -s "${PVE_ROOT_SSH_KEY}" ]]; then
    ssh-keygen -q -t ed25519 -N "" -f "${PVE_ROOT_SSH_KEY}" -C "autopilot-controller-to-pve-root" >/dev/null
  fi
  chmod 600 "${PVE_ROOT_SSH_KEY}"
  chmod 644 "${PVE_ROOT_SSH_KEY}.pub"

  local pubkey
  pubkey="$(tr -d '\n' <"${PVE_ROOT_SSH_KEY}.pub")"
  touch /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  if ! grep -qxF "${pubkey}" /root/.ssh/authorized_keys; then
    printf '\n%s\n' "${pubkey}" >>/root/.ssh/authorized_keys
  fi

  cp -p "${PVE_ROOT_SSH_KEY}" "${SECRETS_DIR}/pve-root-ed25519"
  cp -p "${PVE_ROOT_SSH_KEY}.pub" "${SECRETS_DIR}/pve-root-ed25519.pub"
  chmod 600 "${SECRETS_DIR}/pve-root-ed25519"
  chmod 644 "${SECRETS_DIR}/pve-root-ed25519.pub"
  state_bool pve_root_ssh_key_ready true
  state_text pve_root_ssh_key_path "/app/secrets/pve-root-ed25519"
}

write_osdeploy_blank_template_vmid() {
  local vmid="$1"
  PVE_INIT_OSDEPLOY_TEMPLATE_VMID="${vmid}" python3 - "${VARS_FILE}" <<'PY'
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
vmid = os.environ["PVE_INIT_OSDEPLOY_TEMPLATE_VMID"].strip()
updates = {"osdeploy_blank_template_vmid": int(vmid)}
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["---"]
seen = set()
out = []
for line in lines:
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
    if match and match.group(1) in updates:
        key = match.group(1)
        out.append(f"{key}: {updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}: {value}")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

ensure_osdeploy_blank_template() {
  local existing desired vmid disk_storage bridge
  existing="$(detect_osdeploy_blank_template_vmid)"
  if [[ -n "${existing}" ]]; then
    write_osdeploy_blank_template_vmid "${existing}"
    state_bool osdeploy_blank_template_ready true
    state_text osdeploy_blank_template_vmid "${existing}"
    state_text osdeploy_blank_template_name "autopilot-osdeploy-blank-template"
    return 0
  fi

  desired="$(vars_value osdeploy_blank_template_vmid || true)"
  if [[ ! "${desired}" =~ ^[0-9]+$ ]]; then
    desired="9001"
  fi
  if qm config "${desired}" >/dev/null 2>&1; then
    vmid="$(pvesh get /cluster/nextid)"
  else
    vmid="${desired}"
  fi

  disk_storage="$(detect_disk_storage)"
  bridge="$(detect_bridge)"
  [[ -n "${disk_storage}" ]] || die "no VM image storage found"
  [[ -n "${bridge}" ]] || die "no network bridge found"

  log "creating OSDeploy blank template VMID ${vmid}"
  qm create "${vmid}" \
    --name "autopilot-osdeploy-blank-template" \
    --memory 2048 \
    --cores 2 \
    --cpu host \
    --ostype win11 \
    --machine q35 \
    --bios ovmf \
    --agent enabled=1 \
    --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=${bridge}" \
    --boot order=scsi0
  qm set "${vmid}" --efidisk0 "${disk_storage}:1,efitype=4m,pre-enrolled-keys=1" >/dev/null
  qm set "${vmid}" --tpmstate0 "${disk_storage}:4,version=v2.0" >/dev/null
  qm set "${vmid}" --scsi0 "${disk_storage}:8,discard=on,iothread=1" >/dev/null
  qm template "${vmid}"
  write_osdeploy_blank_template_vmid "${vmid}"
  state_bool osdeploy_blank_template_ready true
  state_text osdeploy_blank_template_vmid "${vmid}"
  state_text osdeploy_blank_template_name "autopilot-osdeploy-blank-template"
}

repair_pve_access_contract() {
  ensure_runtime_secrets
  ensure_pve_token_and_vault
  repair_pve_permissions
  ensure_pve_root_ssh_key
  update_vars_yml
  ensure_osdeploy_blank_template
  state_bool pve_access_contract_ready true
}

create_migration_bundle() {
  log "creating non-destructive migration bundle from any PVE-host runtime state"
  mkdir -p "${MIGRATION_DIR}"
  local stamp bundle_dir bundle_tar
  stamp="$(date -u +"%Y%m%dT%H%M%SZ")"
  bundle_dir="${MIGRATION_DIR}/autopilot-pve-migration-${stamp}"
  bundle_tar="${bundle_dir}.tar.gz"
  rm -rf "${bundle_dir}"
  mkdir -p "${bundle_dir}"

  [[ -f "${ENV_FILE}" ]] && cp -p "${ENV_FILE}" "${bundle_dir}/.env"
  if [[ -d "${SECRETS_DIR}" ]]; then
    mkdir -p "${bundle_dir}/secrets"
    rsync -a "${SECRETS_DIR}/" "${bundle_dir}/secrets/"
  fi
  if [[ -f "${VARS_FILE}" || -f "${VAULT_FILE}" ]]; then
    mkdir -p "${bundle_dir}/inventory/group_vars/all"
    [[ -f "${VARS_FILE}" ]] && cp -p "${VARS_FILE}" "${bundle_dir}/inventory/group_vars/all/vars.yml"
    [[ -f "${VAULT_FILE}" ]] && cp -p "${VAULT_FILE}" "${bundle_dir}/inventory/group_vars/all/vault.yml"
  fi
  if [[ -d "${SETUP_DIR}" ]]; then
    mkdir -p "${bundle_dir}/output/setup"
    rsync -a --exclude 'migration/' "${SETUP_DIR}/" "${bundle_dir}/output/setup/"
  fi

  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx 'autopilot-postgres'; then
    log "exporting existing PVE-host Postgres database"
    if docker exec autopilot-postgres pg_dump -U autopilot autopilot >"${bundle_dir}/postgres.sql" 2>"${bundle_dir}/postgres-dump.stderr"; then
      state_bool migration_postgres_dump_ready true
    else
      log "Postgres dump failed; continuing with file migration bundle"
      state_bool migration_postgres_dump_ready false
    fi
  fi

  tar -C "${bundle_dir}" -czf "${bundle_tar}" .
  chmod 600 "${bundle_tar}" || true
  state_text migration_bundle "${bundle_tar}"
  state_bool migration_bundle_ready true
  printf '%s' "${bundle_tar}"
}

download_ubuntu_cloud_image() {
  mkdir -p "$(dirname "${UBUNTU_CLOUD_IMAGE_PATH}")"
  if [[ -s "${UBUNTU_CLOUD_IMAGE_PATH}" ]]; then
    state_text controller_cloud_image "${UBUNTU_CLOUD_IMAGE_PATH}"
    return 0
  fi
  log "downloading Ubuntu 24.04 LTS cloud image"
  curl -fL -C - --retry 3 -o "${UBUNTU_CLOUD_IMAGE_PATH}.part" "${UBUNTU_CLOUD_IMAGE_URL}"
  mv -f "${UBUNTU_CLOUD_IMAGE_PATH}.part" "${UBUNTU_CLOUD_IMAGE_PATH}"
  state_text controller_cloud_image "${UBUNTU_CLOUD_IMAGE_PATH}"
}

ensure_controller_ssh_key() {
  mkdir -p "${SECRETS_DIR}" "$(dirname "${CONTROLLER_SSH_KEY}")"
  chmod 700 "${SECRETS_DIR}" "$(dirname "${CONTROLLER_SSH_KEY}")"
  if [[ ! -s "${CONTROLLER_SSH_KEY}" ]]; then
    ssh-keygen -q -t ed25519 -N "" -f "${CONTROLLER_SSH_KEY}" -C "autopilot-controller-bootstrap" >/dev/null
  fi
  chmod 600 "${CONTROLLER_SSH_KEY}"
  chmod 644 "${CONTROLLER_SSH_KEY}.pub"
  state_text controller_ssh_public_key_path "${CONTROLLER_SSH_KEY}.pub"
}

controller_vm_id_by_name() {
  qm list | awk -v name="${CONTROLLER_NAME}" 'NR > 1 && $2 == name {print $1; exit}'
}

write_controller_cloud_init_snippet() {
  local controller_vmid="$1"
  local snippet_dir="/var/lib/vz/snippets"
  local snippet="${snippet_dir}/autopilot-controller-${controller_vmid}-user.yml"
  local pubkey
  pubkey="$(tr -d '\n' <"${CONTROLLER_SSH_KEY}.pub")"
  mkdir -p "${snippet_dir}"
  cat >"${snippet}" <<EOF
#cloud-config
package_update: true
packages:
  - openssh-server
  - qemu-guest-agent
ssh_pwauth: false
users:
  - default
  - name: ${CONTROLLER_USER}
    groups: sudo
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - ${pubkey}
runcmd:
  - systemctl enable --now ssh
  - systemctl enable --now qemu-guest-agent
EOF
  printf 'local:snippets/%s' "$(basename "${snippet}")"
}

extract_imported_disk_volid() {
  local output="$1"
  local storage="$2"
  local controller_vmid="$3"
  local volid
  volid="$(printf '%s\n' "${output}" | sed -n "s/.*'\([^']*\)'.*/\1/p" | tail -n 1)"
  if [[ -n "${volid}" ]]; then
    printf '%s' "${volid}"
    return
  fi
  pvesm list "${storage}" 2>/dev/null \
    | awk -v needle="vm-${controller_vmid}-disk" '$1 ~ needle {print $1; exit}'
}

create_controller_vm() {
  local existing controller_vmid disk_storage bridge ipconfig0 snippet import_output imported_disk
  ensure_controller_ssh_key
  existing="$(controller_vm_id_by_name)"
  if [[ -n "${existing}" ]]; then
    log "controller VM already exists: ${CONTROLLER_NAME} VMID ${existing}"
    state_bool controller_vm_ready true
    state_text controller_vmid "${existing}"
    state_text controller_name "${CONTROLLER_NAME}"
    return 0
  fi

  download_ubuntu_cloud_image
  disk_storage="$(detect_disk_storage)"
  bridge="$(detect_bridge)"
  [[ -n "${disk_storage}" ]] || die "no VM image storage found"
  [[ -n "${bridge}" ]] || die "no network bridge found"
  controller_vmid="${CONTROLLER_VMID:-$(pvesh get /cluster/nextid)}"
  snippet="$(write_controller_cloud_init_snippet "${controller_vmid}")"

  log "creating Ubuntu controller VM ${CONTROLLER_NAME} VMID ${controller_vmid}"
  qm create "${controller_vmid}" \
    --name "${CONTROLLER_NAME}" \
    --memory "${CONTROLLER_MEMORY_MB}" \
    --cores "${CONTROLLER_CORES}" \
    --cpu host \
    --ostype l26 \
    --machine q35 \
    --bios ovmf \
    --agent enabled=1 \
    --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=${bridge}" \
    --serial0 socket \
    --vga serial0
  qm set "${controller_vmid}" --efidisk0 "${disk_storage}:1,efitype=4m,pre-enrolled-keys=1" >/dev/null
  import_output="$(qm importdisk "${controller_vmid}" "${UBUNTU_CLOUD_IMAGE_PATH}" "${disk_storage}" 2>&1)"
  imported_disk="$(extract_imported_disk_volid "${import_output}" "${disk_storage}" "${controller_vmid}")"
  [[ -n "${imported_disk}" ]] || die "could not determine imported controller disk volid: ${import_output}"
  qm set "${controller_vmid}" --scsi0 "${imported_disk},discard=on,iothread=1" >/dev/null
  qm resize "${controller_vmid}" scsi0 "${CONTROLLER_DISK_GB}G" >/dev/null || true
  qm set "${controller_vmid}" --ide2 "${disk_storage}:cloudinit" >/dev/null
  qm set "${controller_vmid}" --boot order=scsi0 >/dev/null
  qm set "${controller_vmid}" --ciuser "${CONTROLLER_USER}" --sshkeys "${CONTROLLER_SSH_KEY}.pub" >/dev/null
  qm set "${controller_vmid}" --cicustom "user=${snippet}" >/dev/null

  if [[ -n "${CONTROLLER_IP}" ]]; then
    ipconfig0="ip=${CONTROLLER_IP}/${CONTROLLER_CIDR}"
    [[ -n "${CONTROLLER_GATEWAY}" ]] && ipconfig0="${ipconfig0},gw=${CONTROLLER_GATEWAY}"
  else
    ipconfig0="ip=dhcp"
  fi
  qm set "${controller_vmid}" --ipconfig0 "${ipconfig0}" >/dev/null
  if [[ -n "${CONTROLLER_DNS}" ]]; then
    qm set "${controller_vmid}" --nameserver "${CONTROLLER_DNS}" >/dev/null
  fi

  if [[ "${CONTROLLER_START}" == "1" ]]; then
    qm start "${controller_vmid}"
  fi

  state_bool controller_vm_ready true
  state_text controller_vmid "${controller_vmid}"
  state_text controller_name "${CONTROLLER_NAME}"
  state_text controller_node "$(detect_node)"
  state_text controller_storage "${disk_storage}"
  state_text controller_bridge "${bridge}"
}

controller_ip_from_qga() {
  local controller_vmid="$1"
  local payload
  payload="$(qm guest cmd "${controller_vmid}" network-get-interfaces 2>/dev/null || true)"
  [[ -n "${payload}" ]] || return 0
  python3 -c '
import json
import sys

try:
    rows = json.loads(sys.stdin.read())
except Exception:
    raise SystemExit
for iface in rows if isinstance(rows, list) else []:
    name = str(iface.get("name") or "").lower()
    if name == "lo" or name.startswith(("docker", "br-", "veth")):
        continue
    for addr in iface.get("ip-addresses", []) or []:
        ip = addr.get("ip-address")
        if addr.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
            print(ip)
            raise SystemExit
' <<<"${payload}" || true
}

controller_mac() {
  local controller_vmid="$1"
  qm config "${controller_vmid}" \
    | awk -F'[:,=]' '/^net0:/ {for (i=1; i<=NF; i++) if ($i ~ /^[0-9A-Fa-f][0-9A-Fa-f]$/ && (i+5)<=NF) {print tolower($i ":" $(i+1) ":" $(i+2) ":" $(i+3) ":" $(i+4) ":" $(i+5)); exit}}'
}

controller_ip_from_arp() {
  local controller_vmid="$1"
  local mac
  mac="$(controller_mac "${controller_vmid}")"
  [[ -n "${mac}" ]] || return 0
  ip neigh show 2>/dev/null | awk -v mac="${mac}" 'tolower($5) == mac && $1 !~ /^fe80/ {print $1; exit}'
}

wait_for_ssh() {
  local ip="$1"
  local attempt
  local ssh_args=(-i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" -o ConnectTimeout=8)
  reset_controller_known_host "${ip}"
  for attempt in $(seq 1 120); do
    : "${attempt}"
    if ssh "${ssh_args[@]}" "${CONTROLLER_USER}@${ip}" "true" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

reset_controller_known_host() {
  local ip="$1"
  [[ -n "${ip}" ]] || return 0
  mkdir -p "$(dirname "${CONTROLLER_KNOWN_HOSTS}")"
  if ssh-keygen -f "${CONTROLLER_KNOWN_HOSTS}" -F "${ip}" >/dev/null 2>&1; then
    log "clearing recycled controller SSH host key for ${ip}"
    ssh-keygen -f "${CONTROLLER_KNOWN_HOSTS}" -R "${ip}" >/dev/null 2>&1 || true
  fi
}

wait_for_controller_ip() {
  local controller_vmid ip attempt
  controller_vmid="$(state_value controller_vmid)"
  [[ -n "${controller_vmid}" ]] || controller_vmid="$(controller_vm_id_by_name)"
  [[ -n "${controller_vmid}" ]] || die "controller VMID not found"

  if [[ -n "${CONTROLLER_IP}" ]]; then
    ip="${CONTROLLER_IP}"
    state_text controller_ip "${ip}"
    state_text controller_url "http://${ip}:5000"
    state_text base_url "http://${ip}:5000"
    wait_for_ssh "${ip}" || die "controller SSH did not become reachable at ${ip}"
    printf '%s' "${ip}"
    return 0
  fi

  log "waiting for controller IP via QGA/ARP"
  for attempt in $(seq 1 120); do
    : "${attempt}"
    ip="$(controller_ip_from_qga "${controller_vmid}")"
    [[ -z "${ip}" ]] && ip="$(controller_ip_from_arp "${controller_vmid}")"
    if [[ -n "${ip}" ]] && wait_for_ssh "${ip}"; then
      state_text controller_ip "${ip}"
      state_text controller_url "http://${ip}:5000"
      state_text base_url "http://${ip}:5000"
      printf '%s' "${ip}"
      return 0
    fi
    sleep 5
  done
  die "could not discover or SSH into controller VM ${controller_vmid}; pass --controller-ip for static bootstrap"
}

ssh_controller() {
  local ip="$1"
  shift
  local ssh_args=(-i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" -o ConnectTimeout=8)
  # shellcheck disable=SC2029
  ssh "${ssh_args[@]}" "${CONTROLLER_USER}@${ip}" "$@"
}

sync_source_to_controller() {
  local ip="$1"
  log "syncing source tree to Ubuntu controller"
  ssh_controller "${ip}" "sudo mkdir -p '${CONTROLLER_REMOTE_ROOT}' && sudo chown -R '${CONTROLLER_USER}:${CONTROLLER_USER}' '${CONTROLLER_REMOTE_ROOT}'"
  rsync -a --delete \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'bin/' \
    --exclude 'obj/' \
    --exclude 'autopilot-proxmox/.env' \
    --exclude 'autopilot-proxmox/inventory/group_vars/all/vars.yml' \
    --exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml' \
    --exclude 'autopilot-proxmox/secrets/' \
    --exclude 'autopilot-proxmox/cache/' \
    --exclude 'autopilot-proxmox/jobs/' \
    --exclude 'autopilot-proxmox/output/' \
    -e "ssh -i ${CONTROLLER_SSH_KEY} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${CONTROLLER_KNOWN_HOSTS}" \
    "${REPO_ROOT}/" "${CONTROLLER_USER}@${ip}:${CONTROLLER_REMOTE_ROOT}/"
  ssh_controller "${ip}" "find '${CONTROLLER_REMOTE_ROOT}/autopilot-agent' -type d \\( -name bin -o -name obj \\) -prune -exec rm -rf {} +"
  state_bool controller_source_synced true
}

copy_migration_bundle_to_controller() {
  local ip="$1"
  local bundle="$2"
  [[ -n "${bundle}" && -f "${bundle}" ]] || return 0
  log "copying migration bundle to Ubuntu controller"
  ssh_controller "${ip}" "mkdir -p '${CONTROLLER_REMOTE_ROOT}/migration'"
  scp -i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" \
    "${bundle}" "${CONTROLLER_USER}@${ip}:${CONTROLLER_REMOTE_ROOT}/migration/$(basename "${bundle}")"
  state_text controller_migration_bundle "${CONTROLLER_REMOTE_ROOT}/migration/$(basename "${bundle}")"
}

publish_setup_state_to_controller() {
  local ip target remote_tmp
  ip="${CONTROLLER_IP:-$(state_value controller_ip)}"
  [[ -n "${ip}" && -f "${STATE_FILE}" && -f "${CONTROLLER_SSH_KEY}" ]] || return 0
  target="${CONTROLLER_REMOTE_APP}/output/setup/foundation_state.json"
  remote_tmp="/tmp/autopilot-pve-foundation_state.json"
  scp -i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" \
    "${STATE_FILE}" "${CONTROLLER_USER}@${ip}:${remote_tmp}" >/dev/null
  ssh_controller "${ip}" "sudo sh -c 'mkdir -p \"$(dirname "${target}")\" && if [ -f \"${target}\" ]; then jq -s '\\''.[0] * .[1]'\\'' \"${target}\" \"${remote_tmp}\" > \"${target}.tmp\"; else cp \"${remote_tmp}\" \"${target}.tmp\"; fi && mv \"${target}.tmp\" \"${target}\" && rm -f \"${remote_tmp}\"'"
}

sync_controller_runtime_config() {
  local ip="$1"
  [[ -n "${ip}" && -f "${CONTROLLER_SSH_KEY}" ]] || return 0
  [[ -f "${VARS_FILE}" && -f "${VAULT_FILE}" ]] || return 0

  log "syncing repaired PVE API config to Ubuntu controller"
  local remote_tmp secret_name
  remote_tmp="/tmp/autopilot-runtime-config-$$"
  ssh_controller "${ip}" "rm -rf '${remote_tmp}' && mkdir -p '${remote_tmp}/inventory/group_vars/all' '${remote_tmp}/secrets'"
  scp -i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" \
    "${VARS_FILE}" "${CONTROLLER_USER}@${ip}:${remote_tmp}/inventory/group_vars/all/vars.yml" >/dev/null
  scp -i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" \
    "${VAULT_FILE}" "${CONTROLLER_USER}@${ip}:${remote_tmp}/inventory/group_vars/all/vault.yml" >/dev/null
  for secret_name in pve-root-ed25519 pve-root-ed25519.pub; do
    if [[ -f "${SECRETS_DIR}/${secret_name}" ]]; then
      scp -i "${CONTROLLER_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${CONTROLLER_KNOWN_HOSTS}" \
        "${SECRETS_DIR}/${secret_name}" "${CONTROLLER_USER}@${ip}:${remote_tmp}/secrets/${secret_name}" >/dev/null
    fi
  done
  ssh_controller "${ip}" "sudo sh -c 'mkdir -p \"${CONTROLLER_REMOTE_APP}/inventory/group_vars/all\" \"${CONTROLLER_REMOTE_APP}/secrets\" && cp -p \"${remote_tmp}/inventory/group_vars/all/vars.yml\" \"${CONTROLLER_REMOTE_APP}/inventory/group_vars/all/vars.yml\" && cp -p \"${remote_tmp}/inventory/group_vars/all/vault.yml\" \"${CONTROLLER_REMOTE_APP}/inventory/group_vars/all/vault.yml\" && rsync -a \"${remote_tmp}/secrets/\" \"${CONTROLLER_REMOTE_APP}/secrets/\" && chmod 600 \"${CONTROLLER_REMOTE_APP}/inventory/group_vars/all/vault.yml\" && if [ -f \"${CONTROLLER_REMOTE_APP}/secrets/pve-root-ed25519\" ]; then chmod 600 \"${CONTROLLER_REMOTE_APP}/secrets/pve-root-ed25519\"; fi && if [ -f \"${CONTROLLER_REMOTE_APP}/secrets/pve-root-ed25519.pub\" ]; then chmod 644 \"${CONTROLLER_REMOTE_APP}/secrets/pve-root-ed25519.pub\"; fi && chmod 700 \"${CONTROLLER_REMOTE_APP}/secrets\" && rm -rf \"${remote_tmp}\"'"
  state_bool controller_runtime_config_synced true
  state_text controller_runtime_config_synced_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}

controller_artifact_host_path() {
  local container_path="$1"
  case "${container_path}" in
    /app/cache/osdeploy/*)
      printf '%s/cache/osdeploy/%s' "${CONTROLLER_REMOTE_APP}" "${container_path#/app/cache/osdeploy/}"
      ;;
    /app/cache/cloudosd/*)
      printf '%s/cache/cloudosd/%s' "${CONTROLLER_REMOTE_APP}" "${container_path#/app/cache/cloudosd/}"
      ;;
    /app/output/*)
      printf '%s/output/%s' "${CONTROLLER_REMOTE_APP}" "${container_path#/app/output/}"
      ;;
    "${CONTROLLER_REMOTE_APP}"/*)
      printf '%s' "${container_path}"
      ;;
    *)
      return 1
      ;;
  esac
}

promote_controller_setup_artifacts() {
  local ip="$1"
  local node iso_storage iso_dir registry_path registry_json registry_file pending_file copied
  [[ -n "${ip}" && -f "${CONTROLLER_SSH_KEY}" ]] || return 0
  node="$(detect_node)"
  iso_storage="$(detect_iso_storage)"
  [[ -n "${iso_storage}" ]] || return 0
  iso_dir="$(iso_dir_for_storage "${iso_storage}")"
  mkdir -p "${iso_dir}"

  registry_path="${CONTROLLER_REMOTE_APP}/cache/osdeploy/setup-artifacts/artifact_registry.json"
  registry_json="$(ssh_controller "${ip}" "if [ -f '${registry_path}' ]; then cat '${registry_path}'; else printf '{\"schema_version\":1,\"artifacts\":[]}'; fi")"
  registry_file="$(mktemp)"
  pending_file="$(mktemp)"
  printf '%s' "${registry_json}" >"${registry_file}"
  python3 - "${registry_file}" >"${pending_file}" <<'PY'
import json
import sys
from pathlib import Path

registry = Path(sys.argv[1])
try:
    data = json.loads(registry.read_text(encoding="utf-8"))
except Exception:
    data = {}
for row in data.get("artifacts") or []:
    if row.get("proxmox_volid"):
        continue
    if row.get("kind") not in {"winpe-iso", "cloudosd-iso", "osdeploy-iso"}:
        continue
    artifact_id = str(row.get("artifact_id") or "").strip()
    path = str(row.get("path") or "").strip()
    if not artifact_id or not path:
        continue
    print("\t".join([artifact_id, str(row.get("kind") or ""), path, Path(path).name]))
PY

  if [[ ! -s "${pending_file}" ]]; then
    rm -f "${registry_file}" "${pending_file}"
    return 0
  fi

  copied=0
  while IFS=$'\t' read -r artifact_id kind container_path filename; do
    [[ -n "${artifact_id}" && -n "${filename}" ]] || continue
    local source_path payload
    if ! source_path="$(controller_artifact_host_path "${container_path}")"; then
      log "skipping ${kind} ${artifact_id}; unsupported controller artifact path ${container_path}"
      continue
    fi
    log "pulling ${kind} ${filename} from controller into PVE ISO storage ${iso_storage}"
    rsync -a --partial --inplace \
      -e "ssh -i ${CONTROLLER_SSH_KEY} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${CONTROLLER_KNOWN_HOSTS}" \
      "${CONTROLLER_USER}@${ip}:${source_path}" "${iso_dir}/${filename}"
    chmod 0644 "${iso_dir}/${filename}" || true
    payload="$(python3 - "${artifact_id}" "${node}" "${iso_storage}" <<'PY'
import json
import sys

print(json.dumps({
    "artifact_ids": [sys.argv[1]],
    "node": sys.argv[2],
    "storage": sys.argv[3],
    "already_copied": True,
}))
PY
)"
    curl -fsS -X POST "http://${ip}:5000/api/setup/v1/artifacts/promote" \
      -H 'Content-Type: application/json' \
      --data "${payload}" >/dev/null
    copied=$((copied + 1))
  done <"${pending_file}"

  rm -f "${registry_file}" "${pending_file}"
  if [[ "${copied}" -gt 0 ]]; then
    state_bool promoted_artifacts_ready true
    state_text promoted_artifacts_last_pull_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  fi
}

collect_controller_bootstrap_debug() {
  local ip="$1"
  log "collecting controller bootstrap diagnostics"
  ssh_controller "${ip}" "cd '${CONTROLLER_REMOTE_APP}' 2>/dev/null && sudo docker compose ps -a || true; sudo docker logs autopilot-postgres --tail 80 || true" || true
}

run_controller_init() {
  local ip="$1"
  local pve_ip="$2"
  local pve_node pve_iso_storage pve_disk_storage pve_bridge
  pve_node="$(state_value pve_node)"
  pve_iso_storage="$(state_value pve_iso_storage)"
  pve_disk_storage="$(state_value pve_disk_storage)"
  pve_bridge="$(state_value pve_bridge)"
  local remote_bundle
  remote_bundle="$(state_value controller_migration_bundle)"
  log "running Ubuntu controller bootstrap"
  local cmd
  cmd="sudo bash '${CONTROLLER_REMOTE_APP}/scripts/init-controller-ubuntu.sh' --repo-dir '${CONTROLLER_REMOTE_APP}' --pve-host '${pve_ip}' --pve-node '${pve_node}' --pve-iso-storage '${pve_iso_storage}' --pve-disk-storage '${pve_disk_storage}' --pve-bridge '${pve_bridge}' --controller-ip '${ip}' --non-interactive"
  if [[ -n "${remote_bundle}" ]]; then
    cmd="${cmd} --restore-bundle '${remote_bundle}'"
  fi
  local rc
  set +e
  ssh_controller "${ip}" "${cmd}"
  rc=$?
  set -e
  if [[ "${rc}" == "0" ]]; then
    return 0
  fi

  log "Ubuntu controller bootstrap exited with rc=${rc}; retrying once to repair partial first-run state"
  collect_controller_bootstrap_debug "${ip}"
  sleep 5
  set +e
  ssh_controller "${ip}" "${cmd}"
  rc=$?
  set -e
  if [[ "${rc}" != "0" ]]; then
    collect_controller_bootstrap_debug "${ip}"
    return "${rc}"
  fi
}

verify_controller_health() {
  local ip="$1"
  log "verifying controller web health"
  for attempt in $(seq 1 90); do
    : "${attempt}"
    if curl -fsS "http://${ip}:5000/healthz" >/dev/null; then
      curl -fsS "http://${ip}:5000/api/version" >/dev/null || true
      state_bool controller_runtime_ready true
      state_bool console_health_ready true
      state_text controller_url "http://${ip}:5000"
      state_text base_url "http://${ip}:5000"
      return 0
    fi
    sleep 2
  done
  die "controller web UI did not become healthy at http://${ip}:5000"
}

stop_pve_runtime_stack() {
  log "stopping accidental PVE-host runtime stack after controller health"
  if ! command -v docker >/dev/null 2>&1; then
    state_bool pve_runtime_absent true
    state_bool pve_runtime_stopped true
    return 0
  fi
  local containers
  containers="$(docker ps -a --format '{{.Names}}' | awk '/^autopilot/ {print}' || true)"
  if [[ -z "${containers}" ]]; then
    state_bool pve_runtime_stopped true
    return 0
  fi
  # Non-destructive: disable restart and stop containers, but keep images,
  # volumes, and the migration bundle for rollback.
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    docker update --restart=no "${name}" >/dev/null 2>&1 || true
  done <<<"${containers}"
  if [[ -f "${APP_DIR}/docker-compose.yml" ]]; then
    (cd "${APP_DIR}" && docker compose stop >/dev/null 2>&1) || \
      (cd "${APP_DIR}" && docker-compose stop >/dev/null 2>&1) || true
  fi
  while IFS= read -r name; do
    [[ -n "${name}" ]] || continue
    docker stop "${name}" >/dev/null 2>&1 || true
  done <<<"${containers}"
  state_bool pve_runtime_stopped true
}

is_dev_lab_vm_name() {
  local name="$1"
  case "${name}" in
    "${CONTROLLER_NAME}"|"${BUILDHOST_NAME}"|autopilot-osdeploy-blank-template|autopilot-cloudosd-blank-template) return 0 ;;
    OSDEPLOY-E2E-*|CLOUDOSD-E2E-*|AUTOPILOT-E2E-*|OSD[0-9]*|CSD[0-9]*|APE2E[0-9]*) return 0 ;;
    *) return 1 ;;
  esac
}

destroy_dev_lab_vm() {
  local vmid="$1"
  local name="$2"
  log "destroying dev lab VM ${vmid} (${name})"
  qm unlock "${vmid}" >/dev/null 2>&1 || true
  if qm status "${vmid}" 2>/dev/null | grep -q 'status: running'; then
    qm stop "${vmid}" --skiplock 1 >/dev/null 2>&1 || qm stop "${vmid}" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      qm status "${vmid}" 2>/dev/null | grep -q 'status: running' || break
      sleep 1
    done
  fi
  qm destroy "${vmid}" --purge 1 --destroy-unreferenced-disks 1 >/dev/null 2>&1 \
    || qm destroy "${vmid}" --purge 1 >/dev/null 2>&1 \
    || qm destroy "${vmid}" >/dev/null
}

reset_dev_lab_vms() {
  local vmid name
  while read -r vmid name; do
    [[ -n "${vmid}" && -n "${name}" ]] || continue
    if is_dev_lab_vm_name "${name}"; then
      destroy_dev_lab_vm "${vmid}" "${name}"
    fi
  done < <(qm list | awk 'NR > 1 {print $1, $2}')
}

reset_dev_lab_media() {
  [[ "${RESET_MEDIA}" == "1" ]] || return 0
  local storage dir removed=0
  while read -r storage; do
    [[ -n "${storage}" ]] || continue
    dir="$(iso_dir_for_storage "${storage}")"
    [[ -d "${dir}" ]] || continue
    log "removing generated/downloaded dev lab media from ${dir}"
    while IFS= read -r path; do
      [[ -n "${path}" ]] || continue
      log "removing media $(basename "${path}")"
      rm -f "${path}"
      removed=1
    done < <(
      find "${dir}" -maxdepth 1 -type f \( \
        -iname 'virtio-win*.iso' -o \
        -iname 'win11*.iso' -o \
        -iname 'windows*.iso' -o \
        -iname '*windows*enterprise*eval*.iso' -o \
        -iname '*cliententerpriseeval*.iso' -o \
        -iname 'autopilot-buildhost-seed-*.iso' -o \
        -iname 'winpe-autopilot-*.iso' -o \
        -iname 'cloudosd-autopilot-*.iso' -o \
        -iname 'osdeploy-server-*.iso' \
      \) -print
    )
  done < <(list_iso_storages)
  if [[ "${removed}" == "0" ]]; then
    log "no generated/downloaded dev lab media matched reset patterns"
  fi
}

reset_dev_lab_runtime_state() {
  log "removing generated dev lab state and runtime secrets"
  rm -rf "${SETUP_DIR}" "${SECRETS_DIR}" "${PVE_RUNTIME_DIR}"
  rm -f "${ENV_FILE}" "${VARS_FILE}" "${VAULT_FILE}"
  mkdir -p "${SETUP_DIR}" "$(dirname "${VARS_FILE}")"
  state_text phase "reset-dev-lab"
  state_bool dev_lab_reset_ready true
  state_bool pve_host_clean_ready true
}

resolve_windows_iso_from_microsoft() {
  python3 - "${WINDOWS_ISO_EDITION_ID}" "${WINDOWS_ISO_LANGUAGE}" "${WINDOWS_ISO_LOCALE}" <<'PY'
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

edition_id = sys.argv[1]
requested_language = sys.argv[2].strip()
locale = sys.argv[3].strip().lower() or "en-us"
session_id = str(uuid.uuid4())
org_id = "y6jn8c31"
profile_id = "606624d44113"
instance_id = "560dc9f3-1aa5-4a2f-b63c-9e18f8d0e175"
base = "https://www.microsoft.com/software-download-connector/api/"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0",
    "Accept": "application/json,text/javascript,*/*;q=0.01",
    "Referer": "https://www.microsoft.com/software-download/windows11",
}


def fetch_text(url, extra_headers=None):
    merged = dict(headers)
    if extra_headers:
        merged.update(extra_headers)
    req = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def fetch_json(action, params, extra_headers=None):
    url = base + action + "?" + urllib.parse.urlencode(params)
    return json.loads(fetch_text(url, extra_headers=extra_headers))


def microsoft_download_params(**updates):
    params = {
        "profile": profile_id,
        "productEditionId": "undefined",
        "SKU": "undefined",
        "friendlyFileName": "undefined",
        "Locale": locale,
        "sessionID": session_id,
    }
    params.update(updates)
    return params


def language_matches(sku):
    wanted = requested_language.lower()
    language = str(sku.get("Language", "")).lower()
    localized = str(sku.get("LocalizedLanguage", "")).lower()
    aliases = {
        "en": "english",
        "en-us": "english",
        "english (united states)": "english",
        "us english": "english",
    }
    wanted = aliases.get(wanted, wanted)
    return wanted == language or wanted == localized or wanted in language or wanted in localized


try:
    # Microsoft's download-link endpoint rejects unregistered sessions. The
    # public page performs this same registration before requesting the ISO URL.
    fetch_text("https://www.microsoft.com/en-us/software-download/windows11")
    fetch_text(f"https://vlscppe.microsoft.com/fp/tags.js?org_id={org_id}&session_id={session_id}")
    fetch_text(f"https://vlscppe.microsoft.com/tags?org_id={org_id}&session_id={session_id}")
    mdt = fetch_text(
        "https://ov-df.microsoft.com/mdt.js?"
        + urllib.parse.urlencode(
            {"instanceId": instance_id, "PageId": "si", "session_id": session_id}
        )
    )
    w_match = re.search(r"[?&]w=([A-Fa-f0-9]+)", mdt)
    rticks_match = re.search(r"rticks[^0-9]{0,80}(\d{10,})", mdt)
    if not w_match or not rticks_match:
        raise RuntimeError("could not parse Microsoft session challenge response")
    fetch_text(
        "https://ov-df.microsoft.com/?"
        + urllib.parse.urlencode(
            {
                "session_id": session_id,
                "CustomerId": instance_id,
                "PageId": "si",
                "w": w_match.group(1),
                "mdt": str(int(time.time() * 1000)),
                "rticks": rticks_match.group(1),
            }
        )
    )

    sku_response = fetch_json(
        "getskuinformationbyproductedition",
        microsoft_download_params(productEditionId=edition_id),
    )
    if sku_response.get("Errors"):
        raise RuntimeError(sku_response["Errors"][0].get("Value", "Microsoft SKU lookup failed"))
    skus = sku_response.get("Skus") or []
    selected = next((sku for sku in skus if language_matches(sku)), None)
    if selected is None:
        available = ", ".join(sorted({str(sku.get("Language", "")) for sku in skus if sku.get("Language")}))
        raise RuntimeError(f"language {requested_language!r} is unavailable; available: {available}")

    link_response = fetch_json(
        "GetProductDownloadLinksBySku",
        microsoft_download_params(SKU=str(selected["Id"])),
        extra_headers={"Referer": "https://www.microsoft.com/software-download/windows11"},
    )
    if link_response.get("Errors"):
        error = link_response["Errors"][0]
        key = error.get("Key", "Microsoft download-link lookup failed")
        value = error.get("Value", "")
        if error.get("Type") == 9:
            value = (
                "Microsoft rejected the download session. This usually means the "
                "source IP is temporarily rate-limited or blocked for ISO generation."
            )
        raise RuntimeError(f"{key}: {value}")
    options = link_response.get("ProductDownloadOptions") or []
    selected_option = next((option for option in options if option.get("DownloadType") == 1), None)
    selected_option = selected_option or (options[0] if options else None)
    if not selected_option or not selected_option.get("Uri"):
        raise RuntimeError("Microsoft did not return a Windows ISO download URL")

    friendly_names = selected.get("FriendlyFileNames") or []
    print(
        json.dumps(
            {
                "url": selected_option["Uri"],
                "filename": friendly_names[0] if friendly_names else "windows-11-microsoft.iso",
                "language": selected.get("Language") or requested_language,
                "localized_language": selected.get("LocalizedLanguage") or "",
                "product": selected_option.get("ProductDisplayName") or selected.get("ProductDisplayName") or "",
                "sku": str(selected["Id"]),
                "expires_at": link_response.get("DownloadExpirationDatetime") or "",
                "session_id": session_id,
            }
        )
    )
except Exception as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

resolve_windows_eval_iso_from_microsoft() {
  python3 - "${WINDOWS_ISO_LANGUAGE}" "${WINDOWS_ISO_LOCALE}" <<'PY'
import json
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

requested_language = sys.argv[1].strip() or "English"
locale = sys.argv[2].strip().lower() or "en-us"
eval_page = "https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.microsoft.com/en-us/evalcenter/evaluate-windows-11-enterprise",
}


class EvalTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.in_tr = False
        self.in_th = False
        self.in_a = False
        self.language_parts = []
        self.current_href = ""
        self.current_text_parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.in_tr = True
            self.language_parts = []
            self.links = []
        elif self.in_tr and tag == "th":
            self.in_th = True
        elif self.in_tr and tag == "a":
            self.in_a = True
            self.current_href = attrs.get("href", "")
            self.current_text_parts = []

    def handle_data(self, data):
        if self.in_tr and self.in_th:
            self.language_parts.append(data)
        if self.in_tr and self.in_a:
            self.current_text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "th":
            self.in_th = False
        elif tag == "a" and self.in_a:
            text = " ".join("".join(self.current_text_parts).split())
            if self.current_href:
                self.links.append({"text": text, "href": self.current_href})
            self.in_a = False
            self.current_href = ""
            self.current_text_parts = []
        elif tag == "tr" and self.in_tr:
            language = " ".join("".join(self.language_parts).split())
            if language and self.links:
                self.rows.append({"language": language, "links": list(self.links)})
            self.in_tr = False


def fetch_text(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


def language_matches(language):
    wanted = requested_language.lower()
    candidate = language.lower()
    aliases = {
        "en": "english",
        "en-us": "english",
        "english": "english (united states)",
        "us english": "english (united states)",
        "english (united states)": "english (united states)",
    }
    wanted = aliases.get(wanted, wanted)
    return wanted == candidate or wanted in candidate or candidate in wanted


def absolute_url(href):
    return urllib.parse.urljoin(eval_page, href)


def resolve_redirect(url):
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.url, response.headers.get("content-length", "")


try:
    parser = EvalTableParser()
    parser.feed(fetch_text(eval_page))
    selected = next((row for row in parser.rows if language_matches(row["language"])), None)
    if selected is None:
        available = ", ".join(row["language"] for row in parser.rows)
        raise RuntimeError(f"language {requested_language!r} is unavailable in Microsoft Evaluation Center; available: {available}")

    enterprise_links = [
        link for link in selected["links"]
        if "64-bit" in link["text"].lower() and "linkid=" in link["href"].lower()
    ]
    if not enterprise_links:
        raise RuntimeError(f"Microsoft Evaluation Center did not publish an ISO link for {selected['language']}")

    fwlink = absolute_url(enterprise_links[0]["href"])
    final_url, content_length = resolve_redirect(fwlink)
    parsed = urllib.parse.urlparse(final_url)
    filename = parsed.path.rsplit("/", 1)[-1] or "Win11_Enterprise_Evaluation_x64.iso"
    if not filename.lower().endswith(".iso"):
        filename = "Win11_Enterprise_Evaluation_x64.iso"
    print(json.dumps({
        "url": final_url,
        "filename": filename,
        "language": selected["language"],
        "localized_language": selected["language"],
        "product": "Windows 11 Enterprise Evaluation",
        "sku": "evaluation-center",
        "source": "microsoft-evaluation-center",
        "source_page": eval_page,
        "fwlink": fwlink,
        "content_length": content_length,
        "expires_at": "",
    }))
except Exception as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

download_windows_iso_if_requested() {
  [[ -n "${WINDOWS_ISO_URL}" || "${DOWNLOAD_WINDOWS}" == "1" ]] || return 0
  local iso_storage dir filename source_url resolved_json resolver_error eval_error download_source existing_iso
  iso_storage="$(detect_iso_storage)"
  dir="$(iso_dir_for_storage "${iso_storage}")"
  mkdir -p "${dir}"
  if [[ -z "${WINDOWS_ISO_URL}" ]]; then
    existing_iso="$(select_windows_iso "${dir}")"
    if [[ -n "${existing_iso}" ]]; then
      rm -f "${existing_iso}.part"
      log "Windows ISO already present at ${existing_iso}"
      state_bool windows_iso_ready true
      state_text windows_iso_volid "${iso_storage}:iso/$(basename "${existing_iso}")"
      return 0
    fi
  fi
  if [[ -n "${WINDOWS_ISO_URL}" ]]; then
    source_url="${WINDOWS_ISO_URL}"
    filename="$(basename "${source_url%%\?*}")"
    download_source="operator-supplied-official-url"
  else
    log "resolving Windows ISO from official Microsoft software download API"
    state_bool windows_iso_download_attempted true
    resolver_error="${SETUP_DIR}/windows_iso_resolver.err"
    if ! resolved_json="$(resolve_windows_iso_from_microsoft 2>"${resolver_error}")"; then
      log "Windows ISO software-download resolver failed; trying Microsoft Evaluation Center"
      eval_error="${SETUP_DIR}/windows_iso_eval_resolver.err"
      if ! resolved_json="$(resolve_windows_eval_iso_from_microsoft 2>"${eval_error}")"; then
        state_text windows_iso_download_error "$(cat "${resolver_error}" "${eval_error}" | tail -n 8 | tr '\n' ' ')"
        log "Windows ISO automatic resolver failed; media gate will remain blocked"
        return 0
      fi
    fi
    source_url="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["url"])' "${resolved_json}")"
    filename="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["filename"])' "${resolved_json}")"
    download_source="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("source", "microsoft-software-download-connector"))' "${resolved_json}")"
    state_text windows_iso_download_source "${download_source}"
    state_text windows_iso_download_error ""
    state_text windows_iso_download_language "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("language", ""))' "${resolved_json}")"
    state_text windows_iso_download_product "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("product", ""))' "${resolved_json}")"
    state_text windows_iso_download_sku "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("sku", ""))' "${resolved_json}")"
    state_text windows_iso_download_expires_at "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("expires_at", ""))' "${resolved_json}")"
  fi
  case "${source_url}" in
    https://microsoft.com/*|https://*.microsoft.com/*) ;;
    *) die "--windows-iso-url must be an operator-supplied official Microsoft direct download URL" ;;
  esac
  [[ "${filename}" == *.iso ]] || filename="windows-11-operator-supplied.iso"
  if [[ -s "${dir}/${filename}" ]]; then
    rm -f "${dir}/${filename}.part"
    log "Windows ISO already present at ${dir}/${filename}"
    state_bool windows_iso_ready true
    state_text windows_iso_volid "${iso_storage}:iso/${filename}"
    return 0
  fi
  log "downloading Windows ISO to ${dir}/${filename}"
  curl -fL -C - --retry 3 --retry-all-errors -o "${dir}/${filename}.part" "${source_url}"
  mv -f "${dir}/${filename}.part" "${dir}/${filename}"
  state_bool windows_iso_ready true
  state_text windows_iso_volid "${iso_storage}:iso/${filename}"
}

download_virtio_if_requested() {
  [[ "${DOWNLOAD_VIRTIO}" == "1" ]] || return 0
  local iso_storage dir target
  iso_storage="$(detect_iso_storage)"
  dir="$(iso_dir_for_storage "${iso_storage}")"
  mkdir -p "${dir}"
  if find "${dir}" -maxdepth 1 -type f -iname 'virtio-win*.iso' | grep -q .; then
    return 0
  fi
  target="${dir}/virtio-win.iso"
  log "downloading VirtIO ISO from official virtio-win source"
  curl -fL --retry 3 -o "${target}.part" \
    "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
  mv -f "${target}.part" "${target}"
}

select_windows_iso() {
  local dir="$1"
  find "${dir}" -maxdepth 1 -type f -iname '*.iso' \
    ! -iname 'virtio-win*.iso' ! -iname '*winpe*' ! -iname '*cloudosd*' \
    | awk '
      {
        name=tolower($0)
        if (name ~ /enterpriseeval|cliententerprise|client.*eval|evaluation|eval/) {
          print
          found=1
          exit
        }
        if (candidate == "" && name ~ /win(dows)?|windows_11|win11/) {
          candidate=$0
        }
      }
      END {
        if (!found && candidate != "") {
          print candidate
        }
      }'
}

scan_media() {
  local iso_storage dir windows_iso virtio_iso
  iso_storage="$(detect_iso_storage)"
  dir="$(iso_dir_for_storage "${iso_storage}")"
  mkdir -p "${dir}"
  windows_iso="$(select_windows_iso "${dir}")"
  virtio_iso="$(find "${dir}" -maxdepth 1 -type f -iname 'virtio-win*.iso' | sort | tail -n 1)"

  if [[ -n "${windows_iso}" ]]; then
    state_bool windows_iso_ready true
    state_text windows_iso_volid "${iso_storage}:iso/$(basename "${windows_iso}")"
  else
    state_bool windows_iso_ready false
  fi
  if [[ -n "${virtio_iso}" ]]; then
    state_bool virtio_iso_ready true
    state_text virtio_iso_volid "${iso_storage}:iso/$(basename "${virtio_iso}")"
  else
    state_bool virtio_iso_ready false
  fi
  if [[ -n "${windows_iso}" && -n "${virtio_iso}" ]]; then
    state_bool media_ready true
  else
    state_bool media_ready false
  fi
  update_media_vars_yml \
    "${windows_iso:+${iso_storage}:iso/$(basename "${windows_iso}")}" \
    "${virtio_iso:+${iso_storage}:iso/$(basename "${virtio_iso}")}"
}

update_media_vars_yml() {
  local windows_volid="$1"
  local virtio_volid="$2"
  PVE_INIT_WINDOWS_ISO="${windows_volid}" \
  PVE_INIT_VIRTIO_ISO="${virtio_volid}" \
  python3 - "${VARS_FILE}" <<'PY'
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "proxmox_windows_iso": os.environ.get("PVE_INIT_WINDOWS_ISO", "").strip() or None,
    "proxmox_virtio_iso": os.environ.get("PVE_INIT_VIRTIO_ISO", "").strip() or None,
}

def yaml_value(value):
    if value is None:
        return "null"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["---"]
seen = set()
out = []
for line in lines:
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
    if match and match.group(1) in updates:
        key = match.group(1)
        out.append(f"{key}: {yaml_value(updates[key])}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}: {yaml_value(value)}")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

wait_for_media_if_requested() {
  while true; do
    download_windows_iso_if_requested
    download_virtio_if_requested
    scan_media
    local ready
    ready="$(python3 - "${STATE_FILE}" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print("yes" if data.get("windows_iso_ready") and data.get("virtio_iso_ready") else "no")
PY
)"
    [[ "${ready}" == "yes" ]] && return 0
    if [[ "${WAIT_FOR_MEDIA}" != "1" ]]; then
      log "media gate blocked: upload a Windows ISO to $(iso_dir_for_storage "$(detect_iso_storage)") or rerun with --download-windows or --windows-iso-url"
      return 20
    fi
    log "waiting for Windows/VirtIO ISO media; rescan in 30s"
    sleep 30
  done
}

observe_existing_buildhost_vm() {
  local existing iso_storage dir seed_iso
  existing="$(qm list | awk -v name="${BUILDHOST_NAME}" 'NR > 1 && $2 == name {print $1; exit}')"
  [[ -n "${existing}" ]] || return 0
  state_bool build_host_vm_ready true
  state_text build_host_vmid "${existing}"
  state_text build_host_name "${BUILDHOST_NAME}"
  state_text build_host_node "$(detect_node)"
  state_text build_host_creation_owner "controller"

  iso_storage="$(detect_iso_storage)"
  dir="$(iso_dir_for_storage "${iso_storage}")"
  seed_iso="${dir}/autopilot-buildhost-seed-${existing}.iso"
  if [[ -s "${seed_iso}" ]]; then
    state_bool seed_iso_ready true
    state_bool build_host_unattend_ready true
    state_bool build_host_agent_auto_approve true
    state_text seed_iso_volid "${iso_storage}:iso/$(basename "${seed_iso}")"
    state_text build_host_expected_agent_id "buildhost-${existing}"
    state_text build_host_expected_computer_name "AUTOPILOT-BLD"
    state_text build_host_admin_user "autopilotbuilder"
  fi
}

phase_foundation() {
  require_root
  require_command python3
  require_command pveum
  require_command pvesm
  require_command pvesh
  require_command qm
  mkdir -p "${SETUP_DIR}"
  state_text phase "foundation"
  repair_clock
  repair_apt_sources
  install_host_essentials
  repair_pve_access_contract
  scan_media
  local bundle controller_ip pve_ip
  bundle="$(create_migration_bundle)"
  create_controller_vm
  controller_ip="$(wait_for_controller_ip)"
  pve_ip="$(state_value pve_host_ip)"
  ensure_runtime_secrets
  sync_source_to_controller "${controller_ip}"
  sync_controller_runtime_config "${controller_ip}"
  copy_migration_bundle_to_controller "${controller_ip}" "${bundle}"
  run_controller_init "${controller_ip}" "${pve_ip}"
  verify_controller_health "${controller_ip}"
  stop_pve_runtime_stack
  publish_setup_state_to_controller
}

phase_bootstrap() {
  require_root
  require_command qm
  require_command genisoimage
  require_command pveum
  require_command pvesm
  require_command pvesh
  state_text phase "bootstrap"
  state_text build_host_creation_owner "controller"
  repair_pve_access_contract
  download_windows_iso_if_requested
  download_virtio_if_requested
  scan_media
  if wait_for_media_if_requested; then
    :
  else
    local rc=$?
    publish_setup_state_to_controller || true
    return "${rc}"
  fi
  state_bool bootstrap_media_ready true
  observe_existing_buildhost_vm
  local controller_ip
  controller_ip="${CONTROLLER_IP:-$(state_value controller_ip)}"
  if [[ -n "${controller_ip}" ]]; then
    sync_controller_runtime_config "${controller_ip}"
  fi
  publish_setup_state_to_controller
}

phase_operational() {
  require_root
  require_command pveum
  require_command pvesm
  require_command pvesh
  require_command qm
  state_text phase "operational"
  local controller_ip
  controller_ip="${CONTROLLER_IP:-$(state_value controller_ip)}"
  repair_pve_access_contract
  if [[ -n "${controller_ip}" ]]; then
    verify_controller_health "${controller_ip}"
  fi
  scan_media
  observe_existing_buildhost_vm
  if [[ -n "${controller_ip}" ]]; then
    sync_controller_runtime_config "${controller_ip}"
    promote_controller_setup_artifacts "${controller_ip}"
  fi
  publish_setup_state_to_controller
}

phase_runtime_config() {
  require_root
  require_command pveum
  require_command pvesm
  require_command pvesh
  require_command qm
  state_text phase "runtime-config"
  repair_pve_access_contract
  scan_media
  observe_existing_buildhost_vm
  local controller_ip
  controller_ip="${CONTROLLER_IP:-$(state_value controller_ip)}"
  if [[ -n "${controller_ip}" ]]; then
    sync_controller_runtime_config "${controller_ip}"
  fi
  publish_setup_state_to_controller
}

phase_reset_dev_lab() {
  require_root
  require_command qm
  require_command pvesm
  mkdir -p "${SETUP_DIR}"
  log "resetting disposable dev lab state"
  stop_pve_runtime_stack
  reset_dev_lab_vms
  reset_dev_lab_media
  reset_dev_lab_runtime_state
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    --resume) RESUME=1; shift ;;
    --wait-for-media) WAIT_FOR_MEDIA=1; shift ;;
    --download-windows) DOWNLOAD_WINDOWS=1; shift ;;
    --windows-iso-language) WINDOWS_ISO_LANGUAGE="${2:-}"; shift 2 ;;
    --windows-iso-url) WINDOWS_ISO_URL="${2:-}"; shift 2 ;;
    --download-virtio) DOWNLOAD_VIRTIO=1; shift ;;
    --node) NODE="${2:-}"; shift 2 ;;
    --iso-storage) ISO_STORAGE="${2:-}"; shift 2 ;;
    --controller-ip) CONTROLLER_IP="${2:-}"; shift 2 ;;
    --controller-cidr) CONTROLLER_CIDR="${2:-}"; shift 2 ;;
    --controller-gateway) CONTROLLER_GATEWAY="${2:-}"; shift 2 ;;
    --controller-dns) CONTROLLER_DNS="${2:-}"; shift 2 ;;
    --controller-vmid) CONTROLLER_VMID="${2:-}"; shift 2 ;;
    --controller-storage) CONTROLLER_STORAGE="${2:-}"; shift 2 ;;
    --controller-bridge) CONTROLLER_BRIDGE="${2:-}"; shift 2 ;;
    --reset-media) RESET_MEDIA=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ "${RESUME}" == "1" ]]; then
  log "resume mode enabled"
fi
if [[ "${NON_INTERACTIVE}" == "1" ]]; then
  log "non-interactive mode enabled"
fi

case "${PHASE}" in
  foundation) phase_foundation ;;
  bootstrap) phase_bootstrap ;;
  operational) phase_operational ;;
  runtime-config) phase_runtime_config ;;
  reset-dev-lab) phase_reset_dev_lab ;;
  all)
    phase_foundation
    phase_bootstrap
    phase_operational
    ;;
  *) die "invalid --phase ${PHASE}" ;;
esac

log "state: ${STATE_FILE}"
