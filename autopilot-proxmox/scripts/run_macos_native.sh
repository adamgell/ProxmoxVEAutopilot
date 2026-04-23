#!/usr/bin/env bash
# Launch the autopilot web service natively on macOS (no Docker).
# Required for hypervisor_type=utm because utmctl is a macOS-host binary
# and cannot run inside the project's Linux container.
#
# Usage:
#   ./scripts/run_macos_native.sh        # starts the web service
#   MODE=builder ./scripts/run_macos_native.sh
#   MODE=monitor ./scripts/run_macos_native.sh
#
# For an interactive launcher that manages web/builder/monitor together,
# use ./scripts/tui.sh instead.
#
# See docs/UTM_MACOS_SETUP.md for first-time setup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

MODE="${MODE:-web}"

# macOS's Control Center binds AirPlay Receiver to :5000 by default on
# Ventura+, which makes uvicorn's default bind fail (or worse: succeed
# against AirTunes and return HTTP 403 to browsers). Pick a harmless
# alternate port unless the operator has explicitly set one. Linux
# operators using this script (rare) are unaffected.
if [[ "${MODE}" == "web" && -z "${AUTOPILOT_WEB_PORT:-}" ]]; then
  export AUTOPILOT_WEB_PORT=5055
fi
# Re-export in case it was already set externally — the child needs it.
export AUTOPILOT_WEB_PORT="${AUTOPILOT_WEB_PORT:-}"

# Builder + monitor default to /app/output inside the Docker image, but
# "/" is read-only for non-root on macOS. Point them at repo-local dirs
# (the TUI does the same) unless the operator has overridden them.
: "${AUTOPILOT_OUTPUT_DIR:=$(pwd)/output}"
: "${AUTOPILOT_JOBS_DIR:=$(pwd)/jobs}"
mkdir -p "${AUTOPILOT_OUTPUT_DIR}" "${AUTOPILOT_JOBS_DIR}"
export AUTOPILOT_OUTPUT_DIR AUTOPILOT_JOBS_DIR

# If the vault.yml was copied from production, auth_redirect_uri will
# point at the prod hostname and Entra will redirect the user there
# after login. Pin the callback to localhost for native runs unless the
# operator has explicitly set one. The localhost URL must also be
# registered as a Redirect URI in the Entra app registration.
: "${AUTOPILOT_AUTH_REDIRECT_URI:=http://localhost:${AUTOPILOT_WEB_PORT:-5055}/auth/callback}"
export AUTOPILOT_AUTH_REDIRECT_URI

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: this launcher is macOS-only (detected $(uname -s))" >&2
  echo "       on Linux/CI, use docker compose instead." >&2
  exit 1
fi

if [[ ! -x /Applications/UTM.app/Contents/MacOS/utmctl ]]; then
  echo "warning: UTM.app not found at the default location." >&2
  echo "         Install with: brew install --cask utm" >&2
fi

if [[ ! -d .venv ]]; then
  echo "error: .venv not found. Run first-time setup:" >&2
  echo "       python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

# shellcheck source=/dev/null
source .venv/bin/activate

exec python -m web.entrypoint "${MODE}"
