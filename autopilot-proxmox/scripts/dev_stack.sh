#!/usr/bin/env bash
# One-shot dev launcher for native macOS: ensures deps, then runs the
# web service and the builder together in the foreground. Ctrl+C stops
# both. For interactive process control (start/stop individual
# services, view logs in panes), use ./scripts/tui.sh instead.
#
# Usage:
#   ./scripts/dev_stack.sh           # web + builder
#   WITH_MONITOR=1 ./scripts/dev_stack.sh   # also runs the monitor

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

if [[ ! -d .venv ]]; then
  echo "error: .venv not found. Create it with:" >&2
  echo "  python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

# shellcheck source=/dev/null
source .venv/bin/activate

if ! python -c "import uvicorn" >/dev/null 2>&1; then
  echo "[dev_stack] installing requirements.txt into .venv"
  if ! python -m pip --version >/dev/null 2>&1; then
    python -m ensurepip --upgrade >/dev/null 2>&1 \
      || python -m pip --version >/dev/null 2>&1 \
      || { echo "error: no pip in .venv and ensurepip failed; recreate the venv:" >&2;
           echo "  rm -rf .venv && python3.12 -m venv .venv" >&2; exit 1; }
  fi
  python -m pip install -r requirements.txt
fi

export AUTOPILOT_WEB_PORT="${AUTOPILOT_WEB_PORT:-5055}"
export AUTOPILOT_OUTPUT_DIR="${AUTOPILOT_OUTPUT_DIR:-$(pwd)/output}"
export AUTOPILOT_JOBS_DIR="${AUTOPILOT_JOBS_DIR:-$(pwd)/jobs}"
export AUTOPILOT_AUTH_REDIRECT_URI="${AUTOPILOT_AUTH_REDIRECT_URI:-http://localhost:${AUTOPILOT_WEB_PORT}/auth/callback}"
export AUTOPILOT_BASE_URL="${AUTOPILOT_BASE_URL:-http://192.168.2.50:${AUTOPILOT_WEB_PORT}}"
mkdir -p "${AUTOPILOT_OUTPUT_DIR}" "${AUTOPILOT_JOBS_DIR}" logs

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "[dev_stack] stopping (${pids[*]})"
  for pid in "${pids[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

launch() {
  local label="$1"; shift
  ( "$@" 2>&1 | sed -u "s/^/[${label}] /" ) &
  pids+=("$!")
}

launch web     python -m web.entrypoint web
launch builder python -m web.entrypoint builder
if [[ "${WITH_MONITOR:-0}" == "1" ]]; then
  launch monitor python -m web.entrypoint monitor
fi

echo "[dev_stack] web on http://localhost:${AUTOPILOT_WEB_PORT} (Ctrl+C to stop)"
wait
