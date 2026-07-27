#!/usr/bin/env bash
#
# Finish a Proxmox disk resize inside the Windows guest.
#
# Growing the zvol with `qm resize` is only half the job: virtio-scsi does not
# renegotiate capacity on a running guest, so Windows keeps reporting the old
# size. Neither Update-HostStorageCache, Update-Disk, nor `diskpart rescan`
# shakes it loose (all three were tried on 133/134/135/136). The disk geometry
# is only re-read at power-on, so the guest has to be cycled and the partition
# extended afterwards.
#
# Run this ON the Proxmox node that hosts the VMs.
#
#   scripts/grow_guest_disk.sh 133 134 135 136
#   scripts/grow_guest_disk.sh --dry-run 133
#
# Prefers a graceful shutdown and falls back to a hard stop for guests that are
# already wedged, which is the usual state for a VM that filled its disk.
set -euo pipefail

DRY_RUN=0
VMIDS=()
BOOT_WAIT=300     # seconds to wait for the guest agent after start
SHUTDOWN_WAIT=120 # seconds to wait for a graceful shutdown before forcing

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 1 ;;
    *) VMIDS+=("$1") ;;
  esac
  shift
done

if [ "${#VMIDS[@]}" -eq 0 ]; then
  echo "usage: $0 [--dry-run] <vmid> [vmid ...]" >&2
  exit 1
fi

command -v qm >/dev/null || { echo "qm not found: run this on the Proxmox node" >&2; exit 1; }

# PowerShell that extends C: into whatever free space the new geometry exposed.
read -r -d '' EXTEND_PS <<'PS' || true
$ErrorActionPreference = 'Stop'
$before = (Get-Volume -DriveLetter C)
$s = Get-PartitionSupportedSize -DriveLetter C
$p = Get-Partition -DriveLetter C
if ($s.SizeMax -gt ($p.Size + 1GB)) {
    Resize-Partition -DriveLetter C -Size $s.SizeMax
    $after = Get-Volume -DriveLetter C
    Write-Output ("extended {0:N1}GB -> {1:N1}GB, free {2:N1}GB" -f ($before.Size/1GB), ($after.Size/1GB), ($after.SizeRemaining/1GB))
} else {
    Write-Output ("no growth available: partition {0:N1}GB, max {1:N1}GB" -f ($p.Size/1GB), ($s.SizeMax/1GB))
}
PS

guest_free() {
  qm guest cmd "$1" get-fsinfo 2>/dev/null \
    | python3 -c "
import json,sys
try: d = json.load(sys.stdin)
except Exception: print('  (no fsinfo)'); raise SystemExit
for fs in (d if isinstance(d, list) else d.get('result', [])):
    if str(fs.get('mountpoint','')).upper().startswith('C'):
        t = fs.get('total-bytes') or 0; u = fs.get('used-bytes') or 0
        print('  C: total=%.1fGB free=%.1fGB used=%.0f%%' % (t/2**30, (t-u)/2**30, (u/t*100) if t else 0))
" 2>/dev/null || echo "  (guest agent not answering)"
}

for vmid in "${VMIDS[@]}"; do
  echo "=============================== VM ${vmid}"
  cfg_size="$(qm config "$vmid" 2>/dev/null | sed -n 's/.*size=\([0-9]*G\).*/\1/p' | head -1)"
  echo "configured disk: ${cfg_size:-unknown}"
  echo "before:"; guest_free "$vmid"

  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] would cycle the VM and extend C:"
    continue
  fi

  if [ "$(qm status "$vmid" | awk '{print $2}')" = "running" ]; then
    echo "shutting down gracefully (up to ${SHUTDOWN_WAIT}s) ..."
    qm shutdown "$vmid" --timeout "$SHUTDOWN_WAIT" >/dev/null 2>&1 || true
    for _ in $(seq 1 "$SHUTDOWN_WAIT"); do
      [ "$(qm status "$vmid" | awk '{print $2}')" = "stopped" ] && break
      sleep 1
    done
    if [ "$(qm status "$vmid" | awk '{print $2}')" != "stopped" ]; then
      # Expected for a guest that filled its disk: it can no longer write, so
      # it cannot run a clean shutdown.
      echo "graceful shutdown did not finish; forcing stop"
      qm stop "$vmid" >/dev/null
      sleep 3
    fi
  fi

  echo "starting ..."
  qm start "$vmid" >/dev/null
  echo "waiting for the guest agent (up to ${BOOT_WAIT}s) ..."
  ok=0
  for _ in $(seq 1 "$BOOT_WAIT"); do
    if qm guest cmd "$vmid" ping >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" != "1" ]; then
    echo "guest agent never came up; extend C: by hand once it boots" >&2
    continue
  fi

  echo "extending C: ..."
  # guest-exec is unreliable on a busy node, so give it a few tries.
  for attempt in 1 2 3; do
    if out="$(qm guest exec "$vmid" -- powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$EXTEND_PS" 2>&1)"; then
      echo "$out" | python3 -c "
import json,sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    print('  ' + (d.get('out-data') or d.get('err-data') or raw).strip())
except Exception:
    print('  ' + raw.strip())
"
      break
    fi
    echo "  attempt ${attempt} failed; retrying"
    sleep 5
  done

  echo "after:"; guest_free "$vmid"
done

echo
echo "Done. Agents report in on their next heartbeat (~30s)."
