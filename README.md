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
│   ├── files/                  # OEM profiles, bundled scripts, Autopilot config
│   ├── inventory/              # Hosts, variables, vault
│   ├── tests/                  # pytest tests for filter plugin
│   └── README.md               # Detailed Ansible usage documentation
├── Get-WindowsAutopilotInfo.ps1  # Microsoft's hardware hash capture script
└── Old_Legacy_Powershell_Code/   # Original APHVTools PowerShell module (reference)
```

## Quick Start

See [`autopilot-proxmox/README.md`](autopilot-proxmox/README.md) for full setup and usage instructions.

```bash
cd autopilot-proxmox

# 1. Edit inventory/group_vars/all/vars.yml with your Proxmox details
# 2. Encrypt secrets
ansible-vault encrypt inventory/group_vars/all/vault.yml

# 3. Provision VMs
ansible-playbook playbooks/provision_clone.yml --ask-vault-pass \
  -e proxmox_template_vmid=9000 -e vm_oem_profile=lenovo-t14 -e vm_count=5
```

## Architecture

All Proxmox communication uses the REST API via Ansible's `uri` module. All in-guest operations (file writes, script execution, hash capture) use the QEMU guest agent — no WinRM, no SSH, no network dependency on the guest.

```
Controller (Linux VM/CT) ──REST API──> Proxmox Host
                                          │
                                    QEMU Guest Agent
                                          │
                                    Windows VM (guest)
```

## OEM Profiles

13 built-in hardware profiles for SMBIOS emulation:

| Manufacturer | Models |
|-------------|--------|
| Lenovo | ThinkStation P520, ThinkPad T14 Gen 4, ThinkPad X1 Carbon Gen 11 |
| Dell | OptiPlex 7090, Latitude 5540, XPS 15 9530 |
| HP | EliteDesk 800 G8 SFF, EliteBook 840 G10, ZBook Fury 16 G10 |
| Microsoft | Surface Pro 10, Surface Laptop 6 |
| Generic | Virtual Desktop, Virtual Laptop |

## Prerequisites

- **Controller**: Linux VM/CT on the Proxmox host with Ansible 2.14+ and Python 3.9+
- **Proxmox**: API token with VM management permissions
- **Windows ISO**: Custom ISO with VirtIO storage/network drivers baked in
- **VirtIO ISO**: `virtio-win.iso` for guest agent/tools

## Testing

```bash
cd autopilot-proxmox

# Set up test environment
python3 -m venv .venv && source .venv/bin/activate
pip install pytest ansible ansible-lint

# Run all checks
pytest tests/ -v                              # 22 unit tests for filter plugin
ansible-playbook --syntax-check playbooks/*.yml  # YAML syntax validation
ansible-lint playbooks/ roles/                # Lint (production profile)
```

## Legacy PowerShell

The `Old_Legacy_Powershell_Code/` directory contains the original APHVTools PowerShell module that this project was converted from. It is kept for reference. The `Get-WindowsAutopilotInfo.ps1` script in the root is Microsoft's official hardware hash capture tool, which is bundled into guest VMs during hash capture.
