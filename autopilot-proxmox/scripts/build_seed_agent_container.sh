#!/usr/bin/env bash
# Build the AutopilotAgent in a Docker .NET SDK container, producing BOTH
# the standalone seed executable (used by build-host bootstrap and agent
# repair) AND the MSI installer (used by /api/cloudosd/assets/autopilotagent.msi
# and the Agent Download page postinstall flow). Both come from the same
# `dotnet publish` output so they cannot drift in version.
#
# No .NET SDK, bin/, or obj/ output is written to the Proxmox host repo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${APP_DIR}/.." && pwd)"
AGENT_ROOT="${AGENT_ROOT:-${REPO_ROOT}/autopilot-agent}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${APP_DIR}/output/setup/agent-seed}"
SDK_IMAGE="${SDK_IMAGE:-mcr.microsoft.com/dotnet/sdk:8.0}"
RID_LIST="${RID_LIST:-win-x64 win-arm64}"
NUGET_VOLUME="${NUGET_VOLUME:-autopilot-dotnet-nuget}"

die() {
  echo "error: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# Read the canonical AutopilotAgent version from Directory.Build.props so the
# seed EXE and the MSI cannot disagree. Allow callers to override via env
# variable for emergency rebuilds, but warn when the override differs from
# the in-tree value.
read_source_version() {
  local props="${AGENT_ROOT}/Directory.Build.props"
  [[ -f "${props}" ]] || die "Directory.Build.props not found at ${props}"
  local v
  v="$(grep -oE '<AutopilotAgentVersion[^>]*>[^<]+</AutopilotAgentVersion>' "${props}" \
       | head -n 1 \
       | sed -E 's|.*>([^<]+)<.*|\1|')"
  [[ -n "${v}" ]] || die "could not parse AutopilotAgentVersion from ${props}"
  printf '%s' "${v}"
}

SOURCE_VERSION="$(read_source_version)"
if [[ -n "${AUTOPILOT_AGENT_VERSION:-}" ]]; then
  if [[ "${AUTOPILOT_AGENT_VERSION}" != "${SOURCE_VERSION}" ]]; then
    echo "[agent-seed] WARNING: AUTOPILOT_AGENT_VERSION override ${AUTOPILOT_AGENT_VERSION} differs from source ${SOURCE_VERSION}" >&2
  fi
  VERSION="${AUTOPILOT_AGENT_VERSION}"
else
  VERSION="${SOURCE_VERSION}"
fi

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    die "sha256sum or shasum is required"
  fi
}

require_command docker
require_command python3

[[ -f "${AGENT_ROOT}/src/AutopilotAgent/AutopilotAgent.csproj" ]] \
  || die "AutopilotAgent project not found under ${AGENT_ROOT}"

find "${AGENT_ROOT}" -type d \( -name bin -o -name obj \) -prune -exec rm -rf {} +

mkdir -p "${OUTPUT_ROOT}"
rm -rf "${OUTPUT_ROOT:?}/win-x64" "${OUTPUT_ROOT:?}/win-arm64"

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
if git -C "${REPO_ROOT}" diff --quiet >/dev/null 2>&1 \
  && git -C "${REPO_ROOT}" diff --cached --quiet >/dev/null 2>&1; then
  GIT_DIRTY="false"
else
  GIT_DIRTY="true"
fi
BUILD_TIME="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "[agent-seed] building ${RID_LIST} (agent v${VERSION}) with ${SDK_IMAGE}"
# NOTE on MSI: WiX 5 is still Windows-only (`wix.exe` aborts on Linux with
# "The WiX Toolset only supports Windows."). The MSI is therefore produced
# by the Windows build host's pipeline, not here. Drift is avoided because
# both builds read AutopilotAgentVersion from Directory.Build.props -- this
# script via read_source_version above, and AutopilotAgent.Installer.wixproj
# via MSBuild property inheritance. As long as the build host publishes the
# MSI when source changes, the standalone seed EXE and the MSI ship the
# same version.
docker run --rm \
  --mount "type=bind,src=${AGENT_ROOT},dst=/src,readonly" \
  --mount "type=bind,src=${OUTPUT_ROOT},dst=/out" \
  --mount "type=volume,src=${NUGET_VOLUME},dst=/root/.nuget/packages" \
  -e "RID_LIST=${RID_LIST}" \
  -e "AUTOPILOT_AGENT_VERSION=${VERSION}" \
  "${SDK_IMAGE}" \
  bash -lc '
set -euo pipefail
for rid in ${RID_LIST}; do
  work="/tmp/autopilot-agent-${rid}"
  mkdir -p "${work}/obj" "${work}/bin"
  dotnet restore /src/src/AutopilotAgent/AutopilotAgent.csproj \
    -r "${rid}" \
    -p:DefaultItemExcludes="\$(DefaultItemExcludes)%3Bobj/**%3Bbin/**" \
    -p:BaseIntermediateOutputPath="${work}/obj/" \
    -p:MSBuildProjectExtensionsPath="${work}/obj/"
  dotnet publish /src/src/AutopilotAgent/AutopilotAgent.csproj \
    --no-restore \
    -c Release \
    -r "${rid}" \
    --self-contained true \
    -p:Version="${AUTOPILOT_AGENT_VERSION}" \
    -p:AutopilotAgentVersion="${AUTOPILOT_AGENT_VERSION}" \
    -p:PublishSingleFile=true \
    -p:DefaultItemExcludes="\$(DefaultItemExcludes)%3Bobj/**%3Bbin/**" \
    -p:BaseIntermediateOutputPath="${work}/obj/" \
    -p:MSBuildProjectExtensionsPath="${work}/obj/" \
    -p:BaseOutputPath="${work}/bin/" \
    -o "/out/${rid}"
done
'

CHECKSUMS="${OUTPUT_ROOT}/SHA256SUMS"
: > "${CHECKSUMS}"
while IFS= read -r -d '' file; do
  rel="${file#"${OUTPUT_ROOT}"/}"
  printf '%s  %s\n' "$(sha256_file "${file}")" "${rel}" >> "${CHECKSUMS}"
done < <(find "${OUTPUT_ROOT}" -type f ! -name manifest.json ! -name SHA256SUMS -print0 | sort -z)

python3 - "${OUTPUT_ROOT}" "${GIT_SHA}" "${GIT_DIRTY}" "${BUILD_TIME}" "${VERSION}" "${SDK_IMAGE}" "${RID_LIST}" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

output = Path(sys.argv[1])
manifest = {
    "schema_version": 1,
    "producer": "build_seed_agent_container.sh",
    "git_sha": sys.argv[2],
    "git_dirty": sys.argv[3] == "true",
    "build_time": sys.argv[4],
    "agent_version": sys.argv[5],
    "sdk_image": sys.argv[6],
    "runtime_identifiers": sys.argv[7].split(),
    "files": [],
}
for path in sorted(output.rglob("*")):
    if not path.is_file() or path.name == "manifest.json":
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["files"].append({
        "path": str(path.relative_to(output)),
        "size": path.stat().st_size,
        "sha256": digest,
    })
tmp = output / "manifest.json.tmp"
tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, output / "manifest.json")
PY

echo "[agent-seed] wrote ${OUTPUT_ROOT}"
