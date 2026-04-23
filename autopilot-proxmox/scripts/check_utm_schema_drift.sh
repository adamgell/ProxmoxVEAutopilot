#!/usr/bin/env bash
# Diff UTM's configuration schema (Swift sources) against a pinned
# known-good sha. Warning-level CI job — non-zero exit iff drift exists,
# but the calling CI job runs in "continue-on-error" mode.
#
# Usage:
#   scripts/check_utm_schema_drift.sh [utm-source-dir]
#
# Defaults utm-source-dir to $UTM_SOURCE or ~/src/UTM.

set -euo pipefail

UTM_SRC="${1:-${UTM_SOURCE:-$HOME/src/UTM}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIN_FILE="$SCRIPT_DIR/../web/utm_schema_known_good.txt"

if [[ ! -d "$UTM_SRC/Configuration" ]]; then
    echo "ERROR: $UTM_SRC does not look like a UTM checkout (no Configuration/ dir)" >&2
    exit 2
fi
if [[ ! -f "$PIN_FILE" ]]; then
    echo "ERROR: pin file missing: $PIN_FILE" >&2
    exit 2
fi

PIN="$(tr -d '[:space:]' < "$PIN_FILE")"

echo "Fetching UTM upstream…"
git -C "$UTM_SRC" fetch --quiet origin main

SCHEMA_PATHS=(
    "Configuration/UTMQemuConfiguration*.swift"
    "Configuration/QEMUConstant.swift"
    "Configuration/QEMUConstantGenerated.swift"
)

echo "Pinned SHA:  $PIN"
echo "Upstream:    $(git -C "$UTM_SRC" rev-parse origin/main)"
echo

if git -C "$UTM_SRC" diff --quiet "$PIN..origin/main" -- "${SCHEMA_PATHS[@]}"; then
    echo "No schema drift since pinned sha."
    exit 0
fi

echo "WARNING: UTM schema files changed upstream since the pinned sha:"
echo
git -C "$UTM_SRC" diff --stat "$PIN..origin/main" -- "${SCHEMA_PATHS[@]}"
echo
echo "Review the diff, decide whether we need to bump the renderer or the contract,"
echo "then update $PIN_FILE with the new sha:"
echo "  git -C $UTM_SRC rev-parse origin/main > $PIN_FILE"
exit 1
