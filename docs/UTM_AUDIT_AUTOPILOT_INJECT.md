# UTM Compatibility Audit — `roles/autopilot_inject/`

_Branch: `feature/utm-macos-arm64-support`_
_Auditor: Copilot (automated)_

> **Note on scope:** The task specification referenced `web/autopilot_inject.py`.
> That file does not exist. The autopilot injection functionality lives in the
> Ansible role `roles/autopilot_inject/` and is orchestrated from
> `playbooks/_provision_iso_vm.yml`, `playbooks/_provision_clone_vm.yml`, and
> `playbooks/retry_inject_hash.yml`. This audit covers the role and the
> `common/tasks/` dispatch layer it depends on.

---

## Summary

The `autopilot_inject` role writes
`C:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json` into a
running Windows guest. It does so exclusively through the
`common/tasks/guest_exec.yml` + `common/tasks/guest_file_write.yml` dispatch
hubs, which already route to UTM-specific backends
(`_utm_guest_exec.yml` / `_utm_guest_file_write.yml`). There are **no
Proxmox-specific API calls, no `pvesh`/`qm` references, and no
`vm_vmid`-as-integer casts** in this role.

The role is effectively hypervisor-agnostic out of the box. The two concerns
below are deployment-layout questions, not code defects.

---

## Findings

| File : Line | Severity | Issue | Fix / Deferred |
|---|---|---|---|
| `roles/autopilot_inject/defaults/main.yml:1` | **medium** | `autopilot_config_path: "{{ playbook_dir }}/../files/AutopilotConfigurationFile.json"`. In the Docker container `playbook_dir` = `/app/playbooks`, so the path resolves to `/app/files/…`. On macOS the path resolves relative to wherever `ansible-playbook` is invoked — correct only when run from `autopilot-proxmox/playbooks/`. Invoking from repo root or a different directory silently produces a slurp error with no explanation of the expected layout. | Deferred — add a deployment note to `docs/UTM_MACOS_SETUP.md` clarifying that `ansible-playbook` must be run from `autopilot-proxmox/playbooks/` (or the var overridden via `-e autopilot_config_path=…`). |
| `roles/autopilot_inject/tasks/main.yml:12` | **low** | `ignore_errors: true` on the `mkdir` step masks genuine failure modes (e.g., guest agent not running, path not writable). The intent is to suppress "directory already exists" return codes, but neither `cmd.exe /c mkdir` (Proxmox guest agent path) nor the UTM backend exposes a dedicated "already-exists" error code — both return rc=1 for any mkdir failure. A future hardening pass should replace `mkdir` with `if not exist … mkdir` in the command body and remove `ignore_errors`. | Deferred. |
| `common/tasks/_utm_guest_file_write.yml` | **low** | UTM backend feeds `_guest_content` to `utmctl file push` via stdin. `AutopilotConfigurationFile.json` is typically ≤4 KB — well within pipe limits. If an operator points `autopilot_config_path` at a large file (e.g., a combined JSON bundle) the push could hit system pipe-buffer limits (typically 64 KB on macOS). | Deferred — warn in docs that `autopilot_config_path` must point to a standard single-profile JSON. |
| `roles/autopilot_inject/defaults/main.yml` | **low** | `autopilot_guest_file: 'C:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json'` — hardcoded Windows path. This is the **correct and required** path for Windows Autopilot on all editions including ARM64. | No action needed. |
| WinRM / credential passing | **n/a** | The role uses **no WinRM**. Credential passing uses the QEMU guest agent (Proxmox) or `utmctl` (UTM) — both out-of-band channels. No credentials are transmitted over the network for this role. | N/A |
| VMID type (int vs UUID string) | **n/a** | `vm_vmid` is used only in a debug message (`"Wrote … to VM {{ vm_vmid }}"`). The dispatch hub handles type differences between Proxmox integer VMIDs and UTM VM-name strings. | N/A |
| Windows 11 ARM64 `.NET` / PowerShell 7 | **n/a** | No PowerShell is executed by this role; `cmd.exe /c mkdir` and a file write require no `.NET` runtime. Windows 11 ARM64 ships all required components. | N/A |

---

## Proxmox-specific API / CLI Scan

| Pattern | Found? | Notes |
|---|---|---|
| `pvesh` / `qm` CLI | No | — |
| Proxmox REST URI construction | No | All routing via dispatch hub |
| VMID cast to `int` | No | — |
| Storage names (`local-lvm`, etc.) | No | — |
| `/app/` absolute path assumption | Yes (indirect) | Via `playbook_dir` default — see finding above |
| `sshpass` / `guestfish` | No | — |

---

## Transport Architecture

```
roles/autopilot_inject/tasks/main.yml
  └─ common/tasks/guest_exec.yml        (dispatch hub)
       ├─ _proxmox_guest_exec.yml       → Proxmox REST /agent/exec
       └─ _utm_guest_exec.yml           → utmctl exec  ✔ UTM-ready
  └─ common/tasks/guest_file_write.yml  (dispatch hub)
       ├─ _proxmox_guest_file_write.yml → Proxmox REST /agent/file-write
       └─ _utm_guest_file_write.yml     → utmctl file push  ✔ UTM-ready
```

Both UTM backends exist and are plumbed. The dispatch hubs use
`hypervisor_type | default('proxmox')` to select the correct backend.

---

## Recommendations

1. **P1** — (Deferred) Replace `ignore_errors: true` + bare `mkdir` with
   `cmd.exe /c if not exist "…" mkdir "…"` to fail clearly when the guest
   agent is down or the path is invalid.
2. **P2** — (Deferred) Add a note to `docs/UTM_MACOS_SETUP.md` specifying that
   `ansible-playbook` must be invoked from `autopilot-proxmox/playbooks/`, or
   that `autopilot_config_path` must be overridden via `-e` when running from a
   non-standard working directory.
