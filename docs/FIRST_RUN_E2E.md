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

For repeatable dev-lab replay from a clean Autopilot state, the preferred
operator command is the console installer:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh'
```

For unattended lab replay with defaults:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh --action guided --yes --download-windows --download-virtio'
```

The lower-level equivalent is:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase all --resume --download-windows --download-virtio --non-interactive'
```

Add `--reset-media` to the reset phase only when the next run must re-exercise
official Windows/VirtIO downloads instead of reusing already downloaded media.

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
VMID: 101
VM name: autopilot-buildhost-01
Computer name: AUTOPILOT-BLD
Agent ID: buildhost-101
```

Repair or reseed the agent when the VM exists but the agent token/config is
stale:

```bash
curl -fsS -X POST 'http://192.168.2.115:5000/api/setup/v1/build-host/repair-agent' \
  -H 'Content-Type: application/json' \
  -d '{"vmid":"101","agent_id":"buildhost-101","computer_name":"AUTOPILOT-BLD","auto_approve":true}'
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

CloudOSD v2 provisions from a blank template VM. PVE init creates or repairs
this template on first-run. For manual recovery on a dev node:

```bash
ssh pve-dev-192-168-2-252 'qm status 9001 >/dev/null 2>&1 || (qm create 9001 --name autopilot-osdeploy-blank-template --memory 4096 --cores 2 --cpu host --machine q35 --bios ovmf --ostype win11 --scsihw virtio-scsi-single --net0 virtio,bridge=vmbr0 --agent enabled=1 --boot order=scsi0 && qm set 9001 --efidisk0 local-zfs:1,efitype=4m,pre-enrolled-keys=1 && qm set 9001 --tpmstate0 local-zfs:4,version=v2.0 && qm set 9001 --scsi0 local-zfs:64,discard=on,iothread=1,ssd=1 && qm template 9001)'
```

Publish the VMID to the controller/PVE inventory:

```bash
ssh pve-dev-192-168-2-252 'grep -q "^cloudosd_blank_template_vmid:" /root/ProxmoxVEAutopilot/autopilot-proxmox/inventory/group_vars/all/vars.yml && sed -i "s/^cloudosd_blank_template_vmid:.*/cloudosd_blank_template_vmid: 9001/" /root/ProxmoxVEAutopilot/autopilot-proxmox/inventory/group_vars/all/vars.yml || printf "\ncloudosd_blank_template_vmid: 9001\n" >> /root/ProxmoxVEAutopilot/autopilot-proxmox/inventory/group_vars/all/vars.yml'
```

## Provision Acceptance

Provision acceptance is path-specific. Do not require hardware hash capture for
OSDeploy base runs; hash capture belongs to CloudOSD/Autopilot enrollment or an
explicit task-engine plan that asks for it.

### OSDeploy Base Acceptance

For the OSDeploy base path, the accepted evidence is:

- Proxmox VM identity, VMID, UUID, and MAC recorded on the run.
- PE registration and package staging.
- Install image located, disk partitioned, image applied, VirtIO drivers
  applied, SetupComplete staged, and boot files staged.
- Full-OS OSD client completion from the installed Windows OS.
- Task Engine v2 `install_autopilot_agent` completed and the persistent
  AutopilotAgent heartbeat is visible when the generated OSDeploy v2 plan is
  used.
- Live QGA evidence after Windows finalization, for example
  `qm guest cmd <vmid> get-osinfo`.
- OSDeploy readiness state `complete`, `qga_status=running`,
  `agent_status=online`, and `server_role_status=base_ready`.

QGA can briefly disappear while Windows finishes post-SetupComplete reboot and
first-boot finalization. Poll live QGA for a few minutes after the controller
job completes before calling it failed.

The OSDeploy base workflow does not require hardware-hash capture or Intune
upload. It should still install the persistent `AutopilotAgent` for Task
Engine v2 ownership, post-OS orchestration, and later optional enrollment
steps. If a later plan includes `capture_autopilot_hash`, treat that as a
separate Autopilot enrollment phase, not as OSDeploy base readiness.

### CloudOSD And Autopilot Enrollment Acceptance

CloudOSD or Autopilot enrollment paths are accepted when the run has the
strongest available evidence for that path:

- Proxmox VM identity and VMID.
- PE registration.
- OSDCloud start/end when the path uses OSDCloud.
- Offline validation.
- Windows first boot.
- AutopilotAgent heartbeat from the installed OS when the path installs the
  agent.
- Hardware hash capture when the path includes Autopilot enrollment.
- Hash upload or a clear, expected upload blocker when upload is configured.
- QGA when the Windows guest agent is installed and running.

Hash capture alone is not enough for a healthy deployment; final health should
use OS, agent, QGA, CloudOSD, Intune, or readiness evidence as appropriate for
the selected path.

## Monitoring Checks

Deployment timing is visible through `/monitoring` and these APIs:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/monitoring' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/monitoring/deployments/summary'; rm -f "$tmp"
```

Recent normalized runs:

```bash
tmp=$(mktemp); curl -fsS -c "$tmp" -X POST 'http://192.168.2.115:5000/auth/local/start?next=/monitoring' -o /tmp/autopilot-login.html; curl -fsS -b "$tmp" 'http://192.168.2.115:5000/api/monitoring/deployments/runs?limit=12'; rm -f "$tmp"
```

## Later: Install Progress WebUI

Add an install-process WebUI after the E2E path is stable. It should be backed
by existing setup, job, OSDeploy, CloudOSD, agent, and monitoring telemetry
rather than a separate tracker.

First version:

- `/setup/progress` or an embedded `/setup` panel with Foundation, Bootstrap,
  and Operational lanes.
- A live timeline for PVE foundation, controller bootstrap, media gate,
  build-host creation, source-build workloads, artifact promotion, and first
  provision run.
- Current phase, elapsed time, last evidence, expected next evidence, retry
  command, and log link per lane.
- WebSocket or polling updates from `/api/setup/v1/state`,
  `/api/monitoring/deployments/runs`, `/api/jobs/*`, `/api/osdeploy/v1/runs/*`,
  and CloudOSD/WinPE run APIs.
- Clear paused states for media upload, build-host approval, auth provider
  setup, and vendor download failures.
- No secret values, raw tokens, or vault paths in the browser.

Look for phase-level timings across jobs, task-engine runs, CloudOSD,
build-host work, and agent work.

## Dev Lab Proof Record

### Latest pvetest checkpoint, 2026-05-18

The latest dev-lab pass proved Foundation -> Bootstrap -> Operational on
`pvetest`, then intentionally reset the lab back to a clean pre-init state.

Accepted run before reset:

```text
PVE node: pvetest, 192.168.2.252
Controller VM: 100, autopilot-controller-01, 192.168.2.127
Build host VM: 101, autopilot-buildhost-01, AUTOPILOT-BLD
Accepted test VM: 102, APE2E004, 192.168.2.143
Accepted CloudOSD run: ace280e8-6e9c-43b1-ba15-c74ca716ac29
Provision job: 20260518-e54b, exit code 0
Agent ID: agent-ape2e004
Agent version: 0.1.2.0
Controller build: d36c2ea, 2026-05-18T03:24:45Z
Hash file: 20260518T030221Z-vm102-APE2E004-osd-v2_hwid.csv
Hash SHA-256: 51adeab8a3050326ade7b9fac865b23696f46802e972ce10cad4198d0dcbb750
```

Observed successful evidence:

- `/api/setup/v1/state` reported `phase=operational`, `health=ready`, and
  `blocking_count=0`.
- Local first-run auth was active and Entra auth was not required for local
  setup.
- Build host VMID `101` had `agent_state=ready` and no active work.
- Setup artifacts were ready with promoted `cloudosd-iso`, `osdeploy-iso`, and
  `winpe-iso` kinds.
- PVE had no running Autopilot Docker containers; Docker/Compose runtime lived
  in the Ubuntu controller.
- Controller containers `autopilot`, `autopilot-mcp`, and
  `autopilot-postgres` were healthy; monitor and builder containers were
  running.
- QEMU Guest Agent responded for VMID `102` using `qm guest cmd 102 ping`.
- Guest command execution returned
  `Microsoft Windows [Version 10.0.26200.8246]`.
- AutopilotAgent heartbeat was visible from installed Windows with
  `computer_name=APE2E004`, `primary_ipv4=192.168.2.143`, and
  `qga_state=Running`.
- CloudOSD readiness reported `hash_status=captured`, the hash SHA-256 above,
  `upload_status=not_configured`, `upload_job_id=null`, and no readiness
  errors.
- `/monitoring` reported `active=0`, `stuck=0`, and the accepted CloudOSD row
  as `state=done`, `health=learning`, `duration_seconds=1523`.

Known external-readiness boundary:

- Entra/Graph upload credentials were intentionally not configured. The run
  ended at `upload_not_configured` with `next_action=configure_entra`. This is
  a configuration boundary for Autopilot import, not a failed local first-run.

Reset after acceptance:

```text
Reset command:
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase reset-dev-lab --reset-media --non-interactive'

Reset state phase: reset-dev-lab
dev_lab_reset_ready: true
pve_host_clean_ready: true
```

Verified after reset:

- `qm list` showed no remaining VMs.
- Controller `192.168.2.127:5000` was unreachable, as expected.
- No Autopilot Docker containers were running on the PVE host.
- `/var/lib/vz/template/iso` had no remaining ISO files.
- Reset removed generated media including Windows, VirtIO, build-host seed,
  WinPE, CloudOSD, and OSDeploy ISOs.

Where testing left off:

1. Start the next pass from this clean reset state.
2. Run the console installer guided path or:

   ```bash
   ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase all --resume --download-windows --download-virtio --non-interactive'
   ```

3. Confirm automated official Windows/VirtIO media download works from empty
   ISO storage.
4. Confirm the controller build metadata is populated under
   `/api/version.running`.
5. Recreate the build host and rerun source-build workloads.
6. Promote artifacts and launch one CloudOSD or OSDeploy provision workflow.
7. Verify `/setup` reaches operational and `/monitoring` has no active/stuck
   rows after the provision run completes.

### Previous pvetest checkpoint, 2026-05-16

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
- CloudOSD/Autopilot enrollment evidence included hardware hash capture:
  `autopilotagent_v2_hash_capture_complete` for `agent-ap-e2e-qga-001`.
- The earlier VMID 1200 CloudOSD/Autopilot pass also completed and captured:
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

# First-run end-to-end walkthrough

This section is the operator runbook for the onboarding wizard added under
`/react/onboarding`. It mirrors the steps in
`docs/superpowers/plans/2026-05-27-onboarding-wizard.md` Task 14 and assumes the
controller is reachable and the build host (if needed) is already approved.

## Onboarding wizard happy-path

Touch points used in this runbook (verified against current code):

- Page routes: `/react/onboarding` (wizard) and `/react/onboarding/setup`
  (monitor). Wired in `autopilot-proxmox/frontend/src/App.tsx`.
- API prefix: `/api/onboarding` (all state, probe, launch, and setup-status
  endpoints live under this prefix). Defined in
  `autopilot-proxmox/web/onboarding_endpoints.py`.
- Step rail labels (5 in-wizard steps): `Welcome`, `Identity`, `Tenant`,
  `Artifact`, `Review`. Source: `frontend/src/onboarding/StepRail.tsx`.
- Hero CTAs on `/react-shell`: "Resume onboarding" while
  `status` is `pending` or `in_progress`; "Resume setup monitor" while
  `status` is `launched`; absent for `absent`, `complete`, and `aborted`.

### Build and start the stack

Skip this if a controller is already running. From the operator workstation
shell on the controller VM (or wherever Compose is hosted):

```bash
cd /opt/ProxmoxVEAutopilot/autopilot-proxmox
sudo docker compose down
sudo docker compose build
sudo docker compose up -d
```

Confirm the web app, mcp, postgres, monitor, and at least one builder replica
are healthy:

```bash
sudo docker compose ps
```

### Confirm bootstrap exposes the onboarding field

Open the controller in a browser. In DevTools Console, inspect the inline
bootstrap JSON injected into the page:

```js
// On any /react/* page, the bootstrap is exposed for debugging on window.
window.__APP_BOOTSTRAP__?.onboarding
```

For a brand-new controller the value should be
`{ status: "absent", run_id: null, etag: null }`. The hero CTA on
`/react-shell` is hidden in this state by design (absence means
"do not show"). The Settings nav still shows "Onboarding wizard" so the
operator can discover the entry point.

### Walk the wizard

1. Navigate to `/react-shell`. Confirm there is no "Resume onboarding" hero
   card (status absent).
2. Open the Settings nav group and click "Onboarding wizard". The page lands
   on `/react/onboarding` at the Welcome step. The "Already configured" card
   above the step shows Proxmox host/node/version, default storage pools,
   default bridge, and AD vault status.
3. Pick "Lab hobbyist" on the Welcome step. Click Next.
4. On Identity, pick "Workgroup". Click Next. AD-joined would prompt for
   domain, join account, join password, and local admin password with a
   "Test this now" probe (`POST /api/onboarding/probe/ad`).
5. On Tenant, the step rail shows the `optional` badge because persona is
   `lab` and identity is `workgroup`. Tick "Skip tenant setup for now".
   Click Next.
6. On Artifact, the two radio choices are "Use an existing artifact" and
   "Build one now". Pick an existing CloudOSD or OSDeploy artifact from the
   picker, or click "Build one now" to kick a build (the wizard does not
   block on builds; the build job is tracked in the monitor page's phase
   rail once you reach Review). Click Next.
7. On Review, every prior answer renders with inline edit links. Set the
   trial VM name (default `autopilot-trial-<vmid>`) and target node
   (default the controller's known node for `lab`). Click "Start setup".
8. The page navigates to `/react/onboarding/setup`. The phase rail polls
   `GET /api/onboarding/setup-status` every 2 seconds while the tab is
   foreground, every 10 seconds when backgrounded.
9. Wait for every phase (`Validate`, optional `Build artifact`,
   `Clone template`, optional `Inject Autopilot`, `Provision`,
   `Watch OOBE`) to reach `ready`. Phases that do not apply for the picked
   persona/identity show as `skipped` in the rail.
10. Verify the completion card appears with the VM IP, RDP shortcut, and an
    "Open VM detail" deep link to `/react/vms/<vmid>`.
11. Click the link and confirm the trial VM exists in `/react/vms`.

### Refresh-and-resume

1. Open a second wizard session in a new browser profile. Walk to the
   Identity step and fill in some values.
2. Hard-refresh the page (Cmd+Shift+R / Ctrl+Shift+R).
3. Confirm the wizard restores to the Identity step and the previously typed
   values are still present. The frontend persistence layer in
   `frontend/src/onboarding/persistence.ts` uses ETag + retry to round-trip
   the state through `GET`/`PUT /api/onboarding/state`.

### Discard

1. With the wizard in `pending` or `in_progress`, click the "Discard
   onboarding" link in the wizard footer.
2. Confirm the modal ("Discard your onboarding progress?") with
   "Yes, discard".
3. The frontend issues `DELETE /api/onboarding/state` and bounces back to
   `/react-shell`. Confirm there is no hero CTA after the discard. While
   `status='launched'` the Discard link is disabled with the tooltip
   "Cannot discard mid-launch. Abort the run from /react/jobs first."

### Backward-compat

1. Log in as a different operator (or impersonate) who has no onboarding
   row in `onboarding_state`.
2. Confirm there is no hero CTA on `/react-shell` (absence equals
   "do not show").
3. Confirm the Settings nav still surfaces "Onboarding wizard" for
   discoverability.

## Self-review checklist coverage

The plan's "Self-Review Checklist" at `docs/superpowers/plans/2026-05-27-onboarding-wizard.md`
lines 3517-3527 is partially verifiable in an offline code-only session and
partially requires a live cluster. Status going into the PR:

Session-verified (covered by Task 14's offline pass):

- Every step in the spec's "Wizard steps" section maps to an implementation
  task: Welcome (Task 6), Identity (Task 7), Tenant (Task 8), Artifact
  (Task 9), Review (Task 10), Setup monitor screen (Tasks 11-13).
- No file path in the plan refers to a non-existent file (the plan's path
  references are matched by shipped files: `onboarding_endpoints.py`,
  `onboarding_launch.py`, `onboarding_phases.py`, `frontend/src/onboarding/`,
  `frontend/src/pages/Onboarding*.tsx`).
- No new "TODO"/"TBD"/"FIXME" markers introduced by Tasks 1-13 inside the
  onboarding code paths.
- All unit suites green:
  `pytest tests/test_onboarding_pg.py tests/test_onboarding_endpoints.py tests/test_onboarding_probes.py tests/test_onboarding_launch.py tests/test_onboarding_phases.py tests/test_react_shell.py`
  passes locally; `npx tsc --noEmit` clean; `npx vitest run` on the
  onboarding suites passes.
- ASCII hyphens only: the literal em-dash/en-dash grep in the plan returns
  zero matches across the onboarding code paths and the design spec.

Needs human running the cluster (deferred to live walkthrough):

- Final manual walkthrough against the live controller, with a trial VM
  appearing under `/react/vms`. Use the "Walk the wizard" section above.
- DevTools verification that `window.__APP_BOOTSTRAP__.onboarding` is
  populated on a fresh controller.
- Refresh-and-resume, Discard, and Backward-compat scenarios above.
