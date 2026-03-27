# autopilot-proxmox — Ansible Technical Reference

See the [root README](../README.md) for full documentation, setup instructions, and usage examples.

## Playbooks

| Playbook | Purpose | Key Variables |
|----------|---------|--------------|
| `build_template.yml` | Create template from ISO (one-time) | `vm_oem_profile` |
| `provision_clone.yml` | Clone + hash capture (main workflow) | `vm_oem_profile`, `vm_count`, `vm_group_tag` |
| `provision_iso.yml` | ISO-based provisioning (slower) | `vm_oem_profile`, `vm_count` |
| `upload_hashes.yml` | Upload CSVs to Intune | `vault_entra_*` credentials |
| `retry_inject_hash.yml` | Re-run on existing VM | `vm_vmid`, `vm_name` |

## Roles

| Role | Purpose | Replaces (PowerShell) |
|------|---------|----------------------|
| `proxmox_vm_iso` | Create VM from ISO with UEFI/Q35/VirtIO/vTPM | `New-ProxmoxDevice.ps1` |
| `proxmox_vm_clone` | Clone from template, reconfigure SMBIOS, resize disk | `New-ProxmoxCloneDevice.ps1` |
| `proxmox_template_builder` | ISO install + sysprep + convert to template | Orchestrator workflow |
| `autopilot_inject` | Push Autopilot JSON into guest via guest agent | `Publish-ProxmoxAutoPilotConfig.ps1` |
| `hash_capture` | Push scripts, execute, retrieve hash CSV | `Publish-ProxmoxHashCaptureScript.ps1` + `Get-ProxmoxVMHardwareHash.ps1` |
| `common` | Reusable tasks: guest_exec, file_write, wait_agent, wait_task | `Invoke-ProxmoxGuestCommand.ps1`, `Wait-ProxmoxGuestAgent.ps1` |

## Custom Jinja2 Filters

`filter_plugins/smbios.py` provides 4 filters:

| Filter | Purpose |
|--------|---------|
| `proxmox_smbios1` | Build base64-encoded SMBIOS1 string (UUID stays plain) |
| `proxmox_disk_serial` | Inject/replace serial in disk config string |
| `generate_serial_number` | Manufacturer-prefixed serial (PF/SVC/CZC/MSF/LAB + 8 hex) |
| `generate_vm_identity` | UUID4 + disk serial `APHV{vmid:06d}{uuid_hex[:10]}` |

## Proxmox API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/cluster/nextid` | GET | Get next available VMID |
| `/nodes/{node}/qemu` | POST | Create VM |
| `/nodes/{node}/qemu/{vmid}/clone` | POST | Clone VM |
| `/nodes/{node}/qemu/{vmid}/config` | GET/PUT | Read/update VM config |
| `/nodes/{node}/qemu/{vmid}/resize` | PUT | Resize disk |
| `/nodes/{node}/qemu/{vmid}/status/start` | POST | Start VM |
| `/nodes/{node}/qemu/{vmid}/status/current` | GET | Check VM status |
| `/nodes/{node}/qemu/{vmid}/template` | POST | Convert to template |
| `/nodes/{node}/qemu/{vmid}/monitor` | POST | Send keypress (sendkey) |
| `/nodes/{node}/qemu/{vmid}/agent/ping` | POST | Check guest agent |
| `/nodes/{node}/qemu/{vmid}/agent/exec` | POST | Execute command in guest |
| `/nodes/{node}/qemu/{vmid}/agent/exec-status` | GET | Poll command status |
| `/nodes/{node}/qemu/{vmid}/agent/file-write` | POST | Write file to guest |
| `/nodes/{node}/tasks/{upid}/status` | GET | Poll task completion |

## PVE 9 Permissions Required

```
VM.Allocate, VM.Clone, VM.Config.CPU, VM.Config.CDROM, VM.Config.Cloudinit,
VM.Config.Disk, VM.Config.HWType, VM.Config.Memory, VM.Config.Network,
VM.Config.Options, VM.Audit, VM.PowerMgmt, VM.Console,
VM.Snapshot, VM.Snapshot.Rollback,
VM.GuestAgent.Audit, VM.GuestAgent.FileRead, VM.GuestAgent.FileWrite,
VM.GuestAgent.FileSystemMgmt, VM.GuestAgent.Unrestricted,
Datastore.AllocateSpace, Datastore.Audit,
Sys.Audit, Sys.Modify, SDN.Use
```

ACLs must be applied to `/`, storage paths, and SDN zones.

## Testing

```bash
pytest tests/ -v                                 # 22 unit tests for filter plugin
ansible-playbook --syntax-check playbooks/*.yml  # Syntax validation
ansible-lint playbooks/ roles/                   # Lint (production profile)
```
