# Proxmox VE Autopilot

A web-based tool for provisioning Windows VMs on Proxmox with OEM-accurate SMBIOS fields and hardware hash capture for Windows Autopilot / Intune registration.

![Devices Page](docs/screenshots/devices.png)

## What It Does

Proxmox VE Autopilot creates Windows VMs that appear as real OEM hardware to Windows Autopilot. Each VM gets manufacturer-specific SMBIOS fields (Lenovo, Dell, HP, Microsoft Surface, or generic), a unique serial number, and a hardware identity. The hardware hash is captured and uploaded to Intune — all from a browser, without touching a physical machine.

For Windows desktop deployments, the primary path uses [OSDCloud](https://www.osdcloud.com/) as the Windows deployment substrate, while Proxmox VE Autopilot owns Proxmox VM creation, identity, cache warming, task-sequence intent, AutopilotAgent bootstrap, and readiness evidence.

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Build Template │────>│   Clone VMs      │────>│  Capture Hashes │────>│  Upload to Intune│
│  (one-time)     │     │  (per device)    │     │  (per device)   │     │  (batch)         │
└─────────────────┘     └──────────────────┘     └─────────────────┘     └──────────────────┘
```

All communication with guest VMs happens through the Proxmox REST API and QEMU guest agent — no WinRM, no SSH, no network access to the guest required.

> **Also supported (experimental): UTM + QEMU on macOS ARM64.**
> Run Windows ARM64 VMs on Apple Silicon using UTM.app. Requires
> running the web service natively on macOS (not in Docker) because
> `utmctl` is a macOS-host binary. See
> [`docs/UTM_MACOS_SETUP.md`](docs/UTM_MACOS_SETUP.md) for setup.
> macOS operators can use `autopilot-proxmox/scripts/tui.sh` as an
> interactive launcher (see docs/UTM_MACOS_SETUP.md).

## Prerequisites

| Component       | Requirement                                          |
|-----------------|------------------------------------------------------|
| **Proxmox VE**  | 9.x with API access                                  |
| **Windows ISO** | Stock Windows 11 Enterprise / Business (unmodified)  |
| **VirtIO ISO**  | `virtio-win.iso` (latest from Fedora / Red Hat)      |
| **Ubuntu ISO**  | Ubuntu 24.04 live-server ISO (optional — only needed for Ubuntu sequences) |
| **Controller VM** | Ubuntu Server 24.04 LTS VM created by first-run init |

The primary first-run path keeps the Proxmox VE host clean. PVE creates an
Ubuntu controller VM; Docker, Compose, Postgres, MCP, builder, monitor, web UI,
and Windows artifact orchestration run inside that controller. Windows and
VirtIO media can be uploaded manually or handled by the assisted media gate.

## Quick Start

### 1. Copy the repo to the Proxmox VE node

From the operator workstation:

```bash
rsync -a --delete \
  --exclude 'autopilot-proxmox/.env' \
  --exclude 'autopilot-proxmox/inventory/group_vars/all/vault.yml' \
  --exclude 'autopilot-proxmox/secrets/' \
  --exclude 'autopilot-proxmox/output/' \
  '/Users/Adam.Gell/repo/ProxmoxVEAutopilot/' \
  pve-dev-192-168-2-252:/root/ProxmoxVEAutopilot/
```

### 2. Run the console installer

Run this on the Proxmox VE node as root. The installer is a numbered shell UI
for the core first-run path: Foundation -> Bootstrap -> Operational. It keeps
the PVE host clean by delegating all mutations to `init-proxmox-ve.sh`.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh'
```

For an unattended lab run with defaults:

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/install-proxmox-ve.sh --action guided --yes --controller-ip 192.168.2.115'
```

### 3. Foundation fallback

Run this on the Proxmox VE node as root. It repairs API token/ACLs/storage,
creates the Ubuntu controller VM, syncs source/config, starts the controller
runtime, and verifies `/healthz`.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase foundation --resume --controller-ip 192.168.2.115 --non-interactive'
```

### 4. Satisfy the media gate

The default lab path downloads Windows 11 from Microsoft's official software
download connector and VirtIO from the official virtio-win source. Manual
Windows ISO upload and `--windows-iso-url` remain supported recovery paths.

```bash
ssh pve-dev-192-168-2-252 'bash /root/ProxmoxVEAutopilot/autopilot-proxmox/scripts/init-proxmox-ve.sh --phase bootstrap --resume --download-windows --download-virtio --controller-ip 192.168.2.115 --non-interactive'
```

### 5. Finish `/setup`

Open the controller UI:

```text
http://192.168.2.115:5000/setup
```

Use local first-run auth if Entra is not configured. From `/setup`, create or
repair the Windows build host, queue source-build workloads, promote artifacts,
and wait for operational readiness.

### 5. Provision a test Windows VM

After `/setup` is operational, use CloudOSD or WinPE artifacts to launch a real
template/provision workflow. Confirm QGA when available, AutopilotAgent
heartbeat, hash capture/upload state, and CloudOSD/WinPE readiness evidence.

Full runbook:

- [docs/FIRST_RUN_E2E.md](docs/FIRST_RUN_E2E.md)
- [docs/PVE_INIT.md](docs/PVE_INIT.md)
- [docs/WINDOWS_BUILD_BOX.md](docs/WINDOWS_BUILD_BOX.md)

### Manual Fallback

The older manual container setup remains documented in [docs/SETUP.md](docs/SETUP.md).
Use it only when you intentionally want to bring your own Docker host instead of
the Ubuntu controller VM first-run path.

The app still supports the legacy Build Template and Provision pages. The
first-run path adds controller/build-host/artifact automation; it does not
remove the existing `/winpe/*`, CloudOSD, task sequence, or clone/template
behavior.

The app ships with three seeded **task sequences** — *Entra Join (default)*, *AD Domain Join — Local Admin*, and *Hybrid Autopilot (stub)*. The default reproduces today's Autopilot flow byte-for-byte; pick a different sequence on the Provision page to join a domain instead. See [docs/SETUP.md#task-sequences-and-credentials](docs/SETUP.md#task-sequences-and-credentials).

---

Stuck? Jump to [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md). Need the full walkthrough with screenshots and field-by-field detail? See [docs/SETUP.md](docs/SETUP.md).

**Ubuntu path:** On Build Template, toggle **Ubuntu** → pick a sequence → **Rebuild Ubuntu Seed ISO** → **Build Ubuntu Template** (~25 min). Then Provision just like the Windows flow — cloud-init sets the hostname on first boot. See [docs/SETUP.md#ubuntu-path](docs/SETUP.md#ubuntu-path).

## Web UI

| | |
|---|---|
| ![Home](docs/screenshots/home.png) | ![Provision](docs/screenshots/provision.png) |
| ![Devices](docs/screenshots/devices.png) | ![Jobs](docs/screenshots/jobs.png) |
| ![Settings](docs/screenshots/settings.png) | ![Hashes](docs/screenshots/hashes.png) |

### Pages

| Page | Description |
|------|-------------|
| **Home** | Dashboard with running jobs and hash file count |
| **Provision VMs** | Clone VMs from the template with a selected task sequence, OEM profile, count, and group tag |
| **Devices** | View all Proxmox autopilot VMs and Intune Autopilot devices with inline actions |
| **Sequences** | Create and edit named **task sequences** — ordered lists of steps that define what happens during OOBE. Windows (Entra Join, AD Domain Join) and Ubuntu (Intune + MDE via LinuxESP, Plain) share one builder |
| **Credentials** | Encrypted store for reusable secrets — local admin passwords, AD domain-join accounts, ODJ blobs, MDE Linux onboarding scripts. Includes a **Test connection** button for domain-join credentials |
| **Build Template** | Rebuild the answer ISO and create a Windows or Ubuntu template (one-time setup per OS) |
| **Upload to Intune** | Upload captured hash files to Microsoft Intune |
| **Hash Files** | Browse, download, and delete captured hardware hash CSVs |
| **Import Hashes** | Upload hash CSV files from your local machine |
| **Jobs** | View all running and completed jobs with live log streaming |
| **Settings** | Configure Proxmox connection, VM defaults, and timeouts |

### VM actions (Devices page)

| Action | Description |
|--------|-------------|
| **Start** | Power on a stopped VM |
| **Shutdown** | ACPI graceful shutdown (with confirmation) |
| **Force Stop** | Immediate power off (with confirmation) |
| **Reset** | Reboot the VM |
| **Capture Hash** | Run hash capture via guest agent |
| **Rename** | Rename Windows hostname to match the VM serial |
| **Console** | Open Proxmox noVNC console |
| **Delete** | Stop and remove the VM (with confirmation) |

Select multiple VMs to capture hashes in parallel — each VM gets its own independent job, so one failure doesn't affect the others.

## OEM Profiles

13 built-in profiles in `autopilot-proxmox/files/oem_profiles.yml`:

| Key                   | Manufacturer          | Product                    | Chassis  |
|-----------------------|-----------------------|----------------------------|----------|
| `lenovo-p520`         | Lenovo                | ThinkStation P520          | Desktop  |
| `lenovo-t14`          | Lenovo                | ThinkPad T14 Gen 4         | Notebook |
| `lenovo-x1carbon`     | Lenovo                | ThinkPad X1 Carbon Gen 11  | Notebook |
| `dell-optiplex-7090`  | Dell Inc.             | OptiPlex 7090              | Desktop  |
| `dell-latitude-5540`  | Dell Inc.             | Latitude 5540              | Notebook |
| `dell-xps-15`         | Dell Inc.             | XPS 15 9530                | Notebook |
| `hp-elitedesk-800`    | HP                    | EliteDesk 800 G8 SFF       | Desktop  |
| `hp-elitebook-840`    | HP                    | EliteBook 840 G10          | Notebook |
| `hp-zbook-g10`        | HP                    | ZBook Fury 16 G10          | Notebook |
| `surface-pro-10`      | Microsoft Corporation | Surface Pro 10             | Laptop   |
| `surface-laptop-6`    | Microsoft Corporation | Surface Laptop 6           | Notebook |
| `generic-desktop`     | Proxmox               | Virtual Desktop            | Desktop  |
| `generic-laptop`      | Proxmox               | Virtual Laptop             | Notebook |

Each profile sets SMBIOS type 1 fields and generates a manufacturer-appropriate serial number prefix (Lenovo=PF, Dell=SVC, HP=CZC, Microsoft=MSF).

## More Documentation

- **[docs/SETUP.md](docs/SETUP.md)** — detailed setup walkthrough with field-by-field configuration, unattended-install internals, and an air-gapped answer-ISO recipe. Includes the Ubuntu path (LinuxESP).
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — symptoms, causes, and fixes for common failures (Windows and Ubuntu).
- **[docs/PYTHON_VERSIONS.md](docs/PYTHON_VERSIONS.md)** — Python version matrix: minimum supported, CI-tested versions, 3.13/3.14 concerns, and macOS pyenv/uv install guidance.
- **[autopilot-proxmox/README.md](autopilot-proxmox/README.md)** — Ansible CLI usage, playbooks, and developer reference.
