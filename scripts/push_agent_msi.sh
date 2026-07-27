#!/usr/bin/env bash
#
# Push the published AutopilotAgent MSI into running guests via the Proxmox
# guest agent, instead of waiting for the agent to upgrade itself.
#
# Why this exists
# ---------------
# The agent is supposed to self-upgrade: Worker.cs calls
# AgentUpdateService.CheckAndApplyOnceAsync after every heartbeat, and the
# server answers /api/agent/v1/update-check with "upgrade_available" whenever
# the published MSI is newer than the reported installed version.
#
# On the ring0ivy24 fleet that loop never fires. Those agents heartbeat every
# 30s and have logged 11836 lines with zero "MSI update completed" and zero
# "Agent update check failed", which means CheckAndApplyOnceAsync is returning
# at its first branch: the agent is being told it is current. The server
# computes upgrade_available for the same agent when asked directly, so the
# disagreement is in what the agent reports as installed_version (it sends
# Assembly.GetName().Version, which the endpoint prefers over the
# heartbeat-recorded agent_version).
#
# Until that is fixed, restarting the service does nothing: the agent restarts
# and is told it is current again. Pushing the MSI is the only thing that
# actually moves the version.
#
# Usage
# -----
#   scripts/push_agent_msi.sh --dry-run              # show what would be done
#   scripts/push_agent_msi.sh 108                    # one VM
#   scripts/push_agent_msi.sh 108 141 138 140        # several
#   scripts/push_agent_msi.sh --stale                # every agent below the published version
#
# Runs against the controller over ssh and drives guest-exec from inside the
# autopilot container, which already holds the Proxmox credentials.
set -euo pipefail

HOST="${AUTOPILOT_HOST:-192.168.2.4}"
SSH_USER="${AUTOPILOT_SSH_USER:-root}"
SSH_OPTS=(-o ConnectTimeout=15 -o StrictHostKeyChecking=no)
DRY_RUN=0
PICK_STALE=0
VMIDS=()

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --stale)   PICK_STALE=1 ;;
    -h|--help) usage 0 ;;
    -*)        echo "unknown flag: $1" >&2; usage 1 ;;
    *)         VMIDS+=("$1") ;;
  esac
  shift
done

remote() { ssh "${SSH_OPTS[@]}" "${SSH_USER}@${HOST}" "$@"; }

published_version() {
  remote "docker exec autopilot python3 -c \"
from web import setup_artifacts
r = setup_artifacts.latest_agent_release(runtime_identifier='win-x64') or {}
print(r.get('version') or '')
\"" 2>/dev/null | tr -d '\r' | tail -1
}

# Agents whose recorded version is not the published one, that have a VMID and
# have heartbeated recently enough to still be reachable.
stale_vmids() {
  local pub="$1"
  remote "docker exec autopilot-postgres psql -U autopilot -d autopilot -t -A -c \"
    select vmid from agent_devices
    where vmid is not null
      and agent_version is not null
      and agent_version not like '${pub}%'
      and last_seen_at > now() - interval '10 minutes'
    order by vmid;
  \"" 2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$' || true
}

PUBLISHED="$(published_version)"
if [ -z "$PUBLISHED" ]; then
  echo "==> could not read the published agent version; is the controller up?" >&2
  exit 1
fi
echo "==> published agent MSI: ${PUBLISHED}"

if [ "$PICK_STALE" = "1" ]; then
  # portable read loop: macOS ships bash 3.2, which has no mapfile
  while IFS= read -r line; do
    [ -n "$line" ] && VMIDS+=("$line")
  done < <(stale_vmids "$PUBLISHED")
fi

# de-duplicate and drop blanks
_clean=()
while IFS= read -r line; do
  [ -n "$line" ] && _clean+=("$line")
done < <(printf '%s\n' "${VMIDS[@]:-}" | grep -E '^[0-9]+$' | sort -un || true)
VMIDS=("${_clean[@]:-}")

if [ "${#VMIDS[@]}" -eq 0 ]; then
  echo "==> nothing to do (pass VMIDs, or --stale to discover them)"
  exit 0
fi

echo "==> targets: ${VMIDS[*]}"
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] would download the MSI inside each guest and run msiexec /qn /norestart"
  exit 0
fi

failed=0
for vmid in "${VMIDS[@]}"; do
  echo "==> VM ${vmid}: installing ${PUBLISHED} ..."
  # guest-exec can time out on a busy node, so each VM gets a few attempts.
  if remote "docker exec autopilot python3 -c \"
import sys
import web.app as a

vmid = ${vmid}
node = a._resolve_vm_node(vmid)
if not node:
    print('no cluster node hosts this VM'); sys.exit(1)

ps = r'''
\\\$ErrorActionPreference = 'Stop'
\\\$dst = Join-Path \\\$env:TEMP 'AutopilotAgent-push.msi'
Invoke-WebRequest -Uri 'http://${HOST}:5000/api/cloudosd/assets/autopilotagent.msi' -OutFile \\\$dst -UseBasicParsing -TimeoutSec 600
\\\$p = Start-Process msiexec.exe -ArgumentList @('/i', \\\$dst, '/qn', '/norestart') -Wait -PassThru
Write-Output (\\\"exit=\\\" + \\\$p.ExitCode)
if (\\\$p.ExitCode -ne 0 -and \\\$p.ExitCode -ne 3010) { exit \\\$p.ExitCode }
'''

last = ''
for attempt in range(3):
    r = a._guest_exec_ps_status(node, vmid, ps, timeout_s=600)
    if r.get('ok'):
        print((r.get('out') or '').strip() or 'installed')
        sys.exit(0)
    last = str(r.get('error'))[:160]
    print('attempt %d failed: %s' % (attempt, last))
sys.exit(1)
\"" 2>&1 | grep -vE 'Warning|authlib|^\s*$'; then
    echo "==> VM ${vmid}: ok"
  else
    echo "==> VM ${vmid}: FAILED" >&2
    failed=$((failed + 1))
  fi
done

echo
if [ "$failed" -gt 0 ]; then
  echo "==> ${failed} of ${#VMIDS[@]} failed. Agents report their new version on the next heartbeat (~30s)."
  exit 1
fi
echo "==> all ${#VMIDS[@]} installed. Agents report the new version on the next heartbeat (~30s)."
