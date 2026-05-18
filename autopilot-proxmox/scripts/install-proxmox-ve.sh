#!/usr/bin/env bash
# Interactive first-run console installer for ProxmoxVEAutopilot.
#
# This is intentionally a thin shell UI over init-proxmox-ve.sh. The lower
# level init script owns all PVE/controller mutations; this script collects
# operator choices, shows state, and runs the right phases in order.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INIT_SCRIPT="${SCRIPT_DIR}/init-proxmox-ve.sh"
STATE_FILE="${APP_DIR}/output/setup/foundation_state.json"

ACTION="menu"
YES=0
DRY_RUN=0
RESUME=1
MEDIA_MODE="auto"
DOWNLOAD_VIRTIO=1
RESET_MEDIA=0

NODE=""
ISO_STORAGE=""
CONTROLLER_IP=""
CONTROLLER_CIDR=""
CONTROLLER_GATEWAY=""
CONTROLLER_DNS=""
CONTROLLER_VMID=""
CONTROLLER_STORAGE=""
CONTROLLER_BRIDGE=""
WINDOWS_ISO_URL=""
WINDOWS_ISO_LANGUAGE=""

COMMON_ARGS=()
MEDIA_ARGS=()

usage() {
  cat <<'USAGE'
Usage:
  install-proxmox-ve.sh [options]

Default:
  Launch an interactive console installer on the Proxmox VE shell.

Actions:
  --action menu|guided|foundation|bootstrap|operational|runtime-config|status|reset-dev-lab

Options:
  --yes                      Use defaults and skip confirmations where possible.
  --dry-run                  Print init commands instead of running them.
  --resume / --no-resume     Resume idempotent init state; default is --resume.
  --manual-media             Do not download Windows media; report the media gate.
  --download-windows         Use the Microsoft software-download resolver.
  --windows-iso-url <url>    Use an operator-supplied official Microsoft direct URL.
  --windows-iso-language <l> Windows ISO language for automatic resolver.
  --download-virtio          Download VirtIO ISO; default enabled.
  --no-download-virtio       Do not download VirtIO ISO.
  --node <node>              Proxmox node override.
  --iso-storage <storage>    ISO storage override.
  --controller-ip <ip>       Static or already-known controller IP.
  --controller-cidr <prefix> Static controller CIDR prefix.
  --controller-gateway <ip>  Static controller gateway.
  --controller-dns <ip>      Static controller DNS server.
  --controller-vmid <vmid>   Controller VMID override.
  --controller-storage <s>   Controller VM disk storage override.
  --controller-bridge <br>   Controller network bridge override.
  --reset-media              With reset-dev-lab, remove generated/downloaded lab ISOs.
  --help                     Show this help.

Examples:
  bash scripts/install-proxmox-ve.sh
  bash scripts/install-proxmox-ve.sh --action guided --yes
  bash scripts/install-proxmox-ve.sh --action bootstrap --windows-iso-url "https://download.microsoft.com/..."
  bash scripts/install-proxmox-ve.sh --action reset-dev-lab --reset-media --yes
USAGE
}

log() {
  printf '[installer] %s\n' "$*" >&2
}

die() {
  echo "error: $*" >&2
  exit 1
}

quote_cmd() {
  local item
  for item in "$@"; do
    printf '%q ' "${item}"
  done
  printf '\n'
}

prompt_value() {
  local label="$1"
  local current="$2"
  local answer
  if [[ "${YES}" == "1" ]]; then
    printf '%s' "${current}"
    return 0
  fi
  if [[ -n "${current}" ]]; then
    read -r -p "${label} [${current}]: " answer
    printf '%s' "${answer:-${current}}"
  else
    read -r -p "${label} [auto]: " answer
    printf '%s' "${answer}"
  fi
}

prompt_yes_no() {
  local label="$1"
  local default="${2:-yes}"
  local answer suffix
  if [[ "${default}" == "yes" ]]; then
    suffix="Y/n"
  else
    suffix="y/N"
  fi
  if [[ "${YES}" == "1" ]]; then
    if [[ "${default}" == "yes" ]]; then
      return 0
    fi
    return 1
  fi
  while true; do
    read -r -p "${label} [${suffix}]: " answer
    answer="${answer:-${default}}"
    case "${answer}" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) echo "Please enter yes or no." ;;
    esac
  done
}

select_media_mode() {
  local choice
  if [[ "${YES}" == "1" ]]; then
    MEDIA_MODE="${MEDIA_MODE:-auto}"
    return 0
  fi
  echo
  echo "Windows media source"
  echo "  1) Automatic Microsoft software download resolver (recommended)"
  echo "  2) Paste official Microsoft direct ISO URL"
  echo "  3) Manual upload to Proxmox ISO storage"
  read -r -p "Select media source [1]: " choice
  case "${choice:-1}" in
    1) MEDIA_MODE="auto" ;;
    2)
      MEDIA_MODE="url"
      WINDOWS_ISO_URL="$(prompt_value "Official Microsoft direct ISO URL" "${WINDOWS_ISO_URL}")"
      [[ -n "${WINDOWS_ISO_URL}" ]] || die "Windows ISO URL is required for media source 2"
      ;;
    3) MEDIA_MODE="manual" ;;
    *) die "invalid media source: ${choice}" ;;
  esac
  if prompt_yes_no "Download VirtIO ISO automatically" "yes"; then
    DOWNLOAD_VIRTIO=1
  else
    DOWNLOAD_VIRTIO=0
  fi
}

configure_interactive() {
  [[ "${YES}" == "1" ]] && return 0

  echo
  echo "PVE targeting"
  NODE="$(prompt_value "Proxmox node" "${NODE}")"
  ISO_STORAGE="$(prompt_value "ISO storage" "${ISO_STORAGE}")"

  echo
  echo "Controller VM"
  CONTROLLER_IP="$(prompt_value "Controller IP (blank for DHCP/QGA discovery)" "${CONTROLLER_IP}")"
  if [[ -n "${CONTROLLER_IP}" ]]; then
    CONTROLLER_CIDR="$(prompt_value "Controller CIDR prefix" "${CONTROLLER_CIDR:-24}")"
    CONTROLLER_GATEWAY="$(prompt_value "Controller gateway" "${CONTROLLER_GATEWAY}")"
    CONTROLLER_DNS="$(prompt_value "Controller DNS" "${CONTROLLER_DNS:-${CONTROLLER_GATEWAY}}")"
  fi
  CONTROLLER_VMID="$(prompt_value "Controller VMID" "${CONTROLLER_VMID}")"
  CONTROLLER_STORAGE="$(prompt_value "Controller VM storage" "${CONTROLLER_STORAGE}")"
  CONTROLLER_BRIDGE="$(prompt_value "Controller network bridge" "${CONTROLLER_BRIDGE}")"

  select_media_mode
}

build_common_args() {
  COMMON_ARGS=()
  [[ "${RESUME}" == "1" ]] && COMMON_ARGS+=(--resume)
  [[ "${YES}" == "1" ]] && COMMON_ARGS+=(--non-interactive)
  [[ -n "${NODE}" ]] && COMMON_ARGS+=(--node "${NODE}")
  [[ -n "${ISO_STORAGE}" ]] && COMMON_ARGS+=(--iso-storage "${ISO_STORAGE}")
  [[ -n "${CONTROLLER_IP}" ]] && COMMON_ARGS+=(--controller-ip "${CONTROLLER_IP}")
  [[ -n "${CONTROLLER_CIDR}" ]] && COMMON_ARGS+=(--controller-cidr "${CONTROLLER_CIDR}")
  [[ -n "${CONTROLLER_GATEWAY}" ]] && COMMON_ARGS+=(--controller-gateway "${CONTROLLER_GATEWAY}")
  [[ -n "${CONTROLLER_DNS}" ]] && COMMON_ARGS+=(--controller-dns "${CONTROLLER_DNS}")
  [[ -n "${CONTROLLER_VMID}" ]] && COMMON_ARGS+=(--controller-vmid "${CONTROLLER_VMID}")
  [[ -n "${CONTROLLER_STORAGE}" ]] && COMMON_ARGS+=(--controller-storage "${CONTROLLER_STORAGE}")
  [[ -n "${CONTROLLER_BRIDGE}" ]] && COMMON_ARGS+=(--controller-bridge "${CONTROLLER_BRIDGE}")
  return 0
}

build_media_args() {
  MEDIA_ARGS=()
  case "${MEDIA_MODE}" in
    auto) MEDIA_ARGS+=(--download-windows) ;;
    url) MEDIA_ARGS+=(--windows-iso-url "${WINDOWS_ISO_URL}") ;;
    manual) ;;
    *) die "invalid media mode: ${MEDIA_MODE}" ;;
  esac
  [[ -n "${WINDOWS_ISO_LANGUAGE}" ]] && MEDIA_ARGS+=(--windows-iso-language "${WINDOWS_ISO_LANGUAGE}")
  [[ "${DOWNLOAD_VIRTIO}" == "1" ]] && MEDIA_ARGS+=(--download-virtio)
  return 0
}

run_init_phase() {
  local phase="$1"
  local rc=0
  local cmd
  build_common_args
  cmd=(bash "${INIT_SCRIPT}" --phase "${phase}")
  cmd+=("${COMMON_ARGS[@]}")
  case "${phase}" in
    bootstrap|all)
      build_media_args
      cmd+=("${MEDIA_ARGS[@]}")
      ;;
    reset-dev-lab)
      [[ "${RESET_MEDIA}" == "1" ]] && cmd+=(--reset-media)
      ;;
  esac

  echo
  echo "Running:"
  quote_cmd "${cmd[@]}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    return 0
  fi

  set +e
  "${cmd[@]}"
  rc=$?
  set -e
  if [[ "${rc}" == "20" ]]; then
    echo
    echo "Media gate is still blocked."
    echo "Upload Windows ISO media to Proxmox ISO storage, or rerun bootstrap with"
    echo "--download-windows or --windows-iso-url."
  fi
  return "${rc}"
}

show_state() {
  echo
  echo "Current setup state"
  echo "-------------------"
  if [[ ! -f "${STATE_FILE}" ]]; then
    echo "No state file yet: ${STATE_FILE}"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "State file: ${STATE_FILE}"
    return 0
  fi
  python3 - "${STATE_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
keys = [
    "phase",
    "pve_node",
    "pve_host_ip",
    "pve_iso_storage",
    "pve_disk_storage",
    "controller_vm_ready",
    "controller_ip",
    "controller_url",
    "controller_runtime_ready",
    "console_health_ready",
    "windows_iso_ready",
    "windows_iso_volid",
    "virtio_iso_ready",
    "virtio_iso_volid",
    "media_ready",
    "build_host_creation_owner",
    "build_host_vm_ready",
    "promoted_artifacts_ready",
    "operational_ready",
    "dev_lab_reset_ready",
    "updated_at",
]
for key in keys:
    if key in data:
        print(f"{key}: {data[key]}")
PY
}

guided_install() {
  local rc
  configure_interactive
  set +e
  run_init_phase foundation
  rc=$?
  set -e
  if [[ "${rc}" != "0" ]]; then
    show_state
    return "${rc}"
  fi
  show_state
  set +e
  run_init_phase bootstrap
  rc=$?
  set -e
  if [[ "${rc}" != "0" ]]; then
    show_state
    if [[ "${rc}" == "20" ]]; then
      echo
      echo "Resume from this installer after media is available with action 3 or:"
      echo "  bash ${0} --action bootstrap --yes --download-windows --download-virtio"
    fi
    return "${rc}"
  fi
  show_state
  set +e
  run_init_phase operational
  rc=$?
  set -e
  if [[ "${rc}" != "0" ]]; then
    show_state
    return "${rc}"
  fi
  show_state
  echo
  echo "Core operator console init path completed. Open /setup on the controller URL above for build-host and artifact readiness."
}

run_single_phase() {
  local phase="$1"
  local rc
  configure_interactive
  set +e
  run_init_phase "${phase}"
  rc=$?
  set -e
  if [[ "${rc}" != "0" ]]; then
    show_state
    return "${rc}"
  fi
  show_state
}

confirm_reset() {
  [[ "${YES}" == "1" ]] && return 0
  echo
  echo "This destroys disposable Autopilot dev-lab VMs and optionally generated media."
  echo "It is intended for pvetest/reset labs, not production."
  prompt_yes_no "Continue with reset-dev-lab" "no"
}

run_reset() {
  if confirm_reset; then
    run_init_phase reset-dev-lab
  else
    echo "Reset cancelled."
  fi
}

show_one_liners() {
  cat <<EOF

Common one-liners
-----------------
Guided default install:
  bash ${0} --action guided --yes

Foundation only:
  bash ${0} --action foundation --yes

Bootstrap with official media downloads:
  bash ${0} --action bootstrap --yes --download-windows --download-virtio

Operational repair/promote:
  bash ${0} --action operational --yes

Reset disposable dev lab:
  bash ${0} --action reset-dev-lab --reset-media --yes
EOF
}

menu_loop() {
  local choice
  [[ -t 0 ]] || die "interactive menu requires a TTY; use --action guided --yes for unattended runs"
  while true; do
    clear 2>/dev/null || true
    echo "ProxmoxVEAutopilot First-Run Installer"
    echo "======================================"
    show_state
    echo
    echo "Actions"
    echo "  1) Guided install: Foundation -> Bootstrap -> Operational"
    echo "  2) Foundation only: PVE access + Ubuntu controller runtime"
    echo "  3) Bootstrap media: Windows/VirtIO media gate"
    echo "  4) Operational repair/promote"
    echo "  5) Runtime config repair"
    echo "  6) Reset disposable dev lab"
    echo "  7) Show one-liners"
    echo "  0) Quit"
    echo
    read -r -p "Select action: " choice
    case "${choice}" in
      1) guided_install || true ;;
      2) run_single_phase foundation || true ;;
      3) run_single_phase bootstrap || true ;;
      4) run_single_phase operational || true ;;
      5) run_single_phase runtime-config || true ;;
      6) run_reset || true; show_state ;;
      7) show_one_liners ;;
      0|q|Q) return 0 ;;
      *) echo "Invalid selection." ;;
    esac
    echo
    read -r -p "Press Enter to continue..." _
  done
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --action) ACTION="${2:-}"; shift 2 ;;
      --yes) YES=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --resume) RESUME=1; shift ;;
      --no-resume) RESUME=0; shift ;;
      --manual-media) MEDIA_MODE="manual"; shift ;;
      --download-windows) MEDIA_MODE="auto"; shift ;;
      --windows-iso-url) MEDIA_MODE="url"; WINDOWS_ISO_URL="${2:-}"; shift 2 ;;
      --windows-iso-language) WINDOWS_ISO_LANGUAGE="${2:-}"; shift 2 ;;
      --download-virtio) DOWNLOAD_VIRTIO=1; shift ;;
      --no-download-virtio) DOWNLOAD_VIRTIO=0; shift ;;
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
      --help|-h) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
}

main() {
  [[ -x "${INIT_SCRIPT}" || -f "${INIT_SCRIPT}" ]] || die "init script not found: ${INIT_SCRIPT}"
  parse_args "$@"
  if [[ "${MEDIA_MODE}" == "url" && -z "${WINDOWS_ISO_URL}" ]]; then
    die "--windows-iso-url requires a URL"
  fi

  case "${ACTION}" in
    menu) menu_loop ;;
    guided) guided_install ;;
    foundation) run_single_phase foundation ;;
    bootstrap) run_single_phase bootstrap ;;
    operational) run_single_phase operational ;;
    runtime-config) run_single_phase runtime-config ;;
    status) show_state ;;
    reset-dev-lab) run_reset; show_state ;;
    *) die "invalid --action: ${ACTION}" ;;
  esac
}

main "$@"
