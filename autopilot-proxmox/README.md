# Autopilot Proxmox — Ansible Edition

Ansible project for provisioning Windows VMs on Proxmox with OEM-accurate SMBIOS fields, Windows Autopilot configuration injection, and hardware hash capture for Intune registration.

Converted from the APHVTools PowerShell module.

## Prerequisites

- **Controller**: Linux VM/CT on the Proxmox host with:
  - Ansible 2.14+
  - Python 3.9+
  - `pwsh` (PowerShell Core) — only needed for `upload_hashes.yml`
- **Proxmox**: API token with VM creation/management permissions
- **Windows ISO**: Custom ISO with VirtIO storage/network drivers baked in
- **VirtIO ISO**: `virtio-win.iso` for guest agent/tools installation

## Quick Start

### 1. Configure inventory

Edit `inventory/group_vars/all/vars.yml` with your Proxmox host, storage, and networking:

```yaml
proxmox_host: "192.168.1.100"
proxmox_node: "pve"
proxmox_storage: "local-lvm"
proxmox_bridge: "vmbr0"
proxmox_windows_iso: "local:iso/Win11_24H2.iso"
proxmox_virtio_iso: "local:iso/virtio-win.iso"
```

### 2. Encrypt secrets

```bash
ansible-vault encrypt inventory/group_vars/all/vault.yml
```

Edit the vault with your Proxmox API token:

```bash
ansible-vault edit inventory/group_vars/all/vault.yml
```

### 3. Prepare Autopilot config

Replace `files/AutopilotConfigurationFile.json` with your tenant's Autopilot profile JSON. You can fetch this using the existing PowerShell `Get-AutopilotPolicy.ps1` function.

### 4. Run a playbook

**Provision VMs from ISO:**
```bash
ansible-playbook playbooks/provision_iso.yml --ask-vault-pass \
  -e vm_oem_profile=lenovo-t14 -e vm_count=3
```

**Build a template:**
```bash
ansible-playbook playbooks/build_template.yml --ask-vault-pass
```

**Provision VMs from template clone:**
```bash
ansible-playbook playbooks/provision_clone.yml --ask-vault-pass \
  -e proxmox_template_vmid=9000 -e vm_oem_profile=dell-latitude-5540 -e vm_count=5
```

**Upload hardware hashes to Intune:**
```bash
ansible-playbook playbooks/upload_hashes.yml --ask-vault-pass
```

## Provisioning Paths

| Path | Playbook | Use Case |
|------|----------|----------|
| **ISO** | `provision_iso.yml` | New VM from Windows ISO, inject Autopilot, capture hash |
| **Template Builder** | `build_template.yml` | Create VM from ISO, sysprep, convert to Proxmox template |
| **Clone** | `provision_clone.yml` | Clone from template, reconfigure SMBIOS, inject Autopilot, capture hash |

## OEM Profiles

13 built-in profiles in `files/oem_profiles.yml`:

| Key | Hardware |
|-----|----------|
| `lenovo-p520` | ThinkStation P520 |
| `lenovo-t14` | ThinkPad T14 Gen 4 |
| `lenovo-x1carbon` | ThinkPad X1 Carbon Gen 11 |
| `dell-optiplex-7090` | OptiPlex 7090 |
| `dell-latitude-5540` | Latitude 5540 |
| `dell-xps-15` | XPS 15 9530 |
| `hp-elitedesk-800` | EliteDesk 800 G8 SFF |
| `hp-elitebook-840` | EliteBook 840 G10 |
| `hp-zbook-g10` | ZBook Fury 16 G10 |
| `surface-pro-10` | Surface Pro 10 |
| `surface-laptop-6` | Surface Laptop 6 |
| `generic-desktop` | Virtual Desktop |
| `generic-laptop` | Virtual Laptop |

## Architecture

All Proxmox communication uses the REST API via Ansible's `uri` module. All in-guest operations use the QEMU guest agent — no WinRM, no SSH into Windows VMs.

```
Controller (Linux) ──REST API──> Proxmox Host
                                    │
                              QEMU Guest Agent
                                    │
                              Windows VM (guest)
```

### Project Structure

```
autopilot-proxmox/
├── ansible.cfg                    # roles_path, inventory, filter_plugins
├── filter_plugins/
│   └── smbios.py                  # Jinja2 filters: SMBIOS, serial, identity
├── files/
│   ├── oem_profiles.yml           # 13 OEM hardware profiles
│   ├── Get-WindowsAutopilotInfo.ps1
│   └── AutopilotConfigurationFile.json
├── inventory/
│   ├── hosts.yml                  # localhost (connection: local)
│   └── group_vars/all/
│       ├── vars.yml               # All non-secret configuration
│       └── vault.yml              # Encrypted secrets (API token, Entra creds)
├── roles/
│   ├── common/tasks/              # Reusable: guest_exec, file_write, wait_agent, wait_task
│   ├── proxmox_vm_iso/            # Create VM from ISO
│   ├── proxmox_vm_clone/          # Clone from template + reconfigure
│   ├── proxmox_template_builder/  # ISO install + sysprep + templatize
│   ├── autopilot_inject/          # Push Autopilot JSON into guest
│   └── hash_capture/              # Capture + retrieve hardware hash CSV
├── playbooks/
│   ├── provision_iso.yml
│   ├── provision_clone.yml
│   ├── build_template.yml
│   └── upload_hashes.yml
├── tests/
│   └── test_smbios_filter.py      # 22 pytest tests for filter plugin
└── README.md
```

### Custom Jinja2 Filters

The `filter_plugins/smbios.py` module provides 4 filters used throughout the roles:

| Filter | Replaces (PowerShell) | Purpose |
|--------|----------------------|---------|
| `proxmox_smbios1` | `Merge-ProxmoxSmbios1.ps1` | Build base64-encoded SMBIOS1 string |
| `proxmox_disk_serial` | `Set-ProxmoxDiskSerial.ps1` | Inject serial into disk config string |
| `generate_serial_number` | `New-ProxmoxSerialNumber.ps1` | Manufacturer-prefixed serial (PF/SVC/CZC/MSF/LAB) |
| `generate_vm_identity` | `New-ProxmoxVmIdentity.ps1` | UUID4 + disk serial `APHV{vmid}{uuid_prefix}` |

## Testing

### Set up test environment

```bash
cd autopilot-proxmox
python3 -m venv .venv
source .venv/bin/activate
pip install pytest ansible ansible-lint
```

### 1. Unit tests (filter plugin)

```bash
pytest tests/test_smbios_filter.py -v
```

Validates all 4 filter functions against the original PowerShell behavior:
- SMBIOS1 string building with base64 encoding (UUID stays plain)
- Disk serial injection and replacement
- Manufacturer prefix mapping (Lenovo/Dell/HP/Microsoft/default)
- VM identity generation (UUID format, disk serial format, padding, uniqueness)

### 2. Ansible syntax check

```bash
ansible-playbook --syntax-check playbooks/provision_iso.yml
ansible-playbook --syntax-check playbooks/provision_clone.yml
ansible-playbook --syntax-check playbooks/build_template.yml
ansible-playbook --syntax-check playbooks/upload_hashes.yml
```

### 3. Ansible lint

```bash
ansible-lint playbooks/ roles/
```

Configured via `.ansible-lint` — skips `var-naming[no-role-prefix]` (internal `_` prefixed facts are shared across roles by design) and excludes `_provision_*_vm.yml` fragments.

### 4. Live smoke test (requires Proxmox)

```bash
# Clone 1 VM and verify the full pipeline
ansible-playbook playbooks/provision_clone.yml --ask-vault-pass \
  -e proxmox_template_vmid=9000 \
  -e vm_oem_profile=lenovo-t14 \
  -e vm_count=1

# Verify:
# 1. VM appears in Proxmox UI with correct SMBIOS fields
# 2. AutopilotConfigurationFile.json exists at C:\Windows\Provisioning\Autopilot\ in guest
# 3. Hardware hash CSV appears in /opt/autopilot-proxmox/hashes/ on controller
# 4. CSV contains valid Device Serial Number and Hardware Hash columns
```

## Variables Reference

Key variables (set in `vars.yml` or via `-e`):

| Variable | Default | Description |
|----------|---------|-------------|
| `proxmox_host` | `192.168.1.100` | Proxmox host IP/hostname |
| `proxmox_node` | `pve` | Proxmox node name |
| `proxmox_storage` | `local-lvm` | Storage for VM disks |
| `proxmox_bridge` | `vmbr0` | Network bridge |
| `proxmox_windows_iso` | — | Windows ISO path (Proxmox notation) |
| `proxmox_virtio_iso` | — | VirtIO ISO path |
| `proxmox_template_vmid` | — | Template VMID for clone provisioning |
| `vm_cores` | `2` | CPU cores per VM |
| `vm_memory_mb` | `4096` | Memory in MB |
| `vm_disk_size_gb` | `64` | Disk size in GB |
| `vm_count` | `1` | Number of VMs to create |
| `vm_oem_profile` | — | OEM profile key (e.g. `lenovo-t14`) |
| `vm_name_prefix` | `autopilot` | VM name prefix |
| `vm_group_tag` | — | Autopilot group tag for hash capture |
| `autopilot_skip` | `false` | Skip Autopilot injection |
| `capture_hardware_hash` | `true` | Capture hardware hash after provisioning |
| `hash_output_dir` | `/opt/autopilot-proxmox/hashes` | Where to save hash CSVs on controller |
| `guest_agent_timeout_seconds` | `1800` | Max wait for guest agent (30 min) |
