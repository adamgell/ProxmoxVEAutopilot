# utm_template_builder

Builds a UTM (macOS/ARM64) template VM from a skeleton bundle and a Windows installer ISO. Mirrors `proxmox_template_builder` but for the UTM hypervisor.

## Overview

UTM has no native "create VM" CLI primitive, so this role materialises a valid `.utm` bundle on disk inside UTM's Documents directory by:

1. Copying a skeleton bundle from `assets/utm-templates/`
2. Patching `config.plist` with fresh UUIDs, the VM name, ISO reference, disk filename, CPU and RAM sizing
3. Creating a blank qcow2 system disk via `qemu-img`
4. Copying the installer ISO into the bundle's `Data/` directory
5. Starting the VM so the operator can complete the Windows OOBE
6. (On resume) Running Sysprep to generalise the image as a reusable template

## Two-phase execution

### Phase 1 — Build (first run)

```bash
ansible-playbook playbooks/build_utm_template.yml
```

The playbook creates the bundle, starts the VM, prints operator instructions, and **intentionally fails** to pause execution.  Complete the Windows OOBE in the UTM window.

### Phase 2 — Finalise (re-run after OOBE)

```bash
ansible-playbook playbooks/build_utm_template.yml -e utm_build_resume=true
```

Runs Windows Sysprep (`/generalize /oobe /shutdown`), polls until the VM stops, and prints the final "template ready" message.

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
| `utm_build_wait_mode` | `manual` | `manual` = operator drives OOBE |
| `utm_build_resume` | `false` | Set `true` on re-run to trigger sysprep |
| `utm_overwrite_bundle` | `false` | Overwrite an existing bundle at the destination |

## Global variables (from `inventory/group_vars/all/vars.yml`)

| Variable | Default |
|---|---|
| `utm_iso_dir` | `~/UTM-ISOs` |
| `utm_skeleton_dir` | `{{ playbook_dir }}/../assets/utm-templates` |
| `utm_qemu_img_path` | `/opt/homebrew/bin/qemu-img` |
| `utm_documents_dir` | `~/Library/Containers/com.utmapp.UTM/Data/Documents` |
| `utm_utmctl_path` | `/Applications/UTM.app/Contents/MacOS/utmctl` |

## Task files

| File | Purpose |
|---|---|
| `tasks/main.yml` | Orchestrator; routes between phase 1 and phase 2 |
| `tasks/create_bundle.yml` | Copies skeleton, generates UUIDs, creates qcow2, copies ISO |
| `tasks/customize_plist.yml` | Patches `config.plist` via macOS `plutil` |
| `tasks/start_and_wait.yml` | Starts VM, polls for `started`, halts for operator |
| `tasks/sysprep_finalize.yml` | Runs Sysprep, polls for `stopped`, emits final message |

## Skeleton bundles

Skeletons are stored at `assets/utm-templates/<os>-arm64.utm/` with placeholder UUIDs:

- `Information.UUID` → `00000000-0000-0000-0000-000000000000`
- `Drive[0].Identifier` → `00000000-0000-0000-0000-000000000001` (installer CD)
- `Drive[1].Identifier` → `00000000-0000-0000-0000-000000000002` (system disk)
- `Drive[2].Identifier` → `00000000-0000-0000-0000-000000000003` (answer CD, reserved)

This role replaces all placeholders at runtime and wires real filenames.
