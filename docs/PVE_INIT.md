# Proxmox VE Init Reference

`autopilot-proxmox/scripts/install-proxmox-ve.sh` is the operator-facing console
installer. It presents a numbered shell UI for the core first-run path and
delegates each action to `init-proxmox-ve.sh`.

`autopilot-proxmox/scripts/init-proxmox-ve.sh` is the lower-level shell
entrypoint that runs as root on a Proxmox VE host. It is a hypervisor
bootstrapper only. The Autopilot runtime belongs in the Ubuntu controller VM.

## Console Installer

Start here for a new PVE node:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh
```

The console installer exposes:

- Guided install: Foundation -> Bootstrap -> Operational.
- Foundation only: PVE access, storage, Ubuntu controller VM, controller
  runtime health.
- Bootstrap media: Windows/VirtIO media download or manual media gate.
- Operational repair/promote: controller health and setup artifact promotion.
- Runtime config repair: token/config/media resync without rebuilding runtime.
- Dev-lab reset: disposable lab VM/media cleanup.
- Status and copy/paste one-liners.

The unattended equivalent for a lab node is:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh --action guided --yes --controller-ip 192.168.2.115
```

Use `--dry-run` to print the underlying `init-proxmox-ve.sh` commands without
changing PVE.

## What PVE Init Does

The PVE script:

- Repairs apt sources enough to install shell essentials if needed.
- Creates or repairs the `autopilot@pve!ansible` API token.
- Creates or repairs the `AutopilotProvisioner` role and ACLs.
- Enables snippet-capable storage and seeds chassis SMBIOS binaries.
- Generates runtime secrets without printing them.
- Detects node, VM storage, ISO storage, bridge, and host IP.
- Creates or discovers the Ubuntu controller VM.
- Copies source/config/secrets to the controller.
- Runs `init-controller-ubuntu.sh` inside the controller.
- Scans Windows and VirtIO media.
- Revalidates and syncs the PVE API token/config during bootstrap and
  operational reruns so resumed labs do not keep stale controller vault data.
- Creates or repairs the OSDeploy blank template VM used as the clone source
  for boot-from-ISO OSDeploy runs.
- Creates or repairs a controller-to-PVE root SSH key for host-local operations
  that Proxmox API tokens cannot perform, such as QEMU `args` writes.
- Pulls large setup ISO artifacts from the controller into PVE ISO storage when
  Proxmox API multipart upload is not reliable for the artifact size.
- Publishes sanitized setup state to the controller.
- Stops the accidental PVE-host Autopilot Docker runtime if it exists.

The PVE script does not install Docker, .NET SDK, WiX, ADK, WinPE tooling, or
Windows build tools on PVE.

## Script Options

```text
--phase foundation|bootstrap|operational|runtime-config|reset-dev-lab|all
--resume
--wait-for-media
--download-windows
--windows-iso-language <language>
--windows-iso-url <official-direct-url>
--download-virtio
--node <pve-node>
--iso-storage <storage>
--controller-ip <ip>
--controller-cidr <prefix-length>
--controller-gateway <ip>
--controller-dns <ip>
--controller-vmid <vmid>
--controller-storage <storage>
--controller-bridge <bridge>
--reset-media
--non-interactive
```

Use `--resume` by default. All phases are intended to be idempotent.

## Foundation Phase

Foundation is the main first-run phase:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive
```

Foundation:

1. Writes setup state under
   `/root/ProxmoxVEAutopilot/autopilot-proxmox/output/setup/foundation_state.json`.
2. Repairs host prerequisites and PVE API access.
3. Creates a migration bundle if an accidental PVE-host runtime exists.
4. Creates or discovers the Ubuntu controller VM.
5. Waits for controller SSH by static IP, QGA, or ARP.
6. Syncs the repo to `/opt/ProxmoxVEAutopilot` on the controller.
7. Runs controller bootstrap.
8. Verifies controller `/healthz`.
9. Stops the PVE-host Autopilot Docker stack.

Default controller VM:

```text
Name: autopilot-controller-01
OS: Ubuntu Server 24.04 LTS cloud image
CPU: 4 vCPU
Memory: 8192 MB
Disk: 128 GB
Machine: q35
Firmware: UEFI/OVMF
QGA: enabled
Network: virtio on detected bridge
```

## Bootstrap Phase

Bootstrap is the media/build-host readiness phase:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-windows --download-virtio --controller-ip 192.168.2.115 --non-interactive
```

Bootstrap:

- Revalidates the Proxmox API token from `vault.yml`, rotates it if the PVE API
  rejects it, repairs role/ACLs, and syncs the repaired runtime config to the
  controller.
- Downloads VirtIO media when `--download-virtio` is passed.
- Downloads Windows media from Microsoft's official software download connector
  when `--download-windows` is passed. The default language is `English`; use
  `--windows-iso-language` to select a different published language.
- Downloads Windows ISO from an operator-supplied official direct URL when
  `--windows-iso-url` is passed.
- Scans ISO storage for Windows and VirtIO media.
- Publishes media readiness to the controller.
- Detects an existing build-host VM and seed ISO.

Use `--wait-for-media` for an interactive pause when Windows media is missing.
Use `--non-interactive` for CI/lab automation where missing media should return
a non-zero exit instead of waiting.

## Operational Phase

Operational repairs the PVE access contract, verifies the controller,
publishes large setup artifacts, and republishes state:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase operational --resume --controller-ip 192.168.2.115 --non-interactive
```

This phase is safe to rerun after media uploads, build-host repairs, token
rotation, or artifact promotion. It does not install Docker or build tooling on
PVE.

Hard lab fixes are tracked in [FIRST_RUN_HARD_FIXES.md](FIRST_RUN_HARD_FIXES.md).

## State File

PVE writes local state here:

```text
/root/ProxmoxVEAutopilot/autopilot-proxmox/output/setup/foundation_state.json
```

The controller receives a sanitized merge at:

```text
/opt/ProxmoxVEAutopilot/autopilot-proxmox/output/setup/foundation_state.json
```

Important state keys:

| Key | Meaning |
| --- | --- |
| `pve_node` | Proxmox node selected for VM operations |
| `pve_host_ip` | LAN IP the controller should use for Proxmox API |
| `osdeploy_blank_template_vmid` | Blank template VMID created or reused for OSDeploy |
| `cloudosd_blank_template_vmid` | Blank template VMID created or reused for CloudOSD |
| `pve_root_ssh_key_ready` | Root SSH key for controller-to-PVE host-local operations is installed |
| `controller_vmid` | Ubuntu controller VMID |
| `controller_ip` | Controller LAN IP |
| `controller_url` | UI/API URL, normally `http://<controller-ip>:5000` |
| `windows_iso_ready` | Windows ISO was found in PVE ISO storage |
| `windows_iso_volid` | Proxmox volid for Windows media |
| `virtio_iso_ready` | VirtIO ISO was found in PVE ISO storage |
| `virtio_iso_volid` | Proxmox volid for VirtIO media |
| `build_host_vmid` | Existing or created Windows build-host VM |
| `seed_iso_volid` | Build-host seed ISO volid |
| `build_host_expected_agent_id` | Expected agent identity, for example `buildhost-100` |

Secrets are stored in `.env`, `secrets/`, and `inventory/group_vars/all/vault.yml`.
Do not print or commit them.

## Controller Sync

PVE syncs source to:

```text
/opt/ProxmoxVEAutopilot
```

The sync excludes runtime output:

- `.venv/`
- `__pycache__/`
- `.pytest_cache/`
- `bin/`
- `obj/`
- `autopilot-proxmox/output/`

On the operator workstation, use the same protection when copying to PVE:

```bash
rsync -a --delete \
  --exclude 'autopilot-proxmox/.env' \
  --exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml' \
  --exclude 'autopilot-proxmox/secrets/' \
  --exclude 'autopilot-proxmox/output/' \
  '/Users/Adam.Gell/repo/ProxmoxVEAutopilot/' \
  pve-dev-192-168-2-252:/root/ProxmoxVEAutopilot/
```

## Recovery

Verify PVE does not run the Autopilot stack:

```bash
docker ps --format "{{.Names}} {{.Status}}" 2>/dev/null | grep -i autopilot || true
```

Verify controller VM:

```bash
qm status 100
qm guest cmd 100 ping
```

Verify controller health from PVE:

```bash
curl -fsS http://192.168.2.115:5000/healthz
```

Verify controller build metadata:

```bash
curl -fsS http://192.168.2.115:5000/api/version
```

If the version reports `unknown`, rerun foundation so `init-controller-ubuntu.sh`
rebuilds the controller image with `GIT_SHA` and `BUILD_TIME` build args:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive
```

Re-run foundation after source changes:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive
```

Re-run media scan:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-windows --download-virtio --controller-ip 192.168.2.115 --non-interactive
```

Re-publish operational state:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase operational --resume --controller-ip 192.168.2.115 --non-interactive
```

Clean a disposable dev lab before replaying first-run:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase reset-dev-lab --reset-media --non-interactive
```

Replay the full first-run path with automated official media handling:

```bash
bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase all --resume --download-windows --download-virtio --non-interactive
```

The reset phase destroys only Autopilot dev-lab VM names and prefixes:
`autopilot-controller-01`, `autopilot-buildhost-01`,
`autopilot-osdeploy-blank-template`, `autopilot-cloudosd-blank-template`,
`OSDEPLOY-E2E-*`, `CLOUDOSD-E2E-*`, `AUTOPILOT-E2E-*`, generated OSDeploy
batch names `OSD[0-9]*`, generated OSDCloud batch names `CSD[0-9]*`, and
single-VM acceptance names `APE2E[0-9]*`. With `--reset-media`, it also removes
generated/downloaded lab ISOs such as Windows evaluation media,
`virtio-win*.iso`, build-host seed ISOs, WinPE ISOs, CloudOSD ISOs, and
OSDeploy ISOs from ISO-capable storage so `--download-windows` and
`--download-virtio` are exercised on the next bootstrap run.

## Latest Dev-Lab Checkpoint

The latest pvetest handoff on 2026-05-18 is a clean post-reset state, ready for
the next guided install replay:

```text
PVE node: pvetest, 192.168.2.252
Reset state file: /root/ProxmoxVEAutopilot/autopilot-proxmox/output/setup/foundation_state.json
State phase: reset-dev-lab
dev_lab_reset_ready: true
pve_host_clean_ready: true
```

Verified after teardown:

- `qm list` showed no remaining VMs.
- The Ubuntu controller at `192.168.2.127` was unreachable, as expected after
  destroying `autopilot-controller-01`.
- No Autopilot Docker containers were running on the PVE host.
- `/var/lib/vz/template/iso` had no remaining ISO files.
- Runtime secrets/state were removed and the reset state file was recreated.

Where testing left off:

1. Replay the console installer or `--phase all` path from this reset state.
2. Re-exercise automated official Windows and VirtIO media downloads.
3. Recreate the Ubuntu controller VM and verify build metadata is not
   `unknown`.
4. Recreate/repair the Windows build host and run source-build workloads.
5. Promote WinPE, CloudOSD, and OSDeploy artifacts into PVE storage.
6. Launch one real provision workflow and confirm `/setup` operational plus
   `/monitoring` active/stuck counts are clean.

## Rollback Boundary

The first-run migration path preserves PVE-host Docker volumes and migration
bundles. It stops the accidental PVE-host runtime after the controller is
healthy; it does not remove PVE Docker packages or delete volumes in v1.

If controller bootstrap fails, fix the cause and rerun foundation with
`--resume`. Do not reset the PVE node unless the operator intentionally wants a
clean lab rebuild.
