#!/usr/bin/env bash
# Build the PE WIM on the remote build host and register the artifact locally.
#
# Usage:  tools/build-pe-wim.sh [<config.json>]
#         (default: build/build-pe-wim.config.json)
#
# Requires: ssh, rsync, scp (macOS defaults), python3, jq.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$REPO_ROOT/build/build-pe-wim.config.json}"

if [[ ! -f "$CONFIG" ]]; then
    echo "config not found: $CONFIG" >&2
    echo "(copy build/build-pe-wim.config.example.json to build/build-pe-wim.config.json and edit)" >&2
    exit 1
fi
for tool in ssh rsync scp jq python3; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

BUILD_HOST=$(jq -r '.buildHost' "$CONFIG")
BUILD_USER=$(jq -r '.buildHostUser' "$CONFIG")
BUILD_ROOT=$(jq -r '.buildRootRemote' "$CONFIG")
PAYLOAD_DIR_REMOTE=$(jq -r '.payloadDir' "$CONFIG")
OUTPUT_DIR=$(jq -r '.outputDir' "$CONFIG")
ARCH=$(jq -r '.architecture' "$CONFIG")

# --- Convert the config to one Build-PeWim.ps1 will accept (drop dev-Mac-only fields) ---
BUILD_CONFIG_JSON=$(jq 'del(.buildHost, .buildHostUser, .buildRootRemote)' "$CONFIG")

# --- 1. rsync the PE payload tree to the build host ---
echo ">> rsync PE payload → ${BUILD_USER}@${BUILD_HOST}:${PAYLOAD_DIR_REMOTE}"
# rsync -e ssh requires forward-slash paths on the SSH side; OpenSSH on Windows handles them.
rsync -av --delete --exclude '.gitkeep' \
    "$REPO_ROOT/build/pe-payload/" \
    "${BUILD_USER}@${BUILD_HOST}:${PAYLOAD_DIR_REMOTE}/"

# --- 2. ssh + run Build-PeWim.ps1 with config on stdin ---
echo ">> ssh build host: pwsh Build-PeWim.ps1"
SCRIPT_REMOTE="${BUILD_ROOT}/src/build/Build-PeWim.ps1"
BUILD_OUTPUT=$(echo "$BUILD_CONFIG_JSON" | ssh "${BUILD_USER}@${BUILD_HOST}" "pwsh -NoProfile -File '${SCRIPT_REMOTE}' -ConfigJson -")

echo "$BUILD_OUTPUT"

# Parse output: lines "WIM: ...", "ISO: ...", "Sidecar: ...", "Log: ..." appear on success.
WIM_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^WIM:/     {print $2}')
ISO_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^ISO:/     {print $2}')
SIDECAR_REMOTE=$(echo "$BUILD_OUTPUT" | awk -F'[[:space:]]+' '/^Sidecar:/ {print $2}')
LOG_REMOTE=$(echo "$BUILD_OUTPUT"     | awk -F'[[:space:]]+' '/^Log:/     {print $2}')

if [[ -z "$WIM_REMOTE" || -z "$SIDECAR_REMOTE" ]]; then
    echo "Build failed or output unparsable." >&2
    exit 2
fi

# --- 3. scp artifacts back ---
STAGING="$REPO_ROOT/var/artifacts/staging"
mkdir -p "$STAGING"
echo ">> scp artifacts → $STAGING"
scp "${BUILD_USER}@${BUILD_HOST}:${WIM_REMOTE}"     "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${ISO_REMOTE}"     "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${SIDECAR_REMOTE}" "$STAGING/"
scp "${BUILD_USER}@${BUILD_HOST}:${LOG_REMOTE}"     "$STAGING/"

WIM_LOCAL="$STAGING/$(basename "$WIM_REMOTE")"
ISO_LOCAL="$STAGING/$(basename "$ISO_REMOTE")"
SIDECAR_LOCAL="$STAGING/$(basename "$SIDECAR_REMOTE")"

# --- 4. register the WIM ---
echo ">> register WIM"
( cd "$REPO_ROOT/autopilot-proxmox" && python3 -m web.artifact_register \
    --path "$WIM_LOCAL" --sidecar "$SIDECAR_LOCAL" --extension wim )

# --- 5. ISO is registered separately (different sha, different sidecar field) ---
# For v1 we skip ISO registration in the index — the orchestrator's manifest API doesn't
# need to serve the ISO (UTM attaches it directly). Plan 2 may add an ISO register step.
echo ">> ISO staged at $ISO_LOCAL (not registered to artifact-store in v1; UTM attaches directly)"

echo "DONE"
