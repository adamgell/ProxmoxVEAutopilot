#!/usr/bin/env bash
# Ubuntu Server controller bootstrap for ProxmoxVEAutopilot.
#
# Runs inside the dedicated controller VM. This script owns Docker, Compose,
# Postgres readiness, source builds, the seed agent build container, and
# controller-local first-run state.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${APP_DIR}/.." && pwd)"
SETUP_DIR="${APP_DIR}/output/setup"
STATE_FILE="${SETUP_DIR}/foundation_state.json"
ENV_FILE="${APP_DIR}/.env"
VARS_FILE="${APP_DIR}/inventory/group_vars/all/vars.yml"
SECRETS_DIR="${APP_DIR}/secrets"
IMAGE_TAG="${IMAGE_TAG:-ghcr.io/adamgell/proxmox-autopilot:latest}"

PVE_HOST=""
PVE_NODE=""
PVE_ISO_STORAGE=""
PVE_DISK_STORAGE=""
PVE_BRIDGE=""
CONTROLLER_IP=""
RESTORE_BUNDLE=""
SKIP_RESTORE=0
NON_INTERACTIVE=0

usage() {
  cat <<'USAGE'
Usage:
  init-controller-ubuntu.sh [options]

Options:
  --repo-dir <path>           Repo path inside the controller VM.
  --pve-host <ip>             Proxmox VE API host/IP.
  --pve-node <node>           Proxmox VE node name.
  --pve-iso-storage <storage> Proxmox ISO-capable storage.
  --pve-disk-storage <storage> Proxmox VM disk storage.
  --pve-bridge <bridge>       Proxmox VM network bridge.
  --controller-ip <ip>        Controller LAN IP used by agents.
  --restore-bundle <path>     Migration bundle copied from the PVE host.
  --skip-restore              Do not restore a migration bundle.
  --non-interactive
  --help
USAGE
}

log() {
  printf '[controller-init] %s\n' "$*" >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_root() {
  [[ "$(id -u)" == "0" ]] || die "run this script as root or with sudo inside the Ubuntu controller"
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

host_ip() {
  ip -4 -o addr show scope global \
    | awk '!($4 ~ /^127\./) {split($4,a,"/"); print a[1]; exit}'
}

wait_for_apt_ready() {
  log "waiting for cloud-init and apt package locks"
  if command -v cloud-init >/dev/null 2>&1; then
    cloud-init status --wait >/dev/null 2>&1 || true
  fi

  local attempt
  for attempt in $(seq 1 120); do
    : "${attempt}"
    if pgrep -x "apt|apt-get|dpkg|unattended-upgrade|unattended-upgrades" >/dev/null 2>&1; then
      sleep 5
      continue
    fi
    if command -v fuser >/dev/null 2>&1 && \
      fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/cache/apt/archives/lock >/dev/null 2>&1; then
      sleep 5
      continue
    fi
    state_bool controller_apt_ready true
    return 0
  done
  die "apt package locks did not clear"
}

install_runtime_prereqs() {
  log "installing Ubuntu controller runtime prerequisites"
  wait_for_apt_ready
  apt-get update
  local packages=(
    ca-certificates
    curl
    git
    jq
    openssl
    python3
    python3-venv
    rsync
    tar
    docker.io
  )
  if apt-cache show docker-compose-plugin >/dev/null 2>&1; then
    packages+=(docker-compose-plugin)
  elif apt-cache show docker-compose-v2 >/dev/null 2>&1; then
    packages+=(docker-compose-v2)
  elif apt-cache show docker-compose >/dev/null 2>&1; then
    packages+=(docker-compose)
  fi
  if apt-cache show docker-buildx >/dev/null 2>&1; then
    packages+=(docker-buildx)
  elif apt-cache show docker-buildx-plugin >/dev/null 2>&1; then
    packages+=(docker-buildx-plugin)
  fi
  apt-get install -y "${packages[@]}"
  systemctl enable --now docker >/dev/null
  docker version >/dev/null
  if docker compose version >/dev/null 2>&1; then
    docker compose version
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose version
  else
    die "Docker Compose was not installed"
  fi
  state_bool controller_docker_ready true
}

docker_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

log_compose_status() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  log "Compose status after controller bootstrap failure"
  (
    cd "${APP_DIR}" 2>/dev/null || return 0
    docker_compose ps -a >&2 || true
  )
  docker logs autopilot-postgres --tail 80 >&2 || true
}

on_error() {
  local rc=$?
  local line="${BASH_LINENO[0]:-${LINENO}}"
  trap - ERR
  log "controller bootstrap failed at line ${line} (rc=${rc})"
  state_text controller_bootstrap_error "controller bootstrap failed at line ${line} rc ${rc}" || true
  state_bool controller_runtime_ready false || true
  state_bool console_health_ready false || true
  log_compose_status || true
  exit "${rc}"
}

trap on_error ERR

sha256_text() {
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$1"
}

secret_file_value() {
  local path="$1"
  if [[ ! -s "${path}" ]]; then
    openssl rand -hex 48 >"${path}"
    chmod 600 "${path}"
  fi
  tr -d '\n' <"${path}"
}

secret_file_write() {
  local path="$1"
  local value="$2"
  printf '%s\n' "${value}" >"${path}"
  chmod 600 "${path}"
}

env_file_value() {
  local path="$1"
  local key="$2"
  [[ -f "${path}" ]] || return 0
  awk -F= -v key="${key}" '$1 == key {print substr($0, index($0, "=") + 1); exit}' "${path}"
}

postgres_volume_exists() {
  docker volume ls -q 2>/dev/null | grep -Eq '(^|_)autopilot-postgres$'
}

restore_migration_bundle() {
  if [[ "${SKIP_RESTORE}" == "1" || -z "${RESTORE_BUNDLE}" ]]; then
    state_bool migration_bundle_restored false
    return 0
  fi
  if [[ ! -f "${RESTORE_BUNDLE}" ]]; then
    log "migration bundle not found at ${RESTORE_BUNDLE}; continuing without restore"
    state_bool migration_bundle_restored false
    return 0
  fi

  log "restoring migration bundle"
  local tmp
  tmp="$(mktemp -d)"
  tar -xzf "${RESTORE_BUNDLE}" -C "${tmp}"

  if [[ -f "${tmp}/.env" && ! -f "${ENV_FILE}" ]]; then
    cp -p "${tmp}/.env" "${ENV_FILE}"
  elif [[ -f "${tmp}/.env" ]]; then
    mkdir -p "${SETUP_DIR}/migration"
    cp -p "${tmp}/.env" "${SETUP_DIR}/migration/restored.env"
  fi
  if [[ -d "${tmp}/secrets" ]]; then
    if [[ -d "${SECRETS_DIR}" && -n "$(find "${SECRETS_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
      mkdir -p "${SETUP_DIR}/migration/restored-secrets"
      rsync -a "${tmp}/secrets/" "${SETUP_DIR}/migration/restored-secrets/"
    else
      mkdir -p "${SECRETS_DIR}"
      rsync -a "${tmp}/secrets/" "${SECRETS_DIR}/"
    fi
  fi
  if [[ -d "${tmp}/inventory/group_vars/all" ]]; then
    mkdir -p "${APP_DIR}/inventory/group_vars/all"
    rsync -a "${tmp}/inventory/group_vars/all/" "${APP_DIR}/inventory/group_vars/all/"
  fi
  if [[ -d "${tmp}/output/setup" ]]; then
    mkdir -p "${SETUP_DIR}"
    rsync -a "${tmp}/output/setup/" "${SETUP_DIR}/"
  fi
  if [[ -f "${tmp}/postgres.sql" ]]; then
    mkdir -p "${SETUP_DIR}/migration"
    cp -p "${tmp}/postgres.sql" "${SETUP_DIR}/migration/postgres.sql"
    state_text migration_postgres_dump "${SETUP_DIR}/migration/postgres.sql"
  fi

  rm -rf "${tmp}"
  state_text controller_migration_bundle "${RESTORE_BUNDLE}"
  state_bool migration_bundle_restored true
  state_bool controller_migration_bundle_restored true
}

ensure_env() {
  log "creating controller .env"
  mkdir -p "${SECRETS_DIR}" "${SETUP_DIR}" "${APP_DIR}/output" "${APP_DIR}/jobs" "${APP_DIR}/cache/cloudosd"
  chmod 700 "${SECRETS_DIR}"

  local postgres_password mcp_token fleet_token fleet_hash controller_ip restored_env
  restored_env="${SETUP_DIR}/migration/restored.env"
  postgres_password=""
  if postgres_volume_exists; then
    postgres_password="$(env_file_value "${restored_env}" AUTOPILOT_POSTGRES_PASSWORD)"
    if [[ -z "${postgres_password}" ]]; then
      postgres_password="$(env_file_value "${ENV_FILE}" AUTOPILOT_POSTGRES_PASSWORD)"
    fi
  fi
  if [[ -n "${postgres_password}" ]]; then
    secret_file_write "${SECRETS_DIR}/postgres-password" "${postgres_password}"
  else
    postgres_password="$(secret_file_value "${SECRETS_DIR}/postgres-password")"
  fi
  mcp_token="$(secret_file_value "${SECRETS_DIR}/mcp-token")"
  fleet_token="$(secret_file_value "${SECRETS_DIR}/fleet-bootstrap-token")"
  fleet_hash="$(sha256_text "${fleet_token}")"
  controller_ip="${CONTROLLER_IP:-$(host_ip)}"
  [[ -n "${controller_ip}" ]] || die "could not determine controller IP"

  cat >"${ENV_FILE}" <<EOF
AUTOPILOT_POSTGRES_PASSWORD=${postgres_password}
AUTOPILOT_MCP_TOKEN=${mcp_token}
AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256=${fleet_hash}
AUTOPILOT_BASE_URL=http://${controller_ip}:5000
AUTOPILOT_AUTH_MODE=local
EOF
  chmod 600 "${ENV_FILE}"
  state_text controller_ip "${controller_ip}"
  state_text controller_url "http://${controller_ip}:5000"
  state_text base_url "http://${controller_ip}:5000"
  state_text controller_auth_mode "local"
  state_bool controller_env_ready true
}

update_vars_yml() {
  log "writing controller-local Proxmox API defaults into vars.yml"
  [[ -n "${PVE_HOST}" ]] || die "--pve-host is required"
  PVE_INIT_HOST="${PVE_HOST}" \
  PVE_INIT_NODE="${PVE_NODE}" \
  PVE_INIT_ISO_STORAGE="${PVE_ISO_STORAGE}" \
  PVE_INIT_DISK_STORAGE="${PVE_DISK_STORAGE}" \
  PVE_INIT_BRIDGE="${PVE_BRIDGE}" \
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
    "proxmox_validate_certs": False,
}
optional_updates = {
    "proxmox_node": os.environ.get("PVE_INIT_NODE", "").strip(),
    "proxmox_iso_storage": os.environ.get("PVE_INIT_ISO_STORAGE", "").strip(),
    "proxmox_storage": os.environ.get("PVE_INIT_DISK_STORAGE", "").strip(),
    "proxmox_bridge": os.environ.get("PVE_INIT_BRIDGE", "").strip(),
}
for key, value in optional_updates.items():
    if value:
        updates[key] = value

def yaml_value(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
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
  state_bool controller_repo_config_ready true
  state_text pve_host_ip "${PVE_HOST}"
  [[ -n "${PVE_NODE}" ]] && state_text pve_node "${PVE_NODE}"
  [[ -n "${PVE_ISO_STORAGE}" ]] && state_text pve_iso_storage "${PVE_ISO_STORAGE}"
  [[ -n "${PVE_DISK_STORAGE}" ]] && state_text pve_disk_storage "${PVE_DISK_STORAGE}"
  [[ -n "${PVE_BRIDGE}" ]] && state_text pve_bridge "${PVE_BRIDGE}"
}

build_seed_agent() {
  log "building seed AutopilotAgent in Docker SDK container"
  bash "${SCRIPT_DIR}/build_seed_agent_container.sh"
  state_bool seed_agent_ready true
  state_bool controller_seed_agent_ready true
}

source_git_sha() {
  git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null \
    || head -n 1 "${APP_DIR}/VERSION" 2>/dev/null \
    || echo unknown
}

source_build_time() {
  if git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    if git -C "${REPO_ROOT}" diff --quiet --ignore-submodules -- \
      && git -C "${REPO_ROOT}" diff --cached --quiet --ignore-submodules --; then
      git -C "${REPO_ROOT}" show -s --format=%cI HEAD
      return 0
    fi
  fi
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

build_web_image() {
  log "building web image locally from source"
  local git_sha build_time
  git_sha="$(source_git_sha)"
  build_time="$(source_build_time)"
  (
    cd "${APP_DIR}"
    DOCKER_BUILDKIT=1 docker build \
      --network host \
      --security-opt apparmor=unconfined \
      --security-opt seccomp=unconfined \
      -t "${IMAGE_TAG}" \
      --build-arg "GIT_SHA=${git_sha}" \
      --build-arg "BUILD_TIME=${build_time}" \
      .
  )
  state_bool web_image_ready true
  state_text web_image_tag "${IMAGE_TAG}"
  state_text web_image_git_sha "${git_sha}"
  state_text web_image_build_time "${build_time}"
}

ensure_postgres_database() {
  log "verifying Postgres app database"
  local postgres_password
  postgres_password="$(env_file_value "${ENV_FILE}" AUTOPILOT_POSTGRES_PASSWORD)"
  [[ -n "${postgres_password}" ]] || die "AUTOPILOT_POSTGRES_PASSWORD is missing from controller .env"
  local health_status
  for attempt in $(seq 1 90); do
    : "${attempt}"
    health_status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' autopilot-postgres 2>/dev/null || true)"
    if [[ "${health_status}" == "healthy" ]] \
      && docker exec autopilot-postgres pg_isready -U autopilot -d autopilot >/dev/null 2>&1 \
      && docker exec autopilot-postgres psql -U autopilot -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  health_status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' autopilot-postgres 2>/dev/null || true)"
  if [[ "${health_status}" != "healthy" ]] \
    || ! docker exec autopilot-postgres pg_isready -U autopilot -d autopilot >/dev/null 2>&1 \
    || ! docker exec autopilot-postgres psql -U autopilot -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
    docker logs autopilot-postgres --tail 120 >&2 || true
    die "Postgres did not become ready for setup repair"
  fi
  if ! docker exec autopilot-postgres psql -U autopilot -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'autopilot'" | grep -qx 1; then
    docker exec autopilot-postgres createdb -U autopilot autopilot
  fi
  docker exec -i autopilot-postgres psql -U autopilot -d postgres -v ON_ERROR_STOP=1 -v autopilot_password="${postgres_password}" >/dev/null <<'SQL'
ALTER ROLE autopilot WITH PASSWORD :'autopilot_password';
SQL
  docker exec -e PGPASSWORD="${postgres_password}" autopilot-postgres \
    psql -h 127.0.0.1 -U autopilot -d autopilot -Atc "SELECT 1" | grep -qx 1 \
    || die "Postgres app database verification failed"
  state_bool postgres_database_ready true
}

restore_postgres_dump_if_empty() {
  local dump="${SETUP_DIR}/migration/postgres.sql"
  [[ -f "${dump}" ]] || return 0
  log "checking whether migrated Postgres dump should be restored"
  local table_count
  table_count="$(docker exec autopilot-postgres psql -U autopilot -d autopilot -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" 2>/dev/null || echo 0)"
  if [[ "${table_count}" != "0" ]]; then
    log "Postgres already has schema objects; preserving controller database and skipping dump restore"
    state_bool migration_postgres_restore_skipped true
    return 0
  fi
  docker exec -i autopilot-postgres psql -U autopilot -d autopilot <"${dump}"
  state_bool migration_postgres_restored true
}

start_compose() {
  log "starting Compose stack"
  (
    cd "${APP_DIR}"
    docker_compose up -d autopilot-postgres
  )
  ensure_postgres_database
  restore_postgres_dump_if_empty
  (
    cd "${APP_DIR}"
    docker_compose up -d --force-recreate autopilot autopilot-builder autopilot-monitor autopilot-mcp
  )
  state_bool compose_ready true
  state_bool controller_compose_ready true
}

verify_web_health() {
  local controller_ip
  controller_ip="${CONTROLLER_IP:-$(state_value controller_ip)}"
  [[ -n "${controller_ip}" ]] || controller_ip="$(host_ip)"
  log "verifying web health"
  for attempt in $(seq 1 90); do
    : "${attempt}"
    if curl -fsS "http://${controller_ip}:5000/healthz" >/dev/null || curl -fsS http://127.0.0.1:5000/healthz >/dev/null; then
      curl -fsS "http://${controller_ip}:5000/api/version" >/dev/null || true
      state_bool console_health_ready true
      state_bool controller_runtime_ready true
      state_text controller_url "http://${controller_ip}:5000"
      state_text base_url "http://${controller_ip}:5000"
      return
    fi
    sleep 2
  done
  docker logs autopilot --tail 120 >&2 || true
  die "web UI did not become healthy at http://${controller_ip}:5000"
}

main() {
  require_root
  require_command python3
  mkdir -p "${SETUP_DIR}"
  state_text phase "controller-bootstrap"
  state_bool pve_host_clean_ready true
  install_runtime_prereqs
  restore_migration_bundle
  state_text phase "controller-bootstrap"
  state_bool controller_docker_ready true
  ensure_env
  update_vars_yml
  build_seed_agent
  build_web_image
  start_compose
  verify_web_health
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir)
      APP_DIR="$(cd "${2:-}" && pwd)"
      SCRIPT_DIR="${APP_DIR}/scripts"
      REPO_ROOT="$(cd "${APP_DIR}/.." && pwd)"
      SETUP_DIR="${APP_DIR}/output/setup"
      STATE_FILE="${SETUP_DIR}/foundation_state.json"
      ENV_FILE="${APP_DIR}/.env"
      VARS_FILE="${APP_DIR}/inventory/group_vars/all/vars.yml"
      SECRETS_DIR="${APP_DIR}/secrets"
      shift 2
      ;;
    --pve-host) PVE_HOST="${2:-}"; shift 2 ;;
    --pve-node) PVE_NODE="${2:-}"; shift 2 ;;
    --pve-iso-storage) PVE_ISO_STORAGE="${2:-}"; shift 2 ;;
    --pve-disk-storage) PVE_DISK_STORAGE="${2:-}"; shift 2 ;;
    --pve-bridge) PVE_BRIDGE="${2:-}"; shift 2 ;;
    --controller-ip) CONTROLLER_IP="${2:-}"; shift 2 ;;
    --restore-bundle) RESTORE_BUNDLE="${2:-}"; shift 2 ;;
    --skip-restore) SKIP_RESTORE=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ "${NON_INTERACTIVE}" == "1" ]]; then
  log "non-interactive mode enabled"
fi

main

log "state: ${STATE_FILE}"
