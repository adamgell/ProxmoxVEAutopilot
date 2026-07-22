#!/usr/bin/env bash
# Bump the unified CalVer product version across the monorepo.
#
#   scripts/bump_version.sh            # auto: YYYY.MM.<next SEQ for this month>
#   scripts/bump_version.sh 2026.08.0  # explicit
#
# Updates the repo-root VERSION file (the single source of truth) and syncs the
# normalized .NET assembly version in autopilot-agent/Directory.Build.props.
# It does NOT commit, tag, or push - see docs/RELEASING.md for the full flow.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CUR="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo "")"
if [ $# -ge 1 ]; then
  NEW="$1"
else
  YM="$(date +%Y.%m)"                       # e.g. 2026.07
  if [[ "$CUR" == "$YM".* ]]; then
    SEQ="${CUR##*.}"
    NEW="$YM.$((SEQ + 1))"
  else
    NEW="$YM.0"
  fi
fi

if ! [[ "$NEW" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]+$ ]]; then
  echo "ERROR: '$NEW' is not CalVer YYYY.MM.SEQ (e.g. 2026.07.0)" >&2
  exit 2
fi

printf '%s\n' "$NEW" > VERSION

# Normalize for .NET assembly versions: strip leading zeros per component
# (2026.07.0 -> 2026.7.0), since AssemblyVersion components must be plain ints.
IFS=. read -r A B C <<< "$NEW"
DOTNET="$((10#$A)).$((10#$B)).$((10#$C))"
props="autopilot-agent/Directory.Build.props"
perl -0pi -e "s{(<AutopilotAgentVersion[^>]*>)[^<]+(</AutopilotAgentVersion>)}{\${1}$DOTNET\${2}}g" "$props"

# Keep the committed docker-compose image-tag default in sync with the release.
# (Deploys still set AUTOPILOT_IMAGE_TAG authoritatively in the host .env; this
# is only the fallback, but a stale committed default is misleading.)
compose="autopilot-proxmox/docker-compose.yml"
perl -pi -e "s/(AUTOPILOT_IMAGE_TAG:-v)[0-9]+\.[0-9]+\.[0-9]+/\${1}$NEW/g" "$compose"

echo "VERSION: ${CUR:-<none>} -> $NEW"
echo "Agent (Directory.Build.props): $DOTNET"
echo
echo "Next:"
echo "  git add VERSION $props"
echo "  git commit -m \"release: v$NEW\""
echo "  git push origin main"
echo "  git tag v$NEW && git push origin v$NEW   # triggers the tagged image build"
echo "  scripts/deploy_production.sh v$NEW        # after CI publishes the image"
