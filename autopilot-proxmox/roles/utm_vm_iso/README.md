# utm_vm_iso

Thin wrapper that creates and starts a one-off UTM (macOS/ARM64) VM from a skeleton bundle and a Windows installer ISO.  Mirrors `proxmox_vm_iso` but for the UTM hypervisor.

**This role does NOT build templates.**  Use `utm_template_builder` when you want a sysprepped, reusable image.  Use this role for ad-hoc installs (e.g., a developer VM or a throwaway test machine).

## Overview

The role reuses `utm_template_builder`'s `create_bundle.yml` and `customize_plist.yml` task files (via `include_tasks`) to avoid code duplication, then immediately starts the VM.  There is no OOBE pause or sysprep step.

## Required variables

| Variable | Description |
|---|---|
| `vm_os_kind` | `windows11` or `windows_server` |
| `vm_name` | UTM display name and `.utm` bundle directory stem |
| `utm_iso_name` | ISO filename inside `utm_iso_dir` |

## Optional variables (defaults in `defaults/main.yml`)

| Variable | Default | Description |
|---|---|---|
| `vm_cpu_cores` | `4` | vCPU count |
| `vm_memory_mb` | `8192` | RAM in MB |
| `vm_disk_gb` | `80` | System disk size in GB |
| `utm_overwrite_bundle` | `false` | Overwrite an existing bundle at the destination |

## Global variables (from `inventory/group_vars/all/vars.yml`)

| Variable | Default |
|---|---|
| `utm_iso_dir` | `~/UTM-ISOs` |
| `utm_skeleton_dir` | `{{ playbook_dir }}/../assets/utm-templates` |
| `utm_qemu_img_path` | `/opt/homebrew/bin/qemu-img` |
| `utm_documents_dir` | `~/Library/Containers/com.utmapp.UTM/Data/Documents` |
| `utm_utmctl_path` | `/Applications/UTM.app/Contents/MacOS/utmctl` |

## Usage

```bash
ansible-playbook playbooks/provision_utm_iso.yml
```

Configure `utm_iso_targets` in your vars or pass via `-e`:

```yaml
utm_iso_targets:
  - vm_name: my-windows-vm
    vm_os_kind: windows11
    utm_iso_name: "Win11_ARM64.iso"
```
