# UTM macOS ARM64 Windows 11 Template Builder

Session handoff notes for `feature/utm-macos-arm64-support`. Records the
architecture decisions, gotchas, and open tasks so another agent/model
can pick up without re-deriving context.

## Final architecture (sub-project 1 complete)

- **Hypervisor**: official signed `/Applications/UTM.app` release.
  Do **not** run a locally-built UTM fork — it loses the restricted
  `com.apple.vm.networking` entitlement. Ad-hoc codesigning silently
  drops these; macOS denies vmnet at runtime.
- **Automation**: `autopilot-proxmox/web/utm_bundle.py` (Python library
  + `python -m web.utm_bundle build` CLI) generates `config.plist` in
  one shot from a Jinja-free dataclass tree via `plistlib.dumps`. No
  `plutil`, no `PlistBuddy`, no AppleScript `make`. The Ansible role
  `utm_template_builder` is a thin wrapper that builds a spec JSON,
  pipes it to the CLI, then starts the VM via `utmctl`.
- **Bundle registration**: `/usr/bin/open -a UTM <bundle>` via
  `UtmctlClient.register` in the Python library. UTM's `utmctl` has no
  `register` subcommand; Launch Services does the work, then the
  library polls `utmctl list` for the bundle name.
- **TPM 2.0 + Secure Boot (aarch64)**: set at render time in the plist
  (`QEMU.TPMDevice=true`, `efi_vars.fd` copied from UTM's
  `edk2-arm-secure-vars.fd`). UTM derives Secure Boot on aarch64 from
  `arch.hasSecureBootSupport && target.hasSecureBootSupport &&
  qemu.hasTPMDevice`, so TPMDevice=true is enough.
- **Four drives** (USB CD installer, VirtIO system disk, USB CD answer
  ISO, USB CD virtio-win for viostor). All four ImageName fields are
  set by the renderer — no post-hoc plutil patching.
- **Answer ISO + `$OEM$\$1\`**: Windows Setup only processes the `$OEM$`
  folder when the answer file lives on the installer media; ours is a
  separate CD, so a FirstLogonCommand Order 1 scans FS drives for one
  containing `autounattend.xml`, then `Copy-Item`s `$OEM$\$1\*` into
  `C:\`. That's what makes `C:\autopilot\firstboot.ps1` exist at all.
- **Firstboot self-shutdown**: `firstboot.ps1` schedules a one-shot
  Task Scheduler job (`schtasks /SC ONCE /RU SYSTEM /RL HIGHEST`) that
  runs `shutdown /s /t 0 /f` 3 minutes after OOBE. Direct Stop-Computer
  or `shutdown.exe` calls inside the FirstLogonCommand orchestrator
  get swallowed; the scheduled task fires outside that context. No
  ARM64 QEMU Guest Agent exists (virtio-win + utmapp guest-tools ship
  x86/x64 QGA only), so the playbook can't use `utmctl exec` — it polls
  `utmctl status` for `stopped` instead.

## Critical gotchas

1. **Sandboxed Documents dir**. Official UTM stores VMs under
   `~/Library/Containers/com.utmapp.UTM/Data/Documents/`. Inventory
   default already points there; override via
   `-e utm_documents_dir=...` only if needed.
2. **UTM enum casing is strict**. `UTMBackend` rawValue is `"QEMU"`
   (not `"qemu"`); `QEMUNetworkMode.shared` rawValue is `"Shared"`.
   Lower-case values produce `UTMConfigurationError.invalidBackend`
   and "Cannot import this VM" popups.
3. **`Display.VgaRamMib` must be absent for `virtio-ramfb-gl`**. UTM
   always appends `vgamem_mb=<val>` to the `-device` arg when the key
   is present; QEMU rejects it for virtio-ramfb-gl. Make the renderer
   omit the key when unset.
4. **`com.apple.vm.networking` is Apple-restricted**. Self-serve dev
   accounts can't grant it. Shared/Host/Bridged networking only works
   with the signed release build.
5. **EFI shell drop (aarch64 virt + USB CD)**. AAVMF doesn't auto-add
   removable USB CDs to BootOrder on first boot. Today we work around
   it with a 5-line `osascript input keystroke` fallback, gated on
   `utm_boot_fallback_keystrokes: true`. The structural fix (write a
   Boot0000 + BootOrder into `efi_vars.fd` via `virt-fw-vars`) is
   tracked as follow-up `utm-efi-vars-nvram-boot-entry`.
6. **No ARM64 Windows QGA**. Fedora's virtio-win pack ships x86/x64
   qemu-ga MSIs only; utmapp's guest-tools ships ARM64 Spice vdagent
   but no QGA. Any "inside the guest" orchestration that today goes
   through `utmctl exec` needs an alternative signal (shutdown, serial
   port, shared volume). Phase 2 sysprep (`sysprep_finalize.yml`)
   still uses `utmctl exec` and is thus gated behind Phase 2 — if
   Phase 2 becomes load-bearing, switch it to a FirstLogonCommand
   that runs `sysprep /oobe /generalize /shutdown` directly. Tracked
   as follow-up `utm-phase2-sysprep-no-qga`.

## Key files

| Path | Purpose |
| --- | --- |
| `autopilot-proxmox/roles/utm_template_builder/defaults/main.yml` | Role defaults: `utm_enable_tpm_secure_boot: true`, `utm_virtio_win_iso_name: virtio-win.iso`. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/create_bundle.yml` | AppleScript `make` (4 drives, inc. virtio removable), PlistBuddy reads of drive UUIDs, copies virtio-win ISO into `Data/`. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/customize_plist.yml` | `Drive.0.ImageName` (installer), `QEMU.TPMDevice=true`, replaces `efi_vars.fd` with `edk2-arm-secure-vars.fd`. |
| `autopilot-proxmox/playbooks/utm_build_win11_template.yml` | Orchestrates build. Sets `Drive.3.ImageName`, sends EFI shell keys, polls `C:\autopilot\autopilot-firstboot.done` (45 min timeout). |

## Known unresolved

- `autounattend.xml` does not yet set `DriverPaths` for the virtio CD,
  so Setup currently requires manual "Load driver" at the disk picker.
  See todo `utm-autounattend-driverpaths`.
- The abandoned UTM fork lives at `~/src/UTM`, branch
  `feature/applescript-tpm-securebootkeys`, commit `884e75c`. Kept
  only for reference; do not rebuild over `/Applications/UTM.app`.

## Active tasks (session SQL snapshot 2026-04-23)

| id | status | title |
| --- | --- | --- |
| utm-e2e-win11-template | blocked | End-to-end Win11 template test (original block — now mostly resolved by plist TPM/SB + virtio CD) |
| utm-virtio-cd-plumbing | in_progress | Fix virtio-win Drive.3 ImageName plumbing (role default not in play scope) |
| utm-driver-load-verify | in_progress | Verify user-driven driver load on current VM (86F1B7AA-…) |
| utm-autounattend-driverpaths | pending | Add `DriverPaths` for virtio-win to `autounattend.xml` |
| utm-e2e-sequence-full | pending | Full sequence E2E on UTM (clone → autopilot inject → hash capture → Intune) |
| utm-tui-plugin-research | pending | Investigate TUI/UTM plugin surface |
| utm-upstream-utmctl-create | pending | Upstream `utmctl create` subcommand PR to utmapp/UTM |

## Handy one-liners

```sh
# Identify available signing identities
security find-identity -p codesigning -v

# Live-patch Drive.3 on an existing bundle
plutil -replace Drive.3.ImageName -string virtio-win.iso \
  ~/Library/Containers/com.utmapp.UTM/Data/Documents/<VM>.utm/config.plist

# Drive the VM
/Applications/UTM.app/Contents/MacOS/utmctl {start|stop|status} <UUID>
```
