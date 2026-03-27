# Proxmox VE Autopilot

Automated provisioning of Windows VMs on Proxmox with OEM-accurate SMBIOS fields and hardware hash capture for Windows Autopilot / Intune registration.

## How It Works

This project creates Windows VMs on Proxmox that appear as real OEM hardware to Windows Autopilot. Each VM gets manufacturer-specific SMBIOS fields (Lenovo, Dell, HP, Microsoft Surface, or generic), a unique serial number, and a hardware identity. The hardware hash is captured from each VM and uploaded to Intune, registering the device in Autopilot — all without touching a physical machine.

### The Pipeline

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Build Template  │────>│   Clone VMs      │────>│  Capture Hashes │────>│  Upload to Intune│
│  (one-time)      │     │  (per device)    │     │  (per device)   │     │  (batch)         │
└─────────────────┘     └──────────────────┘     └─────────────────┘     └──────────────────┘
```

**Step 1 — Build Template** (one-time): Boot a stock Windows 11 ISO with an unattended answer file. Windows installs automatically — partitions the disk, loads VirtIO drivers, creates an admin account, and installs the QEMU guest agent. Once complete, Ansible syspreps the VM and converts it to a Proxmox template.

**Step 2 — Clone VMs**: Full-clone from the template. Each clone gets a unique SMBIOS identity (manufacturer, product, family, SKU, serial number, UUID) from the selected OEM profile. The VM boots and goes through the sysprep mini-setup (OOBE).

**Step 3 — Capture Hashes**: Once the guest agent is responsive, Ansible pushes `Get-WindowsAutopilotInfo.ps1` into the VM via the Proxmox API, executes it, and reads back the hardware hash CSV. No network access to the guest is needed — everything goes through the QEMU guest agent.

**Step 4 — Upload to Intune**: The captured CSV files are uploaded to Microsoft Intune via the Graph API, registering each device in Windows Autopilot. Devices can optionally include a group tag for automatic Autopilot profile assignment.

### No WinRM, No SSH, No Network

All communication with the guest VM happens through the Proxmox REST API and QEMU guest agent. The controller never needs network access to the Windows VM. File writes, script execution, and result retrieval all go through the hypervisor.

```
Controller ──REST API──> Proxmox Host ──QEMU Guest Agent──> Windows VM
```

## Prerequisites

| Component | Requirement |
|-----------|------------|
| **Proxmox VE** | 9.x with API token (see setup below) |
| **Windows ISO** | Stock Windows 11 Enterprise (no modification needed) |
| **VirtIO ISO** | `virtio-win.iso` (latest from Fedora/Red Hat) |
| **Controller** | Any machine with Ansible 2.14+, Python 3.9+ |
| **Hash upload** | `pwsh` + `WindowsAutopilotIntune` module (optional, for Intune upload) |

## Setup

### 1. Proxmox API Token

Run on your **Proxmox host shell**:

```bash
# Create role with all PVE 9 privileges needed
pveum role add AutopilotProvisioner -privs VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.CDROM,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Audit,VM.PowerMgmt,VM.Console,VM.Snapshot,VM.Snapshot.Rollback,VM.GuestAgent.Audit,VM.GuestAgent.FileRead,VM.GuestAgent.FileWrite,VM.GuestAgent.FileSystemMgmt,VM.GuestAgent.Unrestricted,Datastore.AllocateSpace,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use

# Create service account
pveum user add autopilot@pve --comment "Ansible Autopilot provisioning"

# Create API token (privsep=0 = token inherits user permissions)
pveum user token add autopilot@pve ansible --privsep=0 --comment "Ansible automation"
# ^^^ Save the full-tokenid and value from the output!

# Assign permissions — adjust storage/SDN names to match your environment
pveum acl modify / -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/ssdpool -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/isos -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /sdn/zones/localnetwork -user autopilot@pve -role AutopilotProvisioner
```

### 2. Unattended Answer ISO

The project includes `autounattend.xml` which fully automates the Windows install. It gets mounted as a small separate ISO — your stock Windows ISO stays untouched.

```bash
# Copy answer file to Proxmox host
scp autopilot-proxmox/files/autounattend.xml root@<PROXMOX_IP>:/tmp/autounattend.xml

# On the Proxmox host:
apt-get install -y genisoimage
STORAGE_PATH=$(pvesm path isos:iso/autounattend.iso | sed 's|/autounattend.iso$||')
mkdir -p /tmp/answeriso
cp /tmp/autounattend.xml /tmp/answeriso/autounattend.xml
genisoimage -o "${STORAGE_PATH}/autounattend.iso" -J -r -V "OEMDRV" /tmp/answeriso/
rm -rf /tmp/answeriso /tmp/autounattend.xml
```

If your ISO storage is local (not shared), repeat on every Proxmox node.

### 3. Configure Ansible

```bash
cd autopilot-proxmox

# Set up Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install ansible

# Configure your credentials
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
# Edit vault.yml with your API token and Entra credentials

# Optionally encrypt
ansible-vault encrypt inventory/group_vars/all/vault.yml

# Edit vars.yml with your Proxmox host, storage, ISO paths, etc.
```

### 4. Install pwsh (for hash upload only)

```bash
# macOS
brew install powershell

# Then install the required modules
pwsh -Command "Install-Module -Name Microsoft.Graph.Authentication -Force"
pwsh -Command "Install-Module -Name WindowsAutopilotIntune -MinimumVersion 5.4.0 -Force"
```

## Usage

### Build a template (one-time)

```bash
ansible-playbook playbooks/build_template.yml -e vm_oem_profile=generic-desktop
```

This creates a VM from ISO, waits for the unattended Windows install to complete (~20-30 min), syspreps it, and converts it to a template. The output tells you the template VMID — set it in `vars.yml` as `proxmox_template_vmid`.

### Provision VMs and capture hashes

```bash
# Single VM with Lenovo ThinkPad T14 identity
ansible-playbook playbooks/provision_clone.yml \
  -e vm_oem_profile=lenovo-t14

# 5 Dell Latitude VMs with a group tag
ansible-playbook playbooks/provision_clone.yml \
  -e vm_oem_profile=dell-latitude-5540 \
  -e vm_count=5 \
  -e vm_group_tag=Autopilot-Lab

# HP desktops
ansible-playbook playbooks/provision_clone.yml \
  -e vm_oem_profile=hp-elitedesk-800 \
  -e vm_count=10
```

Each VM is cloned from the template, given a unique SMBIOS identity matching the OEM profile, booted, and has its hardware hash captured. CSVs are saved to `output/hashes/`.

### Upload hashes to Intune

```bash
# Via Ansible (with async progress polling)
ansible-playbook playbooks/upload_hashes.yml --ask-vault-pass

# Or directly via PowerShell (real-time output)
export ENTRA_APP_ID="your-app-id"
export ENTRA_TENANT_ID="your-tenant-id"
export ENTRA_APP_SECRET="your-secret"
export HASH_DIR="output/hashes"
pwsh -NonInteractive -File scripts/upload_hashes.ps1
```

### Other playbooks

```bash
# Provision from ISO (no template needed, slower)
ansible-playbook playbooks/provision_iso.yml -e vm_oem_profile=lenovo-t14

# Re-run inject + hash capture on an existing VM
ansible-playbook playbooks/retry_inject_hash.yml -e vm_vmid=106 -e vm_name=autopilot-106
```

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

Each profile sets SMBIOS type 1 fields (manufacturer, product, family, SKU) and generates a manufacturer-appropriate serial number prefix (Lenovo=PF, Dell=SVC, HP=CZC, Microsoft=MSF).

## Variables Reference

Key variables in `inventory/group_vars/all/vars.yml` (all overridable via `-e`):

| Variable | Default | Description |
|----------|---------|-------------|
| `proxmox_host` | — | Proxmox host IP |
| `proxmox_node` | — | Proxmox node name |
| `proxmox_storage` | `ssdpool` | Storage for VM disks |
| `proxmox_bridge` | `vmbr0` | Network bridge |
| `proxmox_windows_iso` | — | Windows ISO (Proxmox notation) |
| `proxmox_virtio_iso` | — | VirtIO ISO path |
| `proxmox_answer_iso` | — | Unattended answer ISO path |
| `proxmox_template_vmid` | — | Template VMID for clone provisioning |
| `vm_oem_profile` | `generic-desktop` | OEM profile key |
| `vm_cores` | `2` | CPU cores |
| `vm_memory_mb` | `4096` | Memory in MB |
| `vm_disk_size_gb` | `64` | Disk size in GB |
| `vm_count` | `1` | Number of VMs to create |
| `vm_name_prefix` | `autopilot` | VM name prefix |
| `vm_group_tag` | — | Autopilot group tag |
| `autopilot_skip` | `true` | Skip Autopilot JSON injection |
| `capture_hardware_hash` | `true` | Capture hardware hash |
| `hash_output_dir` | `output/hashes` | Where CSVs are saved on controller |

## Project Structure

```
.
├── autopilot-proxmox/
│   ├── ansible.cfg
│   ├── filter_plugins/
│   │   └── smbios.py                  # Jinja2 filters: SMBIOS, serial, identity
│   ├── files/
│   │   ├── oem_profiles.yml           # 13 OEM hardware profiles
│   │   ├── autounattend.xml           # Unattended Windows install answer file
│   │   ├── AutopilotConfigurationFile.json  # Autopilot profile (optional)
│   │   └── Get-WindowsAutopilotInfo.ps1     # Hash capture script (bundled into VMs)
│   ├── inventory/
│   │   ├── hosts.yml
│   │   └── group_vars/all/
│   │       ├── vars.yml               # Configuration
│   │       └── vault.yml.example      # Credentials template
│   ├── roles/
│   │   ├── common/tasks/              # Reusable: guest_exec, file_write, wait_agent, wait_task
│   │   ├── proxmox_vm_iso/            # Create VM from ISO
│   │   ├── proxmox_vm_clone/          # Clone from template + reconfigure SMBIOS
│   │   ├── proxmox_template_builder/  # ISO install + sysprep + templatize
│   │   ├── autopilot_inject/          # Push Autopilot JSON into guest (optional)
│   │   └── hash_capture/              # Capture + retrieve hardware hash CSV
│   ├── playbooks/
│   │   ├── build_template.yml         # One-time template creation
│   │   ├── provision_clone.yml        # Clone + hash capture (main workflow)
│   │   ├── provision_iso.yml          # ISO-based provisioning (slower)
│   │   ├── upload_hashes.yml          # Upload CSVs to Intune
│   │   └── retry_inject_hash.yml      # Re-run on existing VMs
│   ├── scripts/
│   │   ├── create_answer_iso.sh       # Build the autounattend ISO on Proxmox
│   │   └── upload_hashes.ps1          # PowerShell script for Intune upload
│   └── tests/
│       └── test_smbios_filter.py      # 22 pytest tests
├── Get-WindowsAutopilotInfo.ps1       # Microsoft's official hash capture tool
└── Old_Legacy_Powershell_Code/        # Original APHVTools module (reference)
```

## Testing

```bash
cd autopilot-proxmox
python3 -m venv .venv && source .venv/bin/activate
pip install pytest ansible ansible-lint

pytest tests/ -v                                 # 22 unit tests
ansible-playbook --syntax-check playbooks/*.yml  # Syntax validation
ansible-lint playbooks/ roles/                   # Lint (production profile)
```

## How the Unattended Install Works

The `autounattend.xml` answer file is mounted as a separate tiny ISO (`autounattend.iso`) alongside the Windows ISO and VirtIO ISO. When the VM boots:

1. OVMF starts, Ansible sends a keypress to boot from CD
2. Windows Setup starts and automatically finds `autounattend.xml` on the mounted ISO
3. **WindowsPE pass**: Creates GPT partitions (EFI + MSR + Windows), loads VirtIO SCSI and network drivers from the VirtIO ISO (scans drives D: through F:), selects Windows 11 Enterprise
4. **Specialize pass**: Sets computer name and timezone
5. **OOBE pass**: Skips all prompts, creates Administrator account, auto-logs in once
6. **FirstLogonCommands**: Installs the QEMU guest agent MSI from the VirtIO ISO, starts the service, enables RDP
7. Ansible detects the guest agent and proceeds with hash capture or sysprep

The stock Windows ISO is never modified.
