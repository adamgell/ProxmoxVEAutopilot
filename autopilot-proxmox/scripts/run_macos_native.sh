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
# See docs/UTM_MACOS_SETUP.md for first-time setup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

MODE="${MODE:-web}"

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
