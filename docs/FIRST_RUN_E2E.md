# First-Run E2E Runbook

This runbook is the primary operator path for a new Proxmox VE node. The goal is
to move from a clean hypervisor to an operational ProxmoxVEAutopilot controller
without installing Docker, .NET, WiX, ADK, WinPE tooling, or Windows build tools
directly on the Proxmox VE host.

The path is:

```text
Mac or admin workstation
  -> copy source to PVE
  -> run init-proxmox-ve.sh on PVE
  -> PVE creates Ubuntu controller VM
  -> controller runs Docker Compose and /setup
  -> controller creates/repairs Windows build host
  -> build host source-builds MSI, WinPE, and CloudOSD artifacts
  -> controller promotes artifacts to PVE ISO storage
  -> operator provisions a real Windows VM
```

## Roles

| Role | Runs where | Owns |
| --- | --- | --- |
| Proxmox VE host | PVE shell | API token, ACLs, media scan, controller VM, storage upload target |
| Ubuntu controller | dedicated Ubuntu Server VM | Docker, Compose, web UI, MCP, Postgres, monitor, builder, setup state |
| Windows build host | dedicated Windows 11 VM | ADK, WinPE add-on, .NET SDK, WiX, MSI/WinPE/CloudOSD builds |
| Operator workstation | Mac/Linux/Windows shell | first copy, SSH entrypoint, browser access |

## Prerequisites

- Root SSH to the Proxmox VE node.
- A Proxmox VE node with UEFI/OVMF, TPM, and an ISO-capable storage.
- A Windows 11 ISO uploaded manually or downloaded from an operator-supplied
  official Microsoft direct URL.
- Network access from the controller and Windows build host to Microsoft,
  Ubuntu, NuGet, PowerShell Gallery, and the Proxmox API.

The first supported controller target is Ubuntu Server 24.04 LTS cloud image.
The first supported build host target is Windows 11 x64.

## One Operator Flow

From the operator workstation, copy the repo to the PVE node:

```bash
rsync -a --delete \
  --exclude 'autopilot-proxmox/.env' \
  --exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml' \
  --exclude 'autopilot-proxmox/secrets/' \
  --exclude 'autopilot-proxmox/output/' \
  '/Users/Adam.Gell/repo/ProxmoxVEAutopilot/' \
  pve-dev-192-168-2-252:/root/ProxmoxVEAutopilot/
```

For disposable dev hardware, reset generated lab state before replaying the
path from zero. This removes only Autopilot dev-lab VM names/prefixes and, with
`--reset-media`, generated/downloaded lab media so the bootstrap media
automation is tested again.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase reset-dev-lab --reset-media --non-interactive'
```

Run PVE foundation. This repairs the PVE API surface, creates or discovers the
Ubuntu controller VM, syncs source/config to it, builds the local controller
image, and verifies `/healthz`.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive'
```

Run bootstrap with assisted official media downloads. `--download-windows`
resolves a fresh 24-hour Windows 11 ISO URL through Microsoft's official
software download connector, and `--download-virtio` fetches VirtIO from the
official virtio-win source.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-windows --download-virtio --controller-ip 192.168.2.115 --non-interactive'
```

Run operational after the controller and artifacts are ready. This republishes
PVE state to the controller, verifies the controller health endpoint, repairs
PVE API token/config drift, and pulls large promoted setup ISOs through the
PVE-side transfer path when needed.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase operational --resume --controller-ip 192.168.2.115 --non-interactive'
```

Open the controller:

```text
http://192.168.2.115:5000/setup
```

Use the local first-run auth button when Entra is not configured yet.

Hard fixes discovered during lab rebuilds are tracked in
[FIRST_RUN_HARD_FIXES.md](FIRST_RUN_HARD_FIXES.md). Each entry should describe
the root cause, the script-level fix, and the regression guard.

## Media Gate

The PVE init script scans ISO-capable storage and publishes media readiness to
`/setup`. Manual upload is always supported.

Default dev lab upload directory:

```text
/var/lib/vz/template/iso
```

Expected PVE volids after media is ready:

```text
local:iso/Win11_25H2_English_x64_v2.iso
local:iso/virtio-win.iso
```

Manual upload is still supported. When Microsoft's connector rate-limits a lab
IP, the operator can also paste an official direct Microsoft URL:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --windows-iso-url "https://download.microsoft.com/..." --controller-ip 192.168.2.115 --non-interactive'
```

## Controller Acceptance

The controller is ready when these are true:

- `GET /healthz` returns healthy.
- `GET /api/version` returns the local source build SHA/build time instead of
  `unknown`.
- `/setup` shows phase `operational` and health `ready`.
- `/setup` shows local auth active unless Entra is configured.
- Docker Compose is running only inside the Ubuntu controller, not on PVE.
- `autopilot`, `autopilot-mcp`, `autopilot-postgres`, `autopilot-monitor`, and
  at least one `autopilot-builder` replica are healthy/running.

Controller health check:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/setup' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/setup/v1/readiness'; rm -f "$tmp"
```

PVE runtime cleanliness check:

```bash
ssh pve-dev-192-168-2-252 'docker ps --format "{{.Names}} {{.Status}}" 2>/dev/null | grep -i autopilot || true'
```

No output means the accidental PVE-host runtime is not running.

## Build-Host Flow

The controller owns build-host lifecycle through `/setup` and
`/api/setup/v1/*`. The expected dev identity is:

```text
VMID: 100
VM name: autopilot-buildhost-01
Computer name: AUTOPILOT-BLD
Agent ID: buildhost-100
```

Repair or reseed the agent when the VM exists but the agent token/config is
stale:

```bash
curl -fsS -X POST 'http://192.168.2.115:5000/api/setup/v1/build-host/repair-agent' \
  -H 'Content-Type: application/json' \
  -d '{"vmid":"100","agent_id":"buildhost-100","computer_name":"AUTOPILOT-BLD","auto_approve":true}'
```

Queue source-build workloads:

```bash
curl -fsS -X POST 'http://192.168.2.115:5000/api/setup/v1/build-host/workloads' \
  -H 'Content-Type: application/json' \
  -d '{"force":true,"kinds":["fetch_source_bundle","build_agent_msi","build_winpe","build_cloudosd","publish_artifacts"]}'
```

The build host must not consume project prebuilt binaries. It downloads the
exact source bundle from the controller and produces:

- `AutopilotAgent-<version>-win-x64.msi`
- `AutopilotAgent-<version>-win-arm64.msi`
- `winpe-autopilot-amd64-<build_sha>.iso`
- `winpe-autopilot-amd64-<build_sha>.wim`
- `cloudosd-autopilot-amd64-<build_sha>.iso`
- `cloudosd-autopilot-amd64-<build_sha>.wim`
- JSON manifests and SHA-256 metadata

## Operational Acceptance

`/setup` can move to operational only after:

- PVE foundation is ready.
- Controller runtime is ready.
- Windows and VirtIO media are ready.
- Build-host agent is fresh and approved.
- At least one setup-produced agent MSI exists.
- At least one setup-produced WinPE or CloudOSD ISO exists.
- Setup-produced ISO artifacts are promoted to PVE ISO storage.

Promoted ISO examples:

```text
local:iso/winpe-autopilot-amd64-401fe155b3d54fb3.iso
local:iso/cloudosd-autopilot-amd64-b0f58d98948ff451.iso
```

## CloudOSD Blank Template

CloudOSD v2 provisions from a blank template VM. Until this is fully folded into
the PVE init script, create the template once on a new dev node:

```bash
ssh pve-dev-192-168-2-252 'qm status 9000 >/dev/null 2>&1 || qm create 9000 --name autopilot-cloudosd-blank-template --memory 4096 --cores 2 --cpu host --machine q35 --bios ovmf --ostype win11 --scsihw virtio-scsi-single --net0 virtio,bridge=vmbr0 --agent enabled=1 --efidisk0 local-zfs:1,efitype=4m,pre-enrolled-keys=1 --tpmstate0 local-zfs:4,version=v2.0 --scsi0 local-zfs:64,discard=on,iothread=1,ssd=1 --boot order=scsi0 && qm template 9000'
```

Publish the VMID to the controller/PVE inventory:

```bash
ssh pve-dev-192-168-2-252 'grep -q "^cloudosd_blank_template_vmid:" /root/ProxmoxVEAutopilot/autopilot-proxmox/inventory/group_vars/all/vars.yml && sed -i "s/^cloudosd_blank_template_vmid:.*/cloudosd_blank_template_vmid: 9000/" /root/ProxmoxVEAutopilot/autopilot-proxmox/inventory/group_vars/all/vars.yml || printf "\ncloudosd_blank_template_vmid: 9000\n" >> /root/ProxmoxVEAutopilot/autopilot-proxmox/inventory/group_vars/all/vars.yml'
```

## Provision Acceptance

A real provision is accepted when the run has:

- Proxmox VM identity and VMID.
- PE registration.
- OSDCloud start/end.
- Offline validation.
- Windows first boot.
- AutopilotAgent heartbeat from the installed OS.
- Hardware hash capture.
- Hash upload or a clear, expected upload blocker.
- QGA when the Windows guest agent is installed and running.

Hash capture alone is not enough for a healthy deployment; final health should
use the strongest available evidence for the path.

## Monitoring Checks

Deployment timing is visible through `/monitoring` and these APIs:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/monitoring' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/monitoring/deployments/summary'; rm -f "$tmp"
```

Recent normalized runs:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/monitoring' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/monitoring/deployments/runs?limit=12'; rm -f "$tmp"
```

Look for phase-level timings across jobs, task-engine runs, CloudOSD,
build-host work, and agent work.

## Dev Lab Proof Record

The 2026-05-16 dev PVE proof used:

```text
PVE node: pvetest, 192.168.2.252
Controller VM: 101, autopilot-controller-01, 192.168.2.115
Build host VM: 100, autopilot-buildhost-01, AUTOPILOT-BLD
Accepted test VM: 1201, AP-E2E-QGA-001
Accepted CloudOSD run: 1992fdca-e12f-4577-858f-11da11bdc03f
Provision job: 20260516-66e2, exit code 0
CloudOSD artifact: 73256f75-b669-45c2-aba4-a787ddfde8bc
Promoted CloudOSD ISO: local:iso/cloudosd-autopilot-amd64-30a320699379dbc8.iso
Promoted WinPE ISO: local:iso/winpe-autopilot-amd64-401fe155b3d54fb3.iso
```

Observed successful evidence:

- `/setup` phase `operational`, health `ready`.
- PVE had no running Autopilot Docker containers.
- Build-host agent `buildhost-100` had a fresh heartbeat.
- Build host produced agent MSI, WinPE ISO/WIM, CloudOSD ISO/WIM, manifests,
  SHA-256s, source commit, dirty-state, RID/arch, and producer metadata.
- CloudOSD PE registered, started OSDCloud, completed Microsoft ESD download,
  verified SHA, completed offline validation, and signaled PE completion.
- Installed Windows first boot completed.
- QEMU Guest Agent responded through Proxmox:
  `qm guest cmd 1201 ping` and `/nodes/pvetest/qemu/1201/agent/ping`.
- AutopilotAgent heartbeat was visible from the installed OS.
- Hardware hash was captured:
  `autopilotagent_v2_hash_capture_complete` for `agent-ap-e2e-qga-001`.
- The earlier VMID 1200 pass also completed and captured:
  `20260516T040300Z-vm1200-AP-E2E-001-osd-v2_hwid.csv`.

Known external-readiness gaps from that lab pass:

- Hash upload failed because Entra credentials were intentionally not configured
  while local first-run auth was being used. The captured hash exists; Intune
  import/contact/enrollment was not proven in this pass.

## Recovery Commands

Controller health:

```bash
curl -fsS http://192.168.2.115:5000/healthz
```

Controller version:

```bash
curl -fsS http://192.168.2.115:5000/api/version
```

Controller containers through PVE:

```bash
ssh pve-dev-192-168-2-252 'ssh -i /root/.local/share/proxmoxveautopilot/controller-bootstrap-ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new autopilot@192.168.2.115 "cd /opt/ProxmoxVEAutopilot/autopilot-proxmox && sudo docker compose ps"'
```

Rebuild/restart controller from source after syncing the repo to PVE:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive'
```

Recheck final VM QGA:

```bash
ssh pve-dev-192-168-2-252 'qm guest cmd 1201 ping >/dev/null 2>&1 && echo QGA_OK || echo QGA_NOT_READY'
```

Recheck final CloudOSD run:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/cloudosd/runs/1992fdca-e12f-4577-858f-11da11bdc03f' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/cloudosd/runs/1992fdca-e12f-4577-858f-11da11bdc03f'; rm -f "$tmp"
```
