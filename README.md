# Proxmox VE Autopilot

A web-based tool for provisioning Windows VMs on Proxmox with OEM-accurate SMBIOS fields and hardware hash capture for Windows Autopilot / Intune registration.

## What It Does

Proxmox VE Autopilot creates Windows VMs that appear as real OEM hardware to Windows Autopilot. Each VM gets manufacturer-specific SMBIOS fields (Lenovo, Dell, HP, Microsoft Surface, or generic), a unique serial number, and a hardware identity. The hardware hash is captured and uploaded to Intune — all from a browser, without touching a physical machine.

### The Pipeline

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Build Template  │────>│   Clone VMs      │────>│  Capture Hashes │────>│  Upload to Intune│
│  (one-time)      │     │  (per device)    │     │  (per device)   │     │  (batch)         │
└─────────────────┘     └──────────────────┘     └─────────────────┘     └──────────────────┘
```

All communication with guest VMs happens through the Proxmox REST API and QEMU guest agent — no WinRM, no SSH, no network access to the guest required.

```
Browser ──> Web UI ──REST API──> Proxmox Host ──QEMU Guest Agent──> Windows VM
```

## Quick Start

### 1. Proxmox API Token

Run on your **Proxmox host shell**:

```bash
# Create role with required privileges
pveum role add AutopilotProvisioner -privs VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.CDROM,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Audit,VM.PowerMgmt,VM.Console,VM.Snapshot,VM.Snapshot.Rollback,VM.GuestAgent.Audit,VM.GuestAgent.FileRead,VM.GuestAgent.FileWrite,VM.GuestAgent.FileSystemMgmt,VM.GuestAgent.Unrestricted,Datastore.AllocateSpace,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use

# Create service account + API token
pveum user add autopilot@pve --comment "Autopilot provisioning"
pveum user token add autopilot@pve ansible --privsep=0 --comment "Automation"
# ^^^ Save the full-tokenid and value!

# Assign permissions (adjust storage/SDN names for your environment)
pveum acl modify / -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/ssdpool -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/isos -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /sdn/zones/localnetwork -user autopilot@pve -role AutopilotProvisioner
```

### 2. Unattended Answer ISO

The included `autounattend.xml` fully automates Windows installation. Build the ISO on your Proxmox host:

```bash
scp autopilot-proxmox/files/autounattend.xml root@<PROXMOX_IP>:/tmp/autounattend.xml

# On the Proxmox host:
apt-get install -y genisoimage
STORAGE_PATH=$(pvesm path isos:iso/autounattend.iso | sed 's|/autounattend.iso$||')
mkdir -p /tmp/answeriso
cp /tmp/autounattend.xml /tmp/answeriso/autounattend.xml
genisoimage -o "${STORAGE_PATH}/autounattend.iso" -J -r -V "OEMDRV" /tmp/answeriso/
rm -rf /tmp/answeriso /tmp/autounattend.xml
```

### 3. Deploy with Docker

```bash
git clone https://github.com/adamgell/ProxmoxVEAutopilot.git
cd ProxmoxVEAutopilot/autopilot-proxmox

# Configure credentials
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
# Edit vault.yml with your Proxmox API token and Entra app credentials

# Start the container
docker compose up -d
```

The web UI is available at **http://your-host:5000**.

### 4. Configure via Web UI

Open the **Settings** page and configure:

- **Proxmox Connection**: Host, port, node
- **Storage & Networking**: VM storage, ISO storage, network bridge
- **ISO Paths**: Windows ISO, VirtIO ISO, answer ISO
- **VM Defaults**: CPU, memory, disk size, OEM profile, group tag

## Web UI

### Pages

| Page | Description |
|------|-------------|
| **Home** | Dashboard with running jobs and hash file count |
| **Provision VMs** | Clone VMs from the template with a selected OEM profile, count, and group tag |
| **Devices** | View all Proxmox autopilot VMs and Intune Autopilot devices with inline actions |
| **Build Template** | Create a Windows template from ISO (one-time setup) |
| **Upload to Intune** | Upload captured hash files to Microsoft Intune |
| **Hash Files** | Browse, download, and delete captured hardware hash CSVs |
| **Import Hashes** | Upload hash CSV files from your local machine |
| **Jobs** | View all running and completed jobs with live log streaming |
| **Settings** | Configure Proxmox connection, VM defaults, and timeouts |

### VM Actions

The Devices page shows all autopilot VMs with per-VM action buttons:

| Icon | Action | Description |
|------|--------|-------------|
| Power | **Start** | Power on a stopped VM |
| Power | **Shutdown** | ACPI graceful shutdown (with confirmation) |
| Stop | **Force Stop** | Immediate power off (with confirmation) |
| Refresh | **Reset** | Reboot the VM |
| Hash | **Capture Hash** | Run hash capture via guest agent |
| Pencil | **Rename** | Rename Windows hostname to match the VM serial |
| Monitor | **Console** | Open Proxmox noVNC console |
| X | **Delete** | Stop and remove the VM (with confirmation) |

Bulk operations: select multiple VMs and capture hashes in parallel — each VM gets its own independent job, so one failure doesn't affect the others.

## OEM Profiles

13 built-in profiles in `files/oem_profiles.yml`:

| Key | Manufacturer | Product | Chassis |
|-----|-------------|---------|---------|
| `lenovo-p520` | Lenovo | ThinkStation P520 | Desktop |
| `lenovo-t14` | Lenovo | ThinkPad T14 Gen 4 | Notebook |
| `lenovo-x1carbon` | Lenovo | ThinkPad X1 Carbon Gen 11 | Notebook |
| `dell-optiplex-7090` | Dell Inc. | OptiPlex 7090 | Desktop |
| `dell-latitude-5540` | Dell Inc. | Latitude 5540 | Notebook |
| `dell-xps-15` | Dell Inc. | XPS 15 9530 | Notebook |
| `hp-elitedesk-800` | HP | EliteDesk 800 G8 SFF | Desktop |
| `hp-elitebook-840` | HP | EliteBook 840 G10 | Notebook |
| `hp-zbook-g10` | HP | ZBook Fury 16 G10 | Notebook |
| `surface-pro-10` | Microsoft Corporation | Surface Pro 10 | Laptop |
| `surface-laptop-6` | Microsoft Corporation | Surface Laptop 6 | Notebook |
| `generic-desktop` | Proxmox | Virtual Desktop | Desktop |
| `generic-laptop` | Proxmox | Virtual Laptop | Notebook |

Each profile sets SMBIOS type 1 fields and generates a manufacturer-appropriate serial number prefix (Lenovo=PF, Dell=SVC, HP=CZC, Microsoft=MSF).

## Prerequisites

| Component | Requirement |
|-----------|------------|
| **Proxmox VE** | 9.x with API token |
| **Windows ISO** | Stock Windows 11 Enterprise (unmodified) |
| **VirtIO ISO** | `virtio-win.iso` (latest from Fedora/Red Hat) |
| **Docker host** | Any machine with Docker Compose |

## Docker Compose

The default `docker-compose.yml` uses the pre-built image from GitHub Container Registry:

```yaml
services:
  autopilot:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    container_name: autopilot
    network_mode: host
    volumes:
      - ./inventory/group_vars/all/vault.yml:/app/inventory/group_vars/all/vault.yml:ro
      - ./inventory/group_vars/all/vars.yml:/app/inventory/group_vars/all/vars.yml
      - ./output:/app/output
      - autopilot-jobs:/app/jobs
    restart: unless-stopped

volumes:
  autopilot-jobs:
```

Key volume mounts:
- `vault.yml` (read-only): Proxmox API token and Entra app credentials
- `vars.yml` (writable): Configuration editable from the Settings page
- `./output`: Hash CSV files persisted on the host filesystem
- `autopilot-jobs`: Job logs

## Ansible CLI (Advanced)

The web UI runs Ansible playbooks under the hood. You can also run them directly:

```bash
cd autopilot-proxmox
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Build template (one-time)
ansible-playbook playbooks/build_template.yml -e vm_oem_profile=generic-desktop

# Provision VMs
ansible-playbook playbooks/provision_clone.yml -e vm_oem_profile=lenovo-t14 -e vm_count=5

# Upload hashes to Intune
ansible-playbook playbooks/upload_hashes.yml

# Re-capture hash on existing VM
ansible-playbook playbooks/retry_inject_hash.yml -e vm_vmid=106 -e vm_name=autopilot-106
```

## How the Unattended Install Works

The `autounattend.xml` answer file is mounted as a separate tiny ISO alongside the Windows ISO and VirtIO ISO. When the VM boots:

1. OVMF starts, Ansible sends a keypress to boot from CD
2. Windows Setup finds `autounattend.xml` on the mounted ISO
3. **WindowsPE pass**: Creates GPT partitions, loads VirtIO drivers from the VirtIO ISO
4. **Specialize pass**: Sets computer name and timezone
5. **OOBE pass**: Skips all prompts, creates Administrator account, auto-logs in
6. **FirstLogonCommands**: Installs the QEMU guest agent from the VirtIO ISO
7. Ansible detects the guest agent and proceeds with hash capture or sysprep

The stock Windows ISO is never modified.

## Testing

```bash
cd autopilot-proxmox
pip install pytest ansible ansible-lint
pytest tests/ -v
ansible-playbook --syntax-check playbooks/*.yml
```
