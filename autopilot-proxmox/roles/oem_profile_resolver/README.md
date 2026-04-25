# oem_profile_resolver

Shared role that loads OEM hardware profiles and computes per-VM SMBIOS
identity facts. Used by both the Proxmox and UTM VM provisioning backends.

## Required inputs

| Variable | Source | Description |
|---|---|---|
| `vm_vmid` | caller (set before include) | Proxmox: numeric VMID string. UTM: VM name string. Both are valid identity seeds. |
| `vm_oem_profile` | vars.yml or -e | Key into `files/oem_profiles.yml`. Empty = UUID-only SMBIOS1. |
| `vm_custom_serial` | vars.yml (optional) | Override generated serial number with a fixed value. |
| `vm_serial_prefix` | vars.yml (optional) | Override the manufacturer-derived serial prefix. |

## Exported facts

| Fact | Description |
|---|---|
| `_oem_data` | Full contents of `files/oem_profiles.yml` |
| `_oem_profile` | Resolved profile dict (undefined when no profile selected) |
| `_vm_identity` | Dict with `uuid` (str) and `disk_serial` (str) |
| `_vm_serial` | Generated or custom serial number string |
| `_smbios1_string` | Proxmox-format `smbios1=` value (base64-encoded fields + uuid) |

## Backend notes

- **Proxmox**: `_smbios1_string` is passed to `proxmox_vm_clone`'s `update_config.yml`
  as the `smbios1` VM option. If a chassis-type override is in play, the clone role
  builds a separate per-VM SMBIOS binary file instead and ignores `_smbios1_string`.
- **UTM**: `_smbios1_string` is not used directly. The `utm_vm_smbios_override` role
  reads `_oem_profile`, `_vm_serial`, and `_vm_identity` to build a QEMU
  `-smbios type=1,...` argument string and injects it into `QEMU.AdditionalArguments`
  in the bundle's `config.plist`. Chassis type is injected as `-smbios type=3,type=N`
  (QEMU ARM64 supports this; Proxmox 9 does not yet expose it via the API).
