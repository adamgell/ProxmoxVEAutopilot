# QEMU-direct on macOS — Win11 ARM64 Spike

**Status:** POC / spike. Not wired into the web UI, jobs, or any Ansible role.
**Branch:** `spike/qemu-macos-arm64-poc`
**Deliverable:** `autopilot-proxmox/scripts/spike_qemu_macos_arm64.sh`

## What this proves (and doesn't)

This spike answers one question: **can we replace UTM with stock Homebrew QEMU on macOS for the Win11 ARM provisioning path?**

Out of scope: Ansible role refactors, `web/utm_*.py` replacement, sysprep flow,
hash capture, snapshots, multi-VM orchestration, IP discovery.

## Why bypass UTM?

UTM ultimately wraps QEMU. Today on macOS we carry ~2,600 LOC of UTM
glue:

| Surface | LOC |
| --- | --- |
| `web/utm_bundle.py` | 593 |
| `web/utm_cli.py` | 134 |
| `web/utm_host_metrics.py` | 247 |
| `web/utm_snapshots.py` | 289 |
| `web/utm_vm_metrics.py` | 109 |
| 6× `roles/utm_*` Ansible roles | ~870 |
| `playbooks/utm_*.yml` | ~400 |
| `app.py` UTM references | ~199 |

Plus `utm_schema_known_good.txt` and a contract test against a versioned
config.plist schema we don't control.

If a QEMU-direct path is viable, we shed all of that and drop a hard
dependency on a third-party signed app on contributor machines.

## Validated by this spike

Re-running `./spike_qemu_macos_arm64.sh --no-vmnet` cleanly:

- ✅ `qemu-system-aarch64` 11.0.0 (Homebrew) accepts the full machine spec:
  `virt,gic-version=3,highmem=on,virtualization=on` + `-accel hvf` + `-cpu host`
- ✅ Secure-Boot-enabled UEFI VARS seeded from
  `/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd`
  (we still need UTM.app installed *just for this firmware blob* — see
  follow-up below)
- ✅ `swtpm` 0.10.1 starts as a daemon, exposes a UNIX socket, and self-tests
  TPM 2.0 successfully (`startup-clear` + RSA-2048/camellia/tdes self-tests pass)
- ✅ QEMU connects to swtpm via `tpm-tis-device` and a chardev socket
- ✅ QMP socket created at `<workdir>/qmp.sock` for state inspection
- ✅ Win11 ARM ISO attached as USB (`usb-storage`, not virtio-cd —
  virtio-cd is **not** enumerated by Win11 ARM setup, this is a known gotcha)
- ✅ vmnet-shared netdev is present in Homebrew qemu (`-netdev help` lists
  `vmnet-host`, `vmnet-shared`, `vmnet-bridged`); confirmed compiled in,
  not just declared

What this spike does **not** yet prove (needs human-in-the-loop):

- ❓ Win11 setup actually reaches the language picker
- ❓ TPM 2.0 check inside Win11 setup passes (no "this PC can't run Windows 11")
- ❓ Secure Boot is reported as ON in firmware menu / Win11 setup
- ❓ vmnet-shared brings the VM up on the LAN with a working DHCP lease
  (sudo required, can't be exercised from CI / this agent context)
- ❓ Mouse / keyboard input via cocoa display is responsive

## How to run the spike

### Prereqs (one-time)

```bash
brew install qemu swtpm
# UTM.app must be installed for /Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd
```

Place a Win11 ARM64 ISO at `~/UTM-ISOs/Win11_25H2_English_Arm64_v2.iso`
(or pass `--iso PATH`).

### Default run (vmnet-shared, prompts for sudo)

```bash
cd autopilot-proxmox
./scripts/spike_qemu_macos_arm64.sh
```

You'll be asked for your sudo password (vmnet-shared requires root). A cocoa
window opens, EDK2 firmware runs, the boot manager hands off to the Win11
installer.

### Slirp run (no sudo, but no LAN access)

```bash
./scripts/spike_qemu_macos_arm64.sh --no-vmnet
```

Slirp gives the guest NAT'd internet access but the VM is unreachable from
the host LAN. SSH inside the guest is reachable on `localhost:2222` once
the OS is up.

### Custom inputs

```bash
./scripts/spike_qemu_macos_arm64.sh \
  --iso ~/path/to/win11arm.iso \
  --disk ~/path/to/existing.qcow2 \
  --memory 8192 \
  --cpus 6 \
  --workdir ~/my-spike
```

`--disk` lets you point at an existing qcow2 (e.g. one borrowed from
`~/Library/Containers/com.utmapp.UTM/Data/Documents/Windows.utm/Data/*.qcow2`)
to skip Win11 setup and validate boot of an already-installed image.

## Manual verification checklist

After launching the script, walk through this list and record results in
this doc (or a follow-up).

- [ ] **EDK2 → boot manager** — UEFI logo appears, then Windows boot manager
- [ ] **Language picker** — Win11 ARM setup reaches "Choose your language"
- [ ] **Mouse + keyboard** — pointer tracks, typing in a text field works
- [ ] **Secure Boot ON** — at the EDK2 boot menu (Esc during firmware), open
  *Device Manager → Secure Boot Configuration* and confirm `Attempt Secure Boot = ON`
- [ ] **TPM 2.0 detected** — Win11 setup proceeds past the "this PC can't run
  Windows 11" gate (drop to Shift-F10 cmd → `tpm.msc` to confirm 2.0 manufacturer = swtpm)
- [ ] **Network tile present** — Win11 setup shows network interfaces; with
  vmnet-shared the VM should pull a DHCP lease on the host's vmnet-shared subnet
  (typically `192.168.64.0/24` on Apple Silicon)
- [ ] **QMP responsive** — from another terminal:
  ```bash
  echo '{"execute":"qmp_capabilities"}{"execute":"query-status"}' \
    | nc -U ~/.cache/autopilot-spike-qemu/qmp.sock
  ```

## Known gotchas / non-obvious choices in the script

### ISO must be USB-attached, not virtio-cd

Windows 11 ARM setup does not enumerate `-device virtio-scsi-pci` or
`-cdrom` devices. The installer hangs at "no media" without complaint.
The fix is `-device usb-storage,drive=cdrom`. UTM does the same thing
internally.

### Machine flags

- `gic-version=3` is required by Win11 ARM (it refuses to boot on GIC v2).
- `highmem=on` lets us allocate >3 GiB of RAM safely.
- `virtualization=on` exposes EL2 to the guest (some Win11 features check
  for it). Costs nothing on Apple Silicon hosts.
- `iommu=smmuv3` is **omitted** — it's incompatible with `-accel hvf` on
  Apple Silicon and `-cpu host`.

### Secure Boot VARS

`/opt/homebrew/share/qemu/edk2-arm-vars.fd` is the **non-secure** VARS
template; using it leaves the guest in setup mode with no PK enrolled,
which means Win11's Secure Boot check fails. UTM ships a pre-enrolled
secure-vars at `/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd`
and we copy that as our writable VARS file.

> Follow-up gap: if we ever ship this in product, we'd need our own
> secure-vars blob (built with `virt-fw-vars` enrolling MS PK + KEK + db,
> the same way `web/utm_bundle.py:prepare_efi_vars` already does for UTM
> bundles).

### swtpm socket vs CUSE

We use the UNIX socket interface (`--server type=unixio,path=...`)
because CUSE on macOS needs a kernel module we don't want to install.
Socket-mode swtpm + qemu's `chardev socket` + `tpm-tis-device` is the
fully userspace path.

### vmnet under sudo

vmnet-shared opens `/dev/vmnet*` which requires root. There is no way
around this on stock macOS without the `com.apple.vm.networking`
entitlement (Apple-restricted, can't be self-signed onto qemu — same
blocker we hit on the UTM-fork attempt). The script `exec`s `sudo` for
the qemu invocation only; swtpm continues running as the invoking user
and is reachable via the chmod'd socket.

### MAC address

We mint a fresh `52:54:00:xx:xx:xx` MAC on each run. For a real product
path we'd persist it per-VM (UTM does this in `config.plist`, we'd do it
per-spike-workdir or per-VM in a sidecar metadata file).

## Tradeoffs vs UTM (for future planning)

| Concern | UTM | QEMU-direct |
| --- | --- | --- |
| **macOS networking on LAN** | Built-in, signed entitlement | Requires sudo (vmnet under root) |
| **Bundle format** | `.utm` dir + plist; we marshal to/from | None — flat workdir we control |
| **VM lifecycle** | `utmctl list/start/stop/ip-address` | We own state via QMP + pidfiles |
| **IP discovery** | `utmctl ip-address` (uses UTM's lease tracking) | Parse `/var/db/dhcpd_leases` or use guest agent |
| **Snapshots** | UTM API + plist mutation | `qemu-img snapshot` + QMP `snapshot-save` |
| **TPM** | UTM bundles swtpm | We invoke swtpm ourselves |
| **Secure Boot VARS** | Bundled signed blob | Need our own (or borrow UTM's, as this spike does) |
| **GUI display** | UTM provides + handles input redirection | qemu cocoa display |
| **Install footprint** | UTM.app (~700 MB) | qemu + swtpm via brew (~200 MB) |
| **Schema drift risk** | High (config.plist v4, may change in UTM updates) | None — we own everything |
| **Code we own** | ~2,600 LOC of glue | Just the qemu argv builder |

## Follow-ups if we ever productize this

1. **Replace `utmctl` with QMP client** — `web/utm_cli.py` becomes
   `web/qemu_qmp.py`. List = enumerate `*.workdir/qemu.pid`. Start = spawn
   qemu. Stop = QMP `quit`. IP = parse vmnet leases or use guest-agent.
2. **Disk + TPM + UEFI workdir layout** — codify the `~/.cache/autopilot/<vm-id>/`
   directory structure (disk.qcow2, vars.fd, tpm/, qmp.sock, qemu.pid).
3. **Build our own secure-boot VARS blob** — port
   `web/utm_bundle.py:prepare_efi_vars` to operate on
   `edk2-aarch64-vars.fd` instead of UTM's pre-enrolled file. Removes
   the UTM.app install dependency.
4. **Add `hypervisor_type=qemu`** as the third backend, keeping
   `proxmox` and `utm` working unchanged. The dispatch fragments in
   `roles/common/tasks/_*_guest_exec.yml` already follow the pattern.
5. **Sudo-less vmnet** — investigate whether splitting into a tiny
   privileged helper daemon (signed once at install) holding the
   `/dev/vmnet` fd + passing it to qemu via SCM_RIGHTS lets us drop
   the per-launch sudo prompt. (Out of scope until 1–4 ship.)
6. **Linux fallback** — qemu-direct-on-Linux is trivial (kvm + tap +
   no entitlement weirdness), so productizing this also unlocks running
   the macOS-side flow on Linux contributor machines.

## Files in this spike

- `autopilot-proxmox/scripts/spike_qemu_macos_arm64.sh` — the launcher
- `autopilot-proxmox/docs/QEMU_MACOS_SPIKE.md` — this file (lives in
  `docs/QEMU_MACOS_SPIKE.md` if the per-package docs dir is later
  consolidated)

## What's not in the repo

- The Win11 ARM ISO (~5 GB) — operator-provided
- swtpm state (regenerated per run by default)
- The UEFI VARS file (copied from UTM.app at first run)
