# UTM Answer ISO — Unattended Windows 11 ARM64 Template Builds

This document describes the answer-ISO subsystem that enables fully
unattended Windows 11 ARM64 template builds on macOS/ARM64 hosts running
UTM + QEMU.

---

## Table of Contents

1. [Overview](#overview)
2. [How Windows Setup discovers the answer file](#how-windows-setup-discovers-the-answer-file)
3. [ISO structure](#iso-structure)
4. [OEM profile mapping](#oem-profile-mapping)
5. [Sentinel protocol](#sentinel-protocol)
6. [Mode A — standalone template build (utm_build_win11_template.yml)](#mode-a--standalone-template-build)
7. [Mode B — template_mode clone (utm_vm_clone)](#mode-b--template_mode-clone)
8. [Settings (web UI)](#settings-web-ui)
9. [API — unattend.xml preview](#api--unattendxml-preview)
10. [Manual override instructions](#manual-override-instructions)
11. [Troubleshooting](#troubleshooting)

---

## Overview

The answer-ISO workflow automates the entire Windows 11 ARM64 installation
inside UTM without any operator interaction at the OOBE screens.

```
 macOS host
 ┌──────────────────────────────────────────────────────────┐
 │                                                          │
 │  answer_iso.py                                           │
 │  ┌────────────────────────────────────────────────┐      │
 │  │  render unattend.xml.j2  (ARM64, locale, tz)   │      │
 │  │  render firstboot.ps1.j2 (WinRM, sentinel)     │      │
 │  │  hdiutil makehybrid → AUTOUNATTEND.iso          │      │
 │  └────────────────────────────────────────────────┘      │
 │           │                                              │
 │           ▼                                              │
 │  UTM VM bundle/                                          │
 │  ├── config.plist  (Drive.0=Win ISO, Drive.2=answer ISO) │
 │  └── Data/                                               │
 │      ├── Win11_Arm64.iso                                 │
 │      └── win11-tpl-autounattend.iso  ◄── AUTOUNATTEND    │
 │                                                          │
 │  QEMU boots Win11 ISO → Setup auto-detects answer ISO    │
 │  → autounattend.xml drives silent install                │
 │  → $OEM$\$1\autopilot\ copies to C:\autopilot\           │
 │  → firstboot.ps1 runs (AutoLogon Order 2)                │
 │  → writes C:\autopilot\autopilot-firstboot.done          │
 │                                                          │
 │  Ansible polls utmctl exec for sentinel → suspends VM    │
 │  → writes {bundle}/autopilot-template.ready              │
 └──────────────────────────────────────────────────────────┘
```

---

## How Windows Setup discovers the answer file

Windows Setup (WinPE pass) scans all connected drives for a root-level
file named `autounattend.xml`. It also auto-detects media with volume
label `AUTOUNATTEND`. The answer ISO uses both mechanisms:

* The ISO 9660/Joliet volume label is set to **`AUTOUNATTEND`** via
  `hdiutil makehybrid -default-volume-name AUTOUNATTEND`.
* The root of the ISO contains `autounattend.xml`.

This is why the drive *does not* need to be the primary boot device —
Setup scans all removable/CD media regardless of boot order.

---

## ISO structure

```
autounattend.xml          ← auto-detected by Windows Setup
$OEM$/
  $1/
    autopilot/
      firstboot.ps1       → C:\autopilot\firstboot.ps1
```

`$OEM$\$1\` is a special Windows Setup staging tree: everything under
`$1\` is copied to `C:\` during text-mode setup (before first boot).

So `firstboot.ps1` arrives at `C:\autopilot\firstboot.ps1` automatically,
and is then executed by `FirstLogonCommands` Order 2 in `autounattend.xml`.

---

## OEM profile mapping

The profile dict passed to `render_arm64_unattend()` is built from
Ansible facts set by `oem_profile_resolver` and from the answer-ISO
settings in `vars.yml`:

| Profile field | Source | Notes |
|---|---|---|
| `hostname` | `vm_name` (overridable via `utm_answer_hostname`) | `*` = Windows auto-generates |
| `org_name` | `_oem_profile.manufacturer` | Empty string if no OEM profile |
| `locale` | `utm_answer_locale` | Default `en-US` |
| `timezone` | `utm_answer_timezone` | Default `Pacific Standard Time` |
| `admin_user` | `utm_answer_admin_user` | Default `Administrator` |
| `admin_pass` | `utm_answer_admin_pass` (vault) | **Required** |
| `product_key` | `utm_answer_product_key` (vault, optional) | Omit for default edition |
| `windows_edition` | `utm_answer_windows_edition` | Default `Windows 11 Pro` |
| `domain_join` | (not yet wired from Ansible) | Pass via `--profile-json` for now |

---

## Sentinel protocol

`firstboot.ps1` writes a sentinel file when it finishes:

```
C:\autopilot\autopilot-firstboot.done
```

The sentinel contains a human-readable timestamp. The Ansible playbook
polls for this file via `utmctl exec` using:

```
utmctl exec <vm_name> -- powershell.exe -Command \
  "if (Test-Path 'C:\autopilot\autopilot-firstboot.done') { 'SENTINEL_FOUND' } else { 'SENTINEL_MISSING' }"
```

The playbook retries up to **270 times at 10-second intervals (45 minutes
total)**. If the sentinel is never found within the budget the playbook
fails and the operator must inspect the VM manually.

Once the sentinel is detected:

1. The VM is suspended (`utmctl suspend <vm_name>`).
2. A marker file `{bundle}/autopilot-template.ready` is written. The
   `/vms` web UI reads this file to display a **"Template"** badge.

---

## Mode A — Standalone template build

Use `playbooks/utm_build_win11_template.yml` to build a template from
scratch. This is the recommended first-time setup path.

```bash
ansible-playbook playbooks/utm_build_win11_template.yml \
  -e vm_name=win11-arm64-template \
  -e utm_iso_name=Win11_25H2_English_Arm64.iso
```

Flow diagram:

```
utm_build_win11_template.yml
  │
  ├─ oem_profile_resolver         set vm_vmid=vm_name; load OEM profile
  │
  ├─ utm_answer_iso (role)         generate answer ISO → utm_answer_iso_path
  │
  ├─ utm_template_builder          create bundle + customize config.plist
  │   ├─ create_bundle.yml
  │   └─ customize_plist.yml
  │
  ├─ copy answer ISO → Data/
  ├─ plutil attach Drive.2
  │
  ├─ utmctl start win11-arm64-template --hide
  │
  ├─ utmctl exec ... Test-Path sentinel   ← retries up to 45 min
  │   ... (Windows installs silently) ...
  │   SENTINEL_FOUND
  │
  ├─ utmctl suspend win11-arm64-template
  │
  └─ write {bundle}/autopilot-template.ready
```

Required settings before running:

* `utm_iso_dir` — directory containing the Windows ARM64 ISO
* `utm_answer_admin_pass` — set in `vault.yml`
* `utm_skeleton_dir` — path to `assets/utm-templates/` skeletons
* `utm_utmctl_path` — path to `utmctl` binary

---

## Mode B — template_mode clone

Use `utm_vm_clone` with `template_mode: true` when you want the clone
role to both clone the template *and* trigger an unattended finalisation
pass (e.g., re-running sysprep + firstboot on a partially-configured
template).

```yaml
- name: Build finalised template clone
  ansible.builtin.include_role:
    name: utm_vm_clone
  vars:
    utm_template_vm_name: win11-arm64-base
    vm_name: win11-arm64-final
    template_mode: true
```

Flow diagram — `template_mode: true`:

```
utm_vm_clone (template_mode=true)
  │
  ├─ clone_vm.yml                   copy bundle
  ├─ oem_profile_resolver           load OEM profile
  │
  ├─ utm_answer_iso (role)           generate answer ISO
  ├─ copy + plutil attach Drive.2
  │
  ├─ start_vm.yml                   utmctl start
  │
  ├─ utmctl exec ... sentinel poll  ← 45-minute budget
  │
  ├─ utmctl suspend
  └─ write autopilot-template.ready
  (skip: SMBIOS override, network override, guest-agent wait)
```

Flow diagram — `template_mode: false` (default):

```
utm_vm_clone (template_mode=false)
  │
  ├─ clone_vm.yml
  ├─ oem_profile_resolver
  ├─ utm_vm_smbios_override
  ├─ utm_vm_network_override
  ├─ start_vm.yml
  └─ wait_guest_agent.yml           standard ready-check
  (skip: answer ISO, sentinel poll, suspend, marker)
```

---

## Settings (web UI)

The following settings are available under **Settings → UTM Answer ISO**
(only visible when `hypervisor_type = utm`):

| Setting | Key | Default |
|---|---|---|
| Enable Answer ISO | `utm_answer_iso_enabled` | `true` |
| Admin Username | `utm_answer_admin_user` | `Administrator` |
| Locale | `utm_answer_locale` | `en-US` |
| Timezone | `utm_answer_timezone` | `Pacific Standard Time` |
| Windows Edition | `utm_answer_windows_edition` | `Windows 11 Pro` |
| Answer ISO Output Directory | `utm_answer_iso_dir` | `output/answer-isos` |

Under **Settings → UTM Answer ISO Credentials** (vault-backed, values
never echoed to the browser):

| Setting | Key |
|---|---|
| Admin Password | `utm_answer_admin_pass` |
| Product Key | `utm_answer_product_key` |

---

## API — unattend.xml preview

`POST /api/utm/answer-iso/preview` renders the `autounattend.xml` for
a given profile dict *without* building an ISO. Useful for QA.

**Request body** (all fields optional):

```json
{
  "hostname":         "WIN11-TEST",
  "locale":           "en-US",
  "timezone":         "Pacific Standard Time",
  "admin_user":       "Administrator",
  "admin_pass":       "Hunter2!",
  "org_name":         "Acme Corp",
  "product_key":      "",
  "windows_edition":  "Windows 11 Pro",
  "domain_join":      {},
  "firstboot_cmds":   []
}
```

**Response (200)**:

```json
{
  "ok": true,
  "xml": "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<unattend ..."
}
```

Returns 409 if `hypervisor_type != utm`. Returns 500 on render error.

---

## Manual override instructions

### Regenerate the answer ISO without Ansible

```bash
cd autopilot-proxmox
python3 web/answer_iso.py \
  --vm-name my-template \
  --output-dir output/answer-isos \
  --profile-json '{
    "hostname":   "WIN11-TPL",
    "locale":     "en-US",
    "timezone":   "Pacific Standard Time",
    "admin_user": "Administrator",
    "admin_pass": "YourPassword123!"
  }'
```

The ISO is written to `output/answer-isos/my-template-autounattend.iso`.

### Preview unattend.xml without building an ISO

```bash
python3 web/answer_iso.py \
  --vm-name preview \
  --output-dir /dev/null \
  --profile-json '{"hostname":"TEST","admin_pass":"x"}' \
  --preview-only
```

### Attach an answer ISO to an existing bundle manually

```bash
# Copy the ISO into the bundle's Data/ directory
cp output/answer-isos/my-template-autounattend.iso \
   ~/Library/Containers/com.utmapp.UTM/Data/Documents/my-template.utm/Data/

# Patch config.plist to wire Drive.2
plutil -replace Drive.2.ImageName \
  -string my-template-autounattend.iso \
  ~/Library/Containers/com.utmapp.UTM/Data/Documents/my-template.utm/config.plist
```

### Check the sentinel from the host

```bash
utmctl exec my-template -- \
  powershell.exe -Command "Get-Content 'C:\autopilot\autopilot-firstboot.done'"
```

### Inspect firstboot logs

```bash
utmctl exec my-template -- \
  powershell.exe -Command "Get-Content 'C:\autopilot\firstboot.log'"
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `hdiutil: command not found` | Running on Linux/non-macOS | `answer_iso.py` requires macOS. Run on the UTM host. |
| Sentinel never written | firstboot.ps1 failed silently | Inspect `C:\autopilot\firstboot.log` via `utmctl exec`. |
| Windows asks for product key | ISO edition mismatch | Set `utm_answer_windows_edition` to match an edition in the ISO's `install.wim`. Run `dism /Get-WimInfo /WimFile:sources\install.wim` inside WinPE to list editions. |
| AutoLogon skipped | `admin_pass` not set | Set `utm_answer_admin_pass` in `vault.yml` or pass via `-e`. |
| `Drive.2.ImageName` plutil error | Skeleton bundle has no Drive.2 | Ensure the skeleton `config.plist` includes a placeholder Drive.2 entry. |
| Poll timeout (45 min) | Slow hardware or install stall | Increase `utm_template_poll_retries` or inspect the VM display in UTM.app. |
