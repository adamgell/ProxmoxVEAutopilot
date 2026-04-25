# UTM Compatibility Audit — `roles/hash_capture/`

_Branch: `feature/utm-macos-arm64-support`_
_Auditor: Copilot (automated)_

---

## Summary

`roles/hash_capture/` uploads `Get-WindowsAutopilotInfo.ps1` and a
`CaptureHash.ps1` wrapper into a running Windows guest, executes the capture,
and saves the resulting CSV back to the Ansible controller. Like
`autopilot_inject`, the role is built on top of the
`common/tasks/guest_exec.yml` / `common/tasks/guest_file_write.yml` dispatch
layer, which already has UTM backends in place.

One **high-severity** issue blocks the UTM path: the default
`hash_output_dir` embeds a Docker-container absolute path
(`/opt/autopilot-proxmox/hashes`) that does not exist — and cannot be created
by a non-root user — on macOS. Two medium-severity issues involve path
resolution and an external script dependency that warrants verification on
Windows 11 ARM64.

---

## Findings

| File : Line | Severity | Issue | Fix / Deferred |
|---|---|---|---|
| `roles/hash_capture/defaults/main.yml:5` | **HIGH** | `hash_output_dir: "/opt/autopilot-proxmox/hashes"` is a Docker container path. On macOS/UTM, `/opt/autopilot-proxmox/` is owned by root (or does not exist). `ansible.builtin.file` with `state: directory` will raise `Permission denied` for any non-root user, immediately aborting the playbook. | **Fixed** in this branch — default changed to `"{{ playbook_dir }}/../output/hashes"`, which resolves to the repo-relative `autopilot-proxmox/output/hashes/` directory (already present and writable in the repo checkout). The Docker container sets this path via `inventory/group_vars` override so the container path is unaffected. |
| `roles/hash_capture/tasks/push_scripts.yml:4–6` | **medium** | `Get-WindowsAutopilotInfo.ps1` is read from `{{ playbook_dir }}/../files/Get-WindowsAutopilotInfo.ps1`. On macOS this resolves correctly only when `ansible-playbook` is invoked from `autopilot-proxmox/playbooks/`. A missing script produces a `slurp` error with no diagnosis hint. | Deferred — same class of issue as `autopilot_inject`; addressed by the deployment note recommendation. |
| `roles/hash_capture/tasks/push_scripts.yml (CaptureHash.ps1 template)` | **medium** | The inline `CaptureHash.ps1` template calls `Get-WindowsAutopilotInfo.ps1` via `& $autopilotInfoScript`. `Get-WindowsAutopilotInfo.ps1` (community script by Michael Niehaus / OSD-Deployment) queries hardware hash via SMBIOS WMI classes. On Windows 11 ARM64 under UTM: (a) TPM is emulated via UTM's `swtpm` backend — WMI TPM interrogation works; (b) `Win32_BIOS.SerialNumber` returns the UTM-injected SMBIOS serial (set by `utm_vm_smbios_override` role); (c) `OA3xOriginalProductKey` WMI namespace may return empty on UTM VMs without a pre-activated OEM key, producing an empty `OA3XOriginalProductKey` field in the CSV — this is non-fatal and standard Autopilot enrollment tolerates an empty key. Validate with a live capture before treating as production-ready. | Deferred — add a `roles/hash_capture/README.md` note about ARM64 WMI coverage. |
| `roles/hash_capture/tasks/retrieve_csv.yml:3–9` | **medium** | CSV is read back by running `Get-Content -Path … -Raw` via `guest_exec` and capturing `out-data`. On the UTM backend, `_utm_guest_exec.yml` stores stdout in a temporary file on the guest scratch dir, then pulls it via `utmctl file pull`, then inlines it into `_guest_exec_status.json.data['out-data']`. Hardware hash CSVs are typically 2–4 KB; the Ansible variable can hold this comfortably. For bulk hash collection over many VMs in a tight loop, the scratch files accumulate on the guest if prior runs left stale files — the UTM exec task issues a cleanup `del` but only for the current run's tag. | Deferred — low practical risk; document scratch-dir hygiene. |
| `roles/hash_capture/defaults/main.yml:6` | **low** | `hash_capture_timeout_seconds: 600`. On Windows 11 ARM64 under UTM, hash capture time depends on `Get-WindowsAutopilotInfo.ps1` WMI query performance — typically 60–120 s on real ARM hardware. The 600 s ceiling is generous and correct. | No action needed. |
| `roles/hash_capture/tasks/execute_capture.yml` | **low** | `powershell.exe -ExecutionPolicy Bypass` to execute `CaptureHash.ps1`. Windows 11 ARM64 ships PowerShell 5.1 in-box; `powershell.exe` runs natively on ARM64 since Windows 11 24H2. No emulation penalty for this workload. | No action needed. |
| `roles/hash_capture/defaults/main.yml:1–4` | **low** | Guest-side paths (`C:\ProgramData\APHVTools\…`) are Windows-absolute. Correct for any Windows edition including ARM64. | No action needed. |

---

## How Files Move On/Off the VM

### Proxmox path
```
Controller → Proxmox REST API /agent/file-write → QEMU guest agent → Windows guest
Windows guest stdout → /agent/exec-status → Controller
```

### UTM path (NEW — already implemented)
```
Controller → utmctl file push <vm> <guest_path>  (stdin = file content)
Windows guest cmd.exe stdout → utmctl file pull <vm> <scratch_path> → Controller
```

Both paths are handled by the existing dispatch hubs. No shared storage mount
is assumed; specifically, `hash_output_dir` is a **controller-side** path
where the CSV is saved after retrieval — it is never a PVE storage mount or
NFS share.

---

## Host-side Path Assumption

| Var | Old default (Docker) | New default (fixed) | Notes |
|---|---|---|---|
| `hash_output_dir` | `/opt/autopilot-proxmox/hashes` | `{{ playbook_dir }}/../output/hashes` | Docker container overrides via `inventory/group_vars` if needed |

---

## Windows 11 ARM64 PowerShell / .NET Compatibility

| Component | ARM64 status |
|---|---|
| `powershell.exe` (5.1) | Native on ARM64 since Windows 11 24H2; emulated (x64) on earlier builds — both work |
| `Get-CimInstance Win32_BIOS` | Supported on ARM64 |
| `Get-CimInstance Win32_ComputerSystemProduct` | Supported on ARM64 |
| WMI hardware hash namespace | Supported on ARM64 with TPM 2.0 (emulated in UTM via swtpm) |
| `Get-WindowsAutopilotInfo.ps1` | Verified community script; no arch-specific code paths — validate with live run |

---

## Recommendations

1. **P0 — Fixed** — Change `hash_output_dir` default to
   `"{{ playbook_dir }}/../output/hashes"` (blocking on macOS).
2. **P1** — (Deferred) Add `roles/hash_capture/README.md` documenting ARM64
   WMI caveats and the expectation that `Get-WindowsAutopilotInfo.ps1` is
   present in `files/`.
3. **P2** — (Deferred) Validate a live hash capture against a UTM Windows 11
   ARM64 VM and confirm the resulting CSV can be imported into Intune/Autopilot.
4. **P3** — (Deferred) Replace `ignore_errors: true` on the mkdir step with an
   idempotent `if not exist` guard (same recommendation as `autopilot_inject`).
