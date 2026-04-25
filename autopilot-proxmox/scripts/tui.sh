#!/usr/bin/env bash
# Launch the autopilot TUI. Wraps scripts/tui.py with the project's
# venv so operators don't have to remember to activate it first.
#
# Usage: ./scripts/tui.sh
#
# The TUI manages `python -m web.entrypoint {web,builder,monitor}`
# processes and is the recommended way to run the stack natively on
# macOS (UTM backend). Docker/Linux operators should continue to use
# `docker compose up`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

if [[ ! -d .venv ]]; then
  echo "error: .venv not found at $(pwd)/.venv" >&2
  echo "       First-time setup: python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  echo "       See docs/UTM_MACOS_SETUP.md for the full recipe." >&2
  exit 1
fi

# shellcheck source=/dev/null
source .venv/bin/activate

exec python scripts/tui.py "$@"
