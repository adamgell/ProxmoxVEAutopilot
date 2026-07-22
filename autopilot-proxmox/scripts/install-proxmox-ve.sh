#!/usr/bin/env bash
# Interactive first-run console installer for ProxmoxVEAutopilot.
#
# This is intentionally a thin shell UI over init-proxmox-ve.sh. The lower
# level init script owns all PVE/controller mutations; this script collects
# operator choices, shows state, and runs the right phases in order.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INIT_SCRIPT="${INSTALLER_INIT_SCRIPT:-${SCRIPT_DIR}/init-proxmox-ve.sh}"
STATE_HELPER="${SCRIPT_DIR}/installer_state.py"
STATE_FILE="${INSTALLER_STATE_FILE:-${APP_DIR}/output/setup/foundation_state.json}"
DETECT_FILE="${INSTALLER_DETECT_FILE:-${APP_DIR}/output/setup/installer_detect.json}"
FAILURE_FILE="${INSTALLER_FAILURE_FILE:-${APP_DIR}/output/setup/install-last-failure.json}"
INSTALL_LOG="${INSTALLER_LOG_FILE:-${APP_DIR}/output/setup/install.log}"
SUPPORT_DIR="${INSTALLER_SUPPORT_DIR:-${APP_DIR}/output/support}"

ACTION="menu"
YES=0
DRY_RUN=0
RESUME=1
MEDIA_MODE="auto"
ALLOW_WINDOWS_DOWNLOAD=0
DOWNLOAD_VIRTIO=0
RESET_MEDIA=0
SUPPORT_PRINT=0
SUPPORT_NO_BUNDLE=0

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
  --action menu|detect|recommended|guided|foundation|bootstrap|operational|runtime-config|status|support|reset-dev-lab

Options:
  --yes                      Use defaults and skip confirmations where possible.
  --dry-run                  Print init commands instead of running them.
  --resume / --no-resume     Resume idempotent init state; default is --resume.
  --manual-media             Do not download Windows media; report the media gate.
  --download-windows         Use the Microsoft software-download resolver.
  --windows-iso-url <url>    Use an operator-supplied official Microsoft direct URL.
  --windows-iso-language <l> Windows ISO language for automatic resolver.
  --download-virtio          Download VirtIO ISO.
  --no-download-virtio       Do not download VirtIO ISO.
  --support-print            Print the generated GitHub issue draft.
  --support-no-bundle        Write only the issue draft and redaction report.
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
  bash scripts/install-proxmox-ve.sh --action recommended --yes
  bash scripts/install-proxmox-ve.sh --action detect
  bash scripts/install-proxmox-ve.sh --action bootstrap --yes --download-windows --download-virtio
  bash scripts/install-proxmox-ve.sh --action bootstrap --windows-iso-url "https://download.microsoft.com/..."
  bash scripts/install-proxmox-ve.sh --action support --support-print
  bash scripts/install-proxmox-ve.sh --action reset-dev-lab --reset-media --yes
USAGE
}

log() {
  printf '[installer] %s\n' "$*" >&2
}

installer_log() {
  mkdir -p "$(dirname "${INSTALL_LOG}")"
  printf '[installer] %s\n' "$*" >>"${INSTALL_LOG}"
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
    return 0
  fi
  echo
  echo "Windows media source"
  echo "  1) Automatic Microsoft software download resolver"
  echo "  2) Paste official Microsoft direct ISO URL"
  echo "  3) Manual upload to Proxmox ISO storage"
  read -r -p "Select media source [3]: " choice
  case "${choice:-3}" in
    1)
      MEDIA_MODE="auto"
      ALLOW_WINDOWS_DOWNLOAD=1
      ;;
    2)
      MEDIA_MODE="url"
      ALLOW_WINDOWS_DOWNLOAD=1
      WINDOWS_ISO_URL="$(prompt_value "Official Microsoft direct ISO URL" "${WINDOWS_ISO_URL}")"
      [[ -n "${WINDOWS_ISO_URL}" ]] || die "Windows ISO URL is required for media source 2"
      ;;
    3)
      MEDIA_MODE="manual"
      ALLOW_WINDOWS_DOWNLOAD=0
      ;;
    *) die "invalid media source: ${choice}" ;;
  esac
  if prompt_yes_no "Download VirtIO ISO automatically" "no"; then
    DOWNLOAD_VIRTIO=1
  else
    DOWNLOAD_VIRTIO=0
  fi
}

configure_targeting_interactive() {
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
}

configure_for_phase() {
  local phase="$1"
  [[ "${YES}" == "1" ]] && return 0
  configure_targeting_interactive
  case "${phase}" in
    guided|bootstrap|all) select_media_mode ;;
  esac
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
    auto)
      [[ "${ALLOW_WINDOWS_DOWNLOAD}" == "1" ]] && MEDIA_ARGS+=(--download-windows)
      ;;
    url)
      [[ -n "${WINDOWS_ISO_URL}" ]] || die "--windows-iso-url requires a URL"
      MEDIA_ARGS+=(--windows-iso-url "${WINDOWS_ISO_URL}")
      ;;
    manual) ;;
    *) die "invalid media mode: ${MEDIA_MODE}" ;;
  esac
  [[ -n "${WINDOWS_ISO_LANGUAGE}" ]] && MEDIA_ARGS+=(--windows-iso-language "${WINDOWS_ISO_LANGUAGE}")
  [[ "${DOWNLOAD_VIRTIO}" == "1" ]] && MEDIA_ARGS+=(--download-virtio)
  return 0
}

failure_step_id_for_phase() {
  local phase="$1" rc="${2:-1}"
  case "${phase}:${rc}" in
    bootstrap:20) echo "bootstrap.media|Bootstrap media|bootstrap.media.windows_iso_missing|Windows ISO media is missing" ;;
    foundation:*) echo "foundation.setup|Foundation setup|foundation.setup.phase_failed|Foundation phase failed" ;;
    bootstrap:*) echo "bootstrap.media|Bootstrap media|bootstrap.media.phase_failed|Bootstrap phase failed" ;;
    operational:*) echo "operational.repair|Operational repair|operational.repair.phase_failed|Operational phase failed" ;;
    runtime-config:*) echo "runtime_config.repair|Runtime config repair|runtime_config.repair.phase_failed|Runtime config repair failed" ;;
    *) echo "${phase}.run|${phase}|${phase}.run.phase_failed|${phase} failed" ;;
  esac
}

record_failure() {
  local action="$1" phase="$2" rc="$3" mapping
  mapping="$(failure_step_id_for_phase "${phase}" "${rc}")"
  python3 - "${FAILURE_FILE}" "${DETECT_FILE}" "${action}" "${phase}" "${rc}" "${mapping}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

failure_path = Path(sys.argv[1])
detect_path = Path(sys.argv[2])
action, phase, rc, mapping = sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6]
step_id, step_label, check_id, check_label = mapping.split("|", 3)
try:
    detection = json.loads(detect_path.read_text(encoding="utf-8"))
except Exception:
    detection = {}
payload = {
    "schema": 1,
    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    "action": action,
    "phase": phase,
    "step_id": step_id,
    "step_label": step_label,
    "check_id": check_id,
    "check_label": check_label,
    "exit_code": rc,
    "classification": detection.get("classification", ""),
    "confidence": detection.get("confidence", ""),
    "blocked_reasons": detection.get("blocked_reasons", []),
    "recommended_action": detection.get("recommended_action", ""),
    "sanitized_planned_commands": detection.get("planned_commands", []),
}
failure_path.parent.mkdir(parents=True, exist_ok=True)
failure_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

show_failure_footer() {
  local phase="$1" rc="$2" mapping
  mapping="$(failure_step_id_for_phase "${phase}" "${rc}")"
  IFS='|' read -r step_id step_label check_id check_label <<<"${mapping}"
  cat <<EOF

${step_label} did not complete.

Step: ${step_label}
Step ID: ${step_id}
Failed check: ${check_label}
Check ID: ${check_id}
Exit code: ${rc}

Next actions:
  1) Retry recommended repair
  2) Detection details
  3) Manual phase selection
  4) Create GitHub issue / support bundle
  0) Return to main menu
EOF
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
      if ((${#MEDIA_ARGS[@]})); then
        cmd+=("${MEDIA_ARGS[@]}")
      fi
      ;;
    reset-dev-lab)
      [[ "${RESET_MEDIA}" == "1" ]] && cmd+=(--reset-media)
      ;;
  esac

  echo
  echo "Running:"
  quote_cmd "${cmd[@]}"
  installer_log "running phase=${phase} command=$(quote_cmd "${cmd[@]}")"
  if [[ "${DRY_RUN}" == "1" ]]; then
    return 0
  fi

  set +e
  "${cmd[@]}"
  rc=$?
  set -e
  if [[ "${rc}" != "0" ]]; then
    run_detect >/dev/null 2>&1 || true
    record_failure "${ACTION}" "${phase}" "${rc}" || true
    installer_log "phase=${phase} failed rc=${rc}"
    if [[ "${rc}" == "20" ]]; then
      echo
      echo "Media gate is still blocked."
      echo "Upload Windows ISO media to Proxmox ISO storage, or rerun bootstrap with"
      echo "--download-windows or --windows-iso-url."
    fi
    show_failure_footer "${phase}" "${rc}"
  fi
  return "${rc}"
}

run_detect() {
  local show_commands="${1:-0}"
  local args
  args=(python3 "${STATE_HELPER}" detect --state-file "${STATE_FILE}" --output "${DETECT_FILE}")
  [[ "${ALLOW_WINDOWS_DOWNLOAD}" == "1" ]] && args+=(--allow-windows-download)
  [[ "${DOWNLOAD_VIRTIO}" == "1" ]] && args+=(--allow-virtio-download)
  [[ -n "${WINDOWS_ISO_URL}" ]] && args+=(--windows-iso-url "${WINDOWS_ISO_URL}")
  [[ "${show_commands}" == "1" ]] && args+=(--show-commands)
  "${args[@]}"
}

detect_value() {
  local key="$1"
  python3 - "${DETECT_FILE}" "${key}" <<'PY'
import json
import sys
path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
value = data.get(key, "")
if isinstance(value, bool):
    print("1" if value else "0")
elif isinstance(value, list):
    print(",".join(str(item) for item in value))
else:
    print(value)
PY
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
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception as exc:
    print(f"State file is unreadable: {path} ({exc})")
    raise SystemExit(0)
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

show_status() {
  run_detect
  show_state
}

guided_install() {
  local rc
  configure_for_phase guided
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
      echo "Resume after media is available with:"
      echo "  bash ${0} --action recommended --yes"
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
  configure_for_phase "${phase}"
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

run_recommended() {
  local action safe confidence
  run_detect
  action="$(detect_value recommended_action)"
  safe="$(detect_value safe_to_auto_run)"
  confidence="$(detect_value confidence)"

  if [[ "${safe}" != "1" || "${confidence}" == "low" || -z "${action}" ]]; then
    echo
    echo "Recommended repair is not safe to run automatically."
    echo "Use Detection details or Create GitHub issue / support bundle for next steps."
    return 2
  fi

  case "${action}" in
    foundation|bootstrap|operational|runtime-config)
      run_single_phase "${action}"
      ;;
    status|none)
      show_state
      ;;
    *)
      die "invalid recommended action from detection: ${action}"
      ;;
  esac
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

create_support_bundle() {
  local args
  run_detect >/dev/null 2>&1 || true
  args=(
    python3 "${STATE_HELPER}" support
    --detection-file "${DETECT_FILE}"
    --failure-file "${FAILURE_FILE}"
    --log-file "${INSTALL_LOG}"
    --output-dir "${SUPPORT_DIR}"
  )
  [[ "${SUPPORT_NO_BUNDLE}" == "1" ]] && args+=(--no-bundle)
  [[ "${SUPPORT_PRINT}" == "1" ]] && args+=(--print)
  "${args[@]}"
}

show_one_liners() {
  cat <<EOF

Common one-liners
-----------------
Continue safest detected repair:
  bash ${0} --action recommended --yes

Guided install / repair:
  bash ${0} --action guided --yes

Foundation only:
  bash ${0} --action foundation --yes

Bootstrap with official media downloads:
  bash ${0} --action bootstrap --yes --download-windows --download-virtio

Bootstrap with direct Windows ISO URL:
  bash ${0} --action bootstrap --yes --windows-iso-url "https://download.microsoft.com/..." --download-virtio

Operational repair/promote:
  bash ${0} --action operational --yes

Create sanitized support issue draft:
  bash ${0} --action support --support-print

Reset disposable dev lab:
  bash ${0} --action reset-dev-lab --reset-media --yes
EOF
}

manual_phase_menu() {
  local choice
  while true; do
    echo
    echo "Choose phase"
    echo "------------"
    echo "  1) Auto-detect again"
    echo "  2) Foundation"
    echo "  3) Bootstrap media"
    echo "  4) Operational repair/promote"
    echo "  5) Runtime config repair"
    echo "  6) Status only"
    echo "  7) Create GitHub issue / support bundle"
    echo "  8) Reset disposable dev lab"
    echo "  9) Return to main menu"
    echo "  0) Quit"
    read -r -p "Select phase: " choice
    case "${choice}" in
      1) run_detect 1 ;;
      2) run_single_phase foundation || true ;;
      3) run_single_phase bootstrap || true ;;
      4) run_single_phase operational || true ;;
      5) run_single_phase runtime-config || true ;;
      6) show_status ;;
      7) create_support_bundle ;;
      8) run_reset || true; show_state ;;
      9) return 0 ;;
      0|q|Q) exit 0 ;;
      *) echo "Invalid selection." ;;
    esac
  done
}

menu_loop() {
  local choice
  [[ -t 0 ]] || die "interactive menu requires a TTY; use --action recommended --yes for unattended runs"
  while true; do
    clear 2>/dev/null || true
    echo "ProxmoxVEAutopilot First-Run Installer"
    echo "======================================"
    run_detect || true
    show_state
    echo
    echo "Main menu"
    echo "---------"
    echo "  1) Continue recommended repair"
    echo "  2) Guided install / repair"
    echo "  3) Detection details"
    echo "  4) Manual phase selection"
    echo "  5) Configure install inputs"
    echo "  6) Show one-liners"
    echo "  7) Create GitHub issue / support bundle"
    echo "  0) Quit without changes"
    echo
    read -r -p "Select action: " choice
    case "${choice}" in
      1) run_recommended || true ;;
      2) guided_install || true ;;
      3) run_detect 1 ;;
      4) manual_phase_menu ;;
      5) configure_for_phase guided ;;
      6) show_one_liners ;;
      7) create_support_bundle ;;
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
      --manual-media) MEDIA_MODE="manual"; ALLOW_WINDOWS_DOWNLOAD=0; shift ;;
      --download-windows) MEDIA_MODE="auto"; ALLOW_WINDOWS_DOWNLOAD=1; shift ;;
      --windows-iso-url) MEDIA_MODE="url"; ALLOW_WINDOWS_DOWNLOAD=1; WINDOWS_ISO_URL="${2:-}"; shift 2 ;;
      --windows-iso-language) WINDOWS_ISO_LANGUAGE="${2:-}"; shift 2 ;;
      --download-virtio) DOWNLOAD_VIRTIO=1; shift ;;
      --no-download-virtio) DOWNLOAD_VIRTIO=0; shift ;;
      --support-print) SUPPORT_PRINT=1; shift ;;
      --support-no-bundle) SUPPORT_NO_BUNDLE=1; shift ;;
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
  [[ -x "${STATE_HELPER}" || -f "${STATE_HELPER}" ]] || die "state helper not found: ${STATE_HELPER}"
  parse_args "$@"
  if [[ "${MEDIA_MODE}" == "url" && -z "${WINDOWS_ISO_URL}" ]]; then
    die "--windows-iso-url requires a URL"
  fi

  case "${ACTION}" in
    menu) menu_loop ;;
    detect) run_detect ;;
    recommended) run_recommended ;;
    guided) guided_install ;;
    foundation) run_single_phase foundation ;;
    bootstrap) run_single_phase bootstrap ;;
    operational) run_single_phase operational ;;
    runtime-config) run_single_phase runtime-config ;;
    status) show_status ;;
    support) create_support_bundle ;;
    reset-dev-lab) run_reset; show_state ;;
    *) die "invalid --action: ${ACTION}" ;;
  esac
}

main "$@"
