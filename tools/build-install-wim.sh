#!/usr/bin/env bash
# Build install.wim on the remote build host and register the artifact locally.
#
# Usage:  tools/build-install-wim.sh [<config.json>]
#         (default: build/build-install-wim.config.json)
#
# Requires: ssh, scp (macOS defaults), python3, jq.
# Build host: pwsh runs with -ExecutionPolicy Bypass because Build-InstallWim.ps1
# imports the unsigned Autopilot.Build module.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$REPO_ROOT/build/build-install-wim.config.json}"

if [[ ! -f "$CONFIG" ]]; then
    echo "config not found: $CONFIG" >&2
    echo "(copy build/build-install-wim.config.example.json to build/build-install-wim.config.json and edit)" >&2
    exit 1
fi
for tool in ssh scp jq python3; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

BUILD_HOST=$(jq -r '.buildHost' "$CONFIG")
BUILD_USER=$(jq -r '.buildHostUser' "$CONFIG")
BUILD_ROOT=$(jq -r '.buildRootRemote' "$CONFIG")

BUILD_CONFIG_JSON=$(jq 'del(.buildHost, .buildHostUser, .buildRootRemote)' "$CONFIG")

echo ">> ssh build host: pwsh Build-InstallWim.ps1"
SCRIPT_REMOTE="${BUILD_ROOT}/src/build/Build-InstallWim.ps1"
BUILD_OUTPUT=$(echo "$BUILD_CONFIG_JSON" | ssh "${BUILD_USER}@${BUILD_HOST}" "pwsh -NoProfile -ExecutionPolicy Bypass -File '${SCRIPT_REMOTE}' -ConfigJson -")

echo "$BUILD_OUTPUT"

WIM_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^WIM:/     {print $2}')
SIDECAR_REMOTE=$(echo "$BUILD_OUTPUT" | awk -F'[[:space:]]+' '/^Sidecar:/ {print $2}')
LOG_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^Log:/     {print $2}')
# Convert Windows backslash paths to forward slashes — scp's sftp transport mangles
# backslashes between bash shell quoting and the remote sftp helper.
WIM_REMOTE="${WIM_REMOTE//\\//}"
SIDECAR_REMOTE="${SIDECAR_REMOTE//\\//}"
LOG_REMOTE="${LOG_REMOTE//\\//}"

if [[ -z "$WIM_REMOTE" || -z "$SIDECAR_REMOTE" ]]; then
    echo "Build failed or output unparsable." >&2
    exit 2
fi

STAGING="$REPO_ROOT/var/artifacts/staging"
mkdir -p "$STAGING"
echo ">> scp artifacts → $STAGING"
scp "${BUILD_USER}@${BUILD_HOST}:${WIM_REMOTE}"     "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${SIDECAR_REMOTE}" "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${LOG_REMOTE}"     "$STAGING/"

WIM_LOCAL="$STAGING/$(basename "$WIM_REMOTE")"
SIDECAR_LOCAL="$STAGING/$(basename "$SIDECAR_REMOTE")"

echo ">> register WIM"
( cd "$REPO_ROOT/autopilot-proxmox" && python3 -m web.artifact_register \
    --path "$WIM_LOCAL" --sidecar "$SIDECAR_LOCAL" --extension wim )

echo "DONE"
