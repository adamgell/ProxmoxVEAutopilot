# UTM Compatibility Audit — `web/sequence_compiler.py`

_Branch: `feature/utm-macos-arm64-support`_
_Auditor: Copilot (automated)_

---

## Summary

`sequence_compiler.py` is a pure-Python, side-effect-free module. It takes a
sequence dict and emits `CompiledSequence` — a bag of Ansible vars, unattend
XML fragments, and `<FirstLogonCommands>` entries. It makes **no Proxmox API
calls**, executes no external commands, and references no absolute paths.
Storage names, VMIDs, and `pvesh`/`qm` CLI tools are absent from this file
entirely.

The module is therefore broadly hypervisor-agnostic. The two areas requiring
attention are:

1. **`_ps_escape` is defined but never called** — user-supplied strings flow
   directly into single-quoted PowerShell literals in `_handle_install_module`,
   which breaks the generated script if any value contains a single quote.
2. **`powershell.exe` hardcoded** — FLC commands invoke `powershell.exe`
   explicitly. This is correct for the unattend FLC context (where only
   Windows PowerShell 5.1 is available) but carries an advisory note for
   post-FLC `run_script` steps on Windows 11 ARM64.

---

## Findings

| File : Line | Severity | Issue | Fix / Deferred |
|---|---|---|---|
| `sequence_compiler.py:277–291` | **medium** | `_handle_install_module` builds single-quoted PowerShell string literals for `module`, `repository`, `scope`, and `version` without calling `_ps_escape`. A module or version string containing a single quote (e.g. `'module'` in a hypothetical PSGallery name) produces syntactically invalid PowerShell — silent failure in unattend FLC. Affects all hypervisor backends equally, but UTM makes the failure harder to diagnose (no serial console). | **Fixed** in this branch — `_ps_escape()` now wraps every user-supplied value inside single-quoted PS literals in the handler. |
| `sequence_compiler.py:277` | **medium** | Same handler builds the `Get-PSRepository`/`Set-PSRepository` lines with `'{repository}'` interpolated twice, again without `_ps_escape`. | **Fixed** together with the finding above. |
| `sequence_compiler.py:242,296,300,461` | **low** | All FLC command strings hard-code `powershell.exe`. On Windows 11 ARM64, `powershell.exe` (WinPE/WinRT 5.1) is available in-box and is the only interpreter present during the unattend FLC pass — so this is **correct** for the compilation context. Post-FLC `run_script` steps authored by operators may want to target `pwsh.exe` (PowerShell 7 ARM64-native) for performance-sensitive work, but that choice lives in the operator-authored script body, not in the compiler. | Deferred — advisory only; no code change required. |
| `sequence_compiler.py:109` | **low** | `int(cid)` coerces `credential_id` to `int`. Internal to the app; no UTM impact today. Would silently break if credential IDs ever became UUIDs or other string types. | Deferred — track in DB schema review. |
| `sequence_compiler.py:377–379` | **low** | `_ps_escape` is defined but—prior to this fix—was never called in any handler or renderer. All other single-quoted PS literals in the file happen to use only hardcoded strings (registry key paths, shutdown messages), so they required no escaping. Document as the canonical helper; ensure future handlers call it. | **Fixed** (caller added in `_handle_install_module`; docstring updated). |

---

## Proxmox-specific API / CLI Scan

| Pattern | Found? | Notes |
|---|---|---|
| `pvesh` / `qm` CLI calls | No | Pure Python, no subprocess |
| `proxmox_api_base` / Proxmox URI construction | No | No HTTP calls |
| VMID assumed as integer | No | `vm_vmid` never referenced in this file |
| Storage name assumptions (`local-lvm`, `rbd`, etc.) | No | — |
| `/app/` or Docker-absolute path | No | No file I/O at all |
| MSI architecture path (`C:\Program Files (x86)`) | No | — |
| x64-only arch check in generated PowerShell | No | `Install-Module` is arch-agnostic |

---

## `_ps_escape` Coverage Map

All places where a single-quoted PowerShell literal is built in this file:

| Location | String source | `_ps_escape` used? |
|---|---|---|
| `_handle_install_module` line 277 `'{repository}'` | user input | **No → FIXED** |
| `_handle_install_module` line 278 `'{repository}'` | user input | **No → FIXED** |
| `_handle_install_module` line 284 `'{module}'` | user input | **No → FIXED** |
| `_handle_install_module` line 285 `'{repository}'` | user input | **No → FIXED** |
| `_handle_install_module` line 286 `'{scope}'` | user input | **No → FIXED** |
| `_handle_install_module` line 291 `'{version}'` | user input | **No → FIXED** |
| `_append_final_reboot_if_autologon` `'HKLM:\\...'` | hardcoded | N/A — literal constant |
| `_append_final_reboot_if_autologon` `'0'` | hardcoded | N/A — literal constant |
| `_append_final_reboot_if_autologon` `'Provisioning complete...'` | hardcoded | N/A — literal constant |

---

## Recommended Patch Order

1. **P1** — Apply `_ps_escape` in `_handle_install_module` (medium, quick, no
   logic change, fully covered by existing tests).
2. **P2** — (Deferred) Add a `# ps-escape required` convention comment on every
   future handler that accepts user-supplied strings for PS literals.
3. **P3** — (Deferred) Consider replacing `powershell.exe` with a
   `_ps_interpreter` variable in `CompiledSequence` so UTM+ARM64 operators can
   opt in to `pwsh.exe` for heavyweight `run_script` steps without patching the
   compiler.
