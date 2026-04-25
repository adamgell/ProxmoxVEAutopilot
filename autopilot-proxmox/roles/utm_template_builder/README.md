# utm_template_builder

Builds a UTM (macOS/ARM64) template VM from a Windows installer ISO. Mirrors `proxmox_template_builder` but for the UTM hypervisor.

## Overview

UTM exposes a `make new virtual machine` command in its AppleScript dictionary (see `Scripting/UTM.sdef` in [utmapp/UTM](https://github.com/utmapp/UTM)). This role drives that API via `osascript`, which lets UTM itself author a fully schema-valid `config.plist` (no hand-built skeleton, no `plutil` schema-key inserts).

The flow is:

1. Call UTM's `make new virtual machine with properties {backend:qemu, configuration:{...}}` via `osascript`. UTM creates the bundle in its Documents directory, writes a complete `config.plist`, generates a blank qcow2 system disk, and registers the VM with `utmctl` atomically.
2. Read the UTM-generated drive identifiers back from `config.plist` via `PlistBuddy`.
3. Copy the installer ISO into the bundle's `Data/` directory.
4. Insert `Drive[0].ImageName` via `plutil` (the only field UTM does **not** populate from the AppleScript `source:` parameter).
5. Start the VM so the operator (or unattended `autounattend.xml`) can complete Windows Setup.
6. (On resume) Run Sysprep to generalise the image as a reusable template.

## Two-phase execution

### Phase 1 — Build (first run)

```bash
ansible-playbook playbooks/build_utm_template.yml
```

Creates the bundle, starts the VM, prints operator instructions, and **intentionally fails** to pause execution. Complete the Windows OOBE in UTM (or let `autounattend.xml` do it).

### Phase 2 — Finalise (re-run after OOBE)

```bash
ansible-playbook playbooks/build_utm_template.yml -e utm_build_resume=true
```

Runs Windows Sysprep (`/generalize /oobe /shutdown`), polls until the VM stops, writes the `autopilot-template.ready` marker.

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
| `utm_overwrite_bundle` | `false` | Delete an existing VM with the same name before creating |

## Global variables (from `inventory/group_vars/all/vars.yml`)

| Variable | Default |
|---|---|
| `utm_iso_dir` | `~/UTM-ISOs` |
| `utm_documents_dir` | `~/Library/Containers/com.utmapp.UTM/Data/Documents` |
| `utm_utmctl_path` | `/Applications/UTM.app/Contents/MacOS/utmctl` |

`utm_skeleton_dir` and `utm_qemu_img_path` are no longer used by this role and may be removed from inventory.

## Task files

| File | Purpose |
|---|---|
| `tasks/main.yml` | Orchestrator; routes between phase 1 and phase 2 |
| `tasks/create_bundle.yml` | Calls UTM `make` via AppleScript, copies ISO into Data/ |
| `tasks/customize_plist.yml` | Inserts `Drive[0].ImageName` via `plutil` (sole post-`make` patch) |
| `tasks/start_and_wait.yml` | Starts VM, polls for `started`, halts for operator |
| `tasks/sysprep_finalize.yml` | Runs Sysprep, polls for `stopped`, emits final message |

## Why no skeleton bundle?

Earlier revisions of this role copied a hand-maintained `assets/utm-templates/<os>-arm64.utm/` skeleton and patched its `config.plist` via `plutil`. That approach was abandoned because:

- UTM's `config.plist` schema (defined in `UTMQemuConfiguration.swift`) requires top-level `System`, `Sharing`, `Serial`, `Sound`, `Backend`, and `ConfigurationVersion` keys; missing any of them triggers a "Cannot import this VM" rejection by UTM at registration time.
- `Drive[].Identifier` UUIDs must be uppercase.
- A bad bundle that registers but is invalid can be silently aliased by UTM's `Registry.<UUID>.Package.Path` to an existing bundle, leading to data loss when the user later "deletes" it via the sidebar.

Letting UTM's own code construct the bundle eliminates every one of these failure modes by construction.
