# Proxmox VE Autopilot

Automated provisioning of Windows VMs on Proxmox with OEM-accurate SMBIOS fields, Windows Autopilot configuration injection, and hardware hash capture for Intune registration.

## Overview

This project creates Windows VMs on Proxmox that appear as real OEM hardware to Windows Autopilot. Each VM gets manufacturer-specific SMBIOS fields (Lenovo, Dell, HP, Microsoft Surface, or generic), a unique serial number, and a hardware identity — enabling Autopilot enrollment and testing without physical devices.

### What it does

1. **Creates Windows VMs** on Proxmox with UEFI/Q35/VirtIO/vTPM and OEM SMBIOS fields
2. **Injects Autopilot configuration** into the guest via QEMU guest agent (no WinRM)
3. **Captures hardware hashes** from each VM and saves them as CSV files
4. **Optionally uploads hashes** to Intune for Autopilot device registration

### Three provisioning paths

| Path | Description |
|------|-------------|
| **ISO** | Create a new VM from a Windows ISO, inject Autopilot config, capture hash |
| **Template Builder** | Create VM from ISO, run sysprep, convert to reusable Proxmox template |
| **Clone** | Full-clone from a template, reconfigure SMBIOS/identity, inject Autopilot, capture hash |

## Project Structure

```
.
├── autopilot-proxmox/          # Ansible project (the main implementation)
│   ├── roles/                  # 5 Ansible roles
│   ├── playbooks/              # 4 playbooks (provision, build template, upload)
│   ├── filter_plugins/         # Custom Jinja2 filters for SMBIOS encoding
│   ├── files/                  # OEM profiles, autounattend.xml, bundled scripts
│   ├── scripts/                # Helper scripts (answer ISO creation)
│   ├── inventory/              # Hosts, variables, vault
│   ├── tests/                  # pytest tests for filter plugin
│   └── README.md               # Detailed Ansible usage documentation
├── Get-WindowsAutopilotInfo.ps1  # Microsoft's hardware hash capture script
└── Old_Legacy_Powershell_Code/   # Original APHVTools PowerShell module (reference)
```

## Prerequisites

- **Controller**: Linux VM/CT on the Proxmox host (or any machine with network access) with Ansible 2.14+ and Python 3.9+
- **Proxmox VE 9.x**: API token with proper permissions (see setup below)
- **Windows ISO**: Stock Windows 11 Enterprise ISO (no modification needed)
- **VirtIO ISO**: `virtio-win.iso` for drivers and guest agent

## Proxmox Setup

### 1. Create API token

Run these commands on your **Proxmox host shell**:

```bash
# Create a dedicated role with all required PVE 9 privileges
pveum role add AutopilotProvisioner -privs VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.CDROM,VM.Config.Cloudinit,VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Audit,VM.PowerMgmt,VM.Console,VM.Snapshot,VM.Snapshot.Rollback,VM.GuestAgent.Audit,VM.GuestAgent.FileRead,VM.GuestAgent.FileWrite,VM.GuestAgent.FileSystemMgmt,VM.GuestAgent.Unrestricted,Datastore.AllocateSpace,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use

# Create a service account (no password needed for token-only access)
pveum user add autopilot@pve --comment "Ansible Autopilot provisioning service account"

# Create an API token (privsep=0 means token inherits user permissions)
pveum user token add autopilot@pve ansible --privsep=0 --comment "Ansible automation token"
# Save the full-tokenid and value from the output!

# Assign the role — apply to root path, storage, and SDN zone
pveum acl modify / -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/ssdpool -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /sdn/zones/localnetwork -user autopilot@pve -role AutopilotProvisioner
```

Adjust the storage name (`ssdpool`) and SDN zone (`localnetwork`) to match your environment. If you use multiple storage backends, add an ACL for each:

```bash
pveum acl modify /storage/local-lvm -user autopilot@pve -role AutopilotProvisioner
pveum acl modify /storage/isos -user autopilot@pve -role AutopilotProvisioner
```

### 2. Create the unattended answer ISO

The project includes an `autounattend.xml` that automates the entire Windows install (partitioning, driver loading, admin account, guest agent install). It gets mounted as a separate tiny ISO — your stock Windows ISO stays untouched.

```bash
# From your workstation — copy the answer file to the Proxmox host
scp autopilot-proxmox/files/autounattend.xml root@<PROXMOX_IP>:/tmp/autounattend.xml

# SSH into the Proxmox host
ssh root@<PROXMOX_IP>

# Install genisoimage if needed
apt-get install -y genisoimage

# Build the ISO into your ISO storage
# First, find the right path:
STORAGE_PATH=$(pvesm path isos:iso/autounattend.iso | sed 's|/autounattend.iso$||')

# Create the ISO
mkdir -p /tmp/answeriso
cp /tmp/autounattend.xml /tmp/answeriso/autounattend.xml
genisoimage -o "${STORAGE_PATH}/autounattend.iso" -J -r -V "OEMDRV" /tmp/answeriso/
rm -rf /tmp/answeriso /tmp/autounattend.xml
```

If `isos` storage is local to each node, repeat on every node you'll provision VMs on.

### 3. Upload ISOs to Proxmox

Ensure these ISOs are in your Proxmox ISO storage:
- Windows 11 Enterprise ISO
- `virtio-win-0.1.285.iso` (or latest)
- `autounattend.iso` (created above)

## Quick Start

```bash
cd autopilot-proxmox

# 1. Edit inventory/group_vars/all/vars.yml with your Proxmox details
#    (host, node, storage, bridge, ISO paths)

# 2. Copy and fill in your credentials
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
# Edit vault.yml with your API token ID and secret

# 3. Optionally encrypt the vault
ansible-vault encrypt inventory/group_vars/all/vault.yml

# 4. Build a Windows template (one-time)
ansible-playbook playbooks/build_template.yml --ask-vault-pass \
  -e vm_oem_profile=generic-desktop

# 5. Clone VMs from the template
ansible-playbook playbooks/provision_clone.yml --ask-vault-pass \
  -e vm_oem_profile=lenovo-t14 -e vm_count=5
```

## Architecture

All Proxmox communication uses the REST API via Ansible's `uri` module. All in-guest operations (file writes, script execution, hash capture) use the QEMU guest agent — no WinRM, no SSH, no network dependency on the guest.

```
Controller ──REST API──> Proxmox Host
                            │
                      QEMU Guest Agent
                            │
                      Windows VM (guest)
```

The unattended install flow:
1. VM boots from Windows ISO
2. Windows Setup finds `autounattend.xml` on the answer ISO
3. VirtIO drivers loaded from VirtIO ISO (scanned on D:, E:, F:)
4. Windows installs, creates admin account, installs QEMU guest agent
5. Ansible detects guest agent and proceeds with Autopilot injection / hash capture

## OEM Profiles

13 built-in hardware profiles for SMBIOS emulation:

| Manufacturer | Models |
|-------------|--------|
| Lenovo | ThinkStation P520, ThinkPad T14 Gen 4, ThinkPad X1 Carbon Gen 11 |
| Dell | OptiPlex 7090, Latitude 5540, XPS 15 9530 |
| HP | EliteDesk 800 G8 SFF, EliteBook 840 G10, ZBook Fury 16 G10 |
| Microsoft | Surface Pro 10, Surface Laptop 6 |
| Generic | Virtual Desktop, Virtual Laptop |

## Testing

```bash
cd autopilot-proxmox

# Set up test environment
python3 -m venv .venv && source .venv/bin/activate
pip install pytest ansible ansible-lint

# Run all checks
pytest tests/ -v                                 # 22 unit tests for filter plugin
ansible-playbook --syntax-check playbooks/*.yml  # YAML syntax validation
ansible-lint playbooks/ roles/                   # Lint (production profile)
```

## Legacy PowerShell

The `Old_Legacy_Powershell_Code/` directory contains the original APHVTools PowerShell module that this project was converted from. It is kept for reference. The `Get-WindowsAutopilotInfo.ps1` script in the root is Microsoft's official hardware hash capture tool, which is bundled into guest VMs during hash capture.
