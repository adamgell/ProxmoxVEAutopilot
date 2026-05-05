#!/usr/bin/env bash
# Spike: boot a Win11 ARM64 VM on macOS using stock Homebrew QEMU + swtpm,
# bypassing UTM. Goal is to reach the Windows installer language picker with
# working graphics, USB, TPM 2.0, Secure Boot, and vmnet-shared networking.
#
# Not a product. Not wired into the web UI or Ansible roles. Throwaway POC.
#
# Usage:
#   ./spike_qemu_macos_arm64.sh                    # uses defaults below
#   ./spike_qemu_macos_arm64.sh --iso PATH         # override ISO
#   ./spike_qemu_macos_arm64.sh --disk PATH        # use existing qcow2 (won't be wiped)
#   ./spike_qemu_macos_arm64.sh --no-vmnet         # use slirp instead (no sudo)
#   ./spike_qemu_macos_arm64.sh --memory 8192      # MB, default 6144
#   ./spike_qemu_macos_arm64.sh --cpus 4           # default 4
#   ./spike_qemu_macos_arm64.sh --workdir DIR      # default ~/.cache/autopilot-spike-qemu
#
# vmnet-shared requires root, so the qemu invocation is re-exec'd via sudo.
# swtpm runs as the invoking user — its socket is chmod'd so root-qemu can connect.

set -euo pipefail

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------
DEFAULT_ISO="${HOME}/UTM-ISOs/Win11_25H2_English_Arm64_v2.iso"
DEFAULT_WORKDIR="${HOME}/.cache/autopilot-spike-qemu"
DEFAULT_MEMORY=6144
DEFAULT_CPUS=4
DEFAULT_DISK_SIZE=64G
DEFAULT_VNC_DISPLAY=5   # → TCP 5905 (avoids macOS Screen Sharing on 5900)

CODE_FW="/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
# Use homebrew's matching empty VARS first to validate the boot path works.
# Win11 ARM ultimately needs Secure Boot, which means re-enrolling MS keys
# (PK/KEK/db) into this VARS — that's a follow-up if the spike progresses.
SECURE_VARS_FW="/opt/homebrew/share/qemu/edk2-arm-vars.fd"

ISO=""
DISK=""
WORKDIR="${DEFAULT_WORKDIR}"
MEMORY="${DEFAULT_MEMORY}"
CPUS="${DEFAULT_CPUS}"
USE_VMNET=1
VNC_DISPLAY=""   # empty → auto: VNC when vmnet+sudo, cocoa otherwise

# --------------------------------------------------------------------------
# Arg parsing
# --------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso)        ISO="$2"; shift 2 ;;
    --disk)       DISK="$2"; shift 2 ;;
    --workdir)    WORKDIR="$2"; shift 2 ;;
    --memory)     MEMORY="$2"; shift 2 ;;
    --cpus)       CPUS="$2"; shift 2 ;;
    --no-vmnet)   USE_VMNET=0; shift ;;
    --vnc)        VNC_DISPLAY="$2"; shift 2 ;;
    --cocoa)      VNC_DISPLAY="cocoa"; shift ;;
    -h|--help)    sed -n '2,25p' "$0"; exit 0 ;;
    *)            echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

ISO="${ISO:-$DEFAULT_ISO}"

# --------------------------------------------------------------------------
# Prereq checks
# --------------------------------------------------------------------------
log()  { printf '\033[36m[spike]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[spike]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[spike]\033[0m %s\n' "$*" >&2; exit 1; }

command -v qemu-system-aarch64 >/dev/null || die "qemu-system-aarch64 not found (brew install qemu)"
command -v swtpm                >/dev/null || die "swtpm not found (brew install swtpm)"
command -v qemu-img             >/dev/null || die "qemu-img not found"

[[ -r "$CODE_FW"        ]] || die "Missing CODE firmware: $CODE_FW"
[[ -r "$SECURE_VARS_FW" ]] || die "Missing secure-boot VARS firmware: $SECURE_VARS_FW (UTM.app must be installed for the spike)"
[[ -r "$ISO"            ]] || die "ISO not readable: $ISO"

# --------------------------------------------------------------------------
# Workdir
# --------------------------------------------------------------------------
mkdir -p "$WORKDIR" "$WORKDIR/tpm"
VARS="$WORKDIR/vars.fd"
TPM_SOCK="$WORKDIR/tpm/swtpm-sock"
QMP_SOCK="$WORKDIR/qmp.sock"
SWTPM_PIDFILE="$WORKDIR/tpm/swtpm.pid"
SWTPM_LOG="$WORKDIR/tpm/swtpm.log"

if [[ ! -s "$VARS" ]]; then
  log "Seeding VARS from secure-boot firmware: $SECURE_VARS_FW"
  cp "$SECURE_VARS_FW" "$VARS"
  chmod u+w "$VARS"
  # Stock QEMU requires the VARS pflash to match the CODE pflash size exactly.
  # UTM's secure-vars is ~333KB (UTM's qemu is patched to allow the mismatch);
  # pad it to CODE size with 0xFF so the enrolled MS secure-boot keys at the
  # start remain intact and the rest looks like erased flash.
  CODE_SIZE=$(stat -f "%z" "$CODE_FW")
  VARS_SIZE=$(stat -f "%z" "$VARS")
  if (( VARS_SIZE < CODE_SIZE )); then
    log "Padding VARS from $VARS_SIZE to $CODE_SIZE bytes with 0xFF"
    perl -e 'print "\xff" x ('"$((CODE_SIZE - VARS_SIZE))"')' >> "$VARS"
  fi
fi

if [[ -z "$DISK" ]]; then
  DISK="$WORKDIR/disk.qcow2"
  if [[ ! -s "$DISK" ]]; then
    log "Creating new $DEFAULT_DISK_SIZE qcow2 disk: $DISK"
    qemu-img create -f qcow2 "$DISK" "$DEFAULT_DISK_SIZE" >/dev/null
  fi
else
  [[ -r "$DISK" ]] || die "Disk not readable: $DISK"
fi

# --------------------------------------------------------------------------
# swtpm — TPM 2.0 emulator over UNIX socket
# --------------------------------------------------------------------------
# Always start fresh so we don't reuse half-initialised state from a prior run.
if [[ -e "$SWTPM_PIDFILE" ]]; then
  kill "$(cat "$SWTPM_PIDFILE")" 2>/dev/null || true
  rm -f "$SWTPM_PIDFILE"
fi
rm -f "$TPM_SOCK"

log "Starting swtpm on $TPM_SOCK"
# QEMU's tpm-emulator backend connects to the CTRL socket and negotiates a
# separate data fd over it via CMD_SET_DATAFD; do NOT also expose --server,
# or qemu sends ctrl commands to the wrong socket and hangs in init.
swtpm socket \
  --tpm2 \
  --tpmstate "dir=$WORKDIR/tpm" \
  --ctrl "type=unixio,path=$TPM_SOCK" \
  --flags startup-clear \
  --log "file=$SWTPM_LOG,level=20" \
  --pid "file=$SWTPM_PIDFILE" \
  --daemon

# Wait briefly for the socket to appear
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -S "$TPM_SOCK" ]] && break
  sleep 0.2
done
[[ -S "$TPM_SOCK" ]] || die "swtpm failed to create socket; see $SWTPM_LOG"
chmod 666 "$TPM_SOCK"

cleanup() {
  if [[ -s "$SWTPM_PIDFILE" ]]; then
    log "Cleaning up swtpm"
    local pid
    pid="$(cat "$SWTPM_PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# QEMU command
# --------------------------------------------------------------------------
MAC="52:54:00:$(openssl rand -hex 3 | sed 's/\(..\)/\1:/g; s/:$//')"
log "Generated MAC: $MAC"

QEMU_ARGS=(
  -name           "spike-qemu-win11-arm64"
  -machine        "virt,gic-version=3,highmem=on"
  -accel          hvf
  -cpu            host
  -smp            "$CPUS"
  -m              "$MEMORY"

  # UEFI: read-only CODE + writable secure-boot-enabled VARS
  -drive          "if=pflash,format=raw,readonly=on,file=$CODE_FW"
  -drive          "if=pflash,format=raw,file=$VARS"

  # System disk (NVMe). Win11 ARM has an inbox NVMe driver; it does NOT have
  # an inbox virtio-blk driver, so bootmgr hangs probing virtio-blk during
  # boot device enumeration. virtio-blk would require slipstreaming the
  # virtio-win driver, which the spike avoids.
  -device         nvme,drive=hd0,serial=spike-boot,bootindex=1
  -drive          "if=none,id=hd0,format=qcow2,file=$DISK,cache=writeback,discard=unmap"

  # USB controller for input. ISO is on virtio-scsi (NOT usb-storage) — stock
  # EDK2 generates a generic "USB HARDDRIVE" boot entry for usb-storage that
  # times out probing MBR/GPT and never falls through to the El-Torito ESP.
  # virtio-scsi + scsi-cd advertises a real CD-ROM, BDS auto-boots BOOTAA64.EFI.
  -device         qemu-xhci,id=usb
  -device         usb-kbd
  -device         usb-tablet
  -device         virtio-scsi-pci,id=scsi0
  -device         scsi-cd,bus=scsi0.0,drive=cdrom,bootindex=0
  -drive          "if=none,id=cdrom,media=cdrom,readonly=on,file=$ISO"

  # Graphics — ramfb (simple linear framebuffer). virtio-gpu-pci on stock
  # edk2-aarch64-code.fd hangs Windows ARM bootmgr after BdsDxe loads BOOTAA64.EFI.
  -device         ramfb

  # TPM 2.0 via swtpm
  -chardev        "socket,id=chrtpm,path=$TPM_SOCK"
  -tpmdev         emulator,id=tpm0,chardev=chrtpm
  -device         tpm-tis-device,tpmdev=tpm0

  # Audio off, RNG for entropy
  -audio          none
  -object         rng-random,id=rng0,filename=/dev/urandom
  -device         virtio-rng-pci,rng=rng0

  # QMP for state inspection (qmp-shell or socat to interact)
  -qmp            "unix:$QMP_SOCK,server=on,wait=off"
  -monitor        none
  -serial         "file:$WORKDIR/serial.log"
)

if (( USE_VMNET )); then
  QEMU_ARGS+=(
    -netdev "vmnet-shared,id=net0"
    -device "virtio-net-pci,netdev=net0,mac=$MAC"
  )
else
  QEMU_ARGS+=(
    -netdev "user,id=net0,hostfwd=tcp::2222-:22"
    -device "virtio-net-pci,netdev=net0,mac=$MAC"
  )
fi

# Resolve display: cocoa won't work under sudo (no WindowServer access),
# so default to VNC when vmnet+sudo, cocoa otherwise.
if [[ -z "$VNC_DISPLAY" ]]; then
  if (( USE_VMNET )); then VNC_DISPLAY="$DEFAULT_VNC_DISPLAY"; else VNC_DISPLAY="cocoa"; fi
fi
if [[ "$VNC_DISPLAY" == "cocoa" ]]; then
  QEMU_ARGS+=(-display "cocoa,show-cursor=on")
  log "Display:   cocoa window"
else
  QEMU_ARGS+=(-display none -vnc "127.0.0.1:${VNC_DISPLAY}")
  VNC_PORT=$((5900 + VNC_DISPLAY))
  log "Display:   VNC on 127.0.0.1:${VNC_PORT} (open with: open vnc://127.0.0.1:${VNC_PORT})"
fi

log "Workdir:   $WORKDIR"
log "Disk:      $DISK"
log "ISO:       $ISO"
log "VARS:      $VARS"
log "TPM sock:  $TPM_SOCK"
log "QMP sock:  $QMP_SOCK"
log "Serial:    $WORKDIR/serial.log"

if (( USE_VMNET )); then
  log "vmnet-shared mode → re-execing qemu under sudo (will prompt for password)"
  log "If you Ctrl-C the VM window, swtpm will be cleaned up automatically."
  exec sudo -E qemu-system-aarch64 "${QEMU_ARGS[@]}"
else
  log "slirp mode (no sudo). SSH inside guest reachable on host:2222 once OS is up."
  exec qemu-system-aarch64 "${QEMU_ARGS[@]}"
fi
