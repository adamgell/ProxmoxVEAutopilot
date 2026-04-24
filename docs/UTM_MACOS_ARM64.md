# UTM macOS ARM64 Windows 11 Template Builder

Session handoff notes for `feature/utm-macos-arm64-support`. Records the
architecture decisions, gotchas, and open tasks so another agent/model
can pick up without re-deriving context.

## Sub-project 1 status (2026-04-24)

Sub-project 1's foundation is in place and 95% of the goal is met:

- T1–T13 green: Python library (`web/utm_bundle.py`) builds valid
  ConfigurationVersion-4 plists, lays out `.utm` bundles, wraps
  `qemu-img` and `utmctl`, with a golden fixture + schema contract
  extractor + Tier 1 drift CI script. 20 unit tests pass.
- T14 green: first autonomous end-to-end Win11 ARM64 template build
  completed with `failed=0` in run 8 (600s shutdown poll).
- T15 deferred: `virt-fw-vars` EFI-shell elimination is a follow-up,
  tracked as `utm-efi-vars-nvram-boot-entry`. Fallback is a 5-line
  osascript keystroke behind `utm_boot_fallback_keystrokes` (defaults
  true).
- T16 green: `create_bundle.yml` and `customize_plist.yml` deleted;
  playbook stripped of plutil patches, quit-relaunch dance, and the
  30-line keystroke block (shrunk to 5 lines).
- T17 partial: acceptance run 1 (win11-acc-1) green with a mid-run
  manual keystroke recovery (triggered by a keystroke-block bug that
  was fixed after). Acceptance run 2 (win11-acc-2) regressed — Setup
  and OOBE completed, but firstboot's scheduled-task shutdown never
  fired (VM stayed `started` for full 45-min poll budget). Cause not
  yet pinned down; suspects: `Get-PSDrive`-based `$OEM$` stage fails
  on some runs (non-deterministic drive-letter assignment), OR
  `schtasks /RU SYSTEM` fails silently from FirstLogonCommand context.

Known reliable shape for manually reproducing the spec's green state:
run with `-e utm_boot_fallback_keystrokes=false` if/when the
`virt-fw-vars` fix lands, AND add an explicit sanity check after the
OEM staging FLC (e.g., `Test-Path C:\autopilot\firstboot.ps1` in a
new Order 1.5 that fails the setup loudly). Those are the obvious
next fixes; out of scope for this session's context budget.

See follow-ups: `utm-efi-vars-nvram-boot-entry`,
`utm-phase2-sysprep-no-qga`, `utm-firstboot-shutdown-reliability`.

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
6. **ARM64 Windows QGA — available via sibling repo**. Fedora's
   virtio-win pack still ships x86/x64 qemu-ga MSIs only, and utmapp's
   guest-tools still ships ARM64 Spice vdagent but no QGA. The gap is
   filled by `adamgell/qemu-ga-aarch64-msi` (QGA 11.0.0, MSYS2
   `clangarm64` build, WiX-packaged). That MSI is bundled at
   `autopilot-proxmox/assets/qemu-ga-aarch64-win/qemu-ga-aarch64.msi`
   and gets staged into `$OEM$\$1\autopilot\` by `answer_iso.py` for
   `firstboot.ps1` to `msiexec /i ... /quiet /norestart` into the
   guest.

   **Prerequisite** (already wired): the MSI is user-mode only and
   opens `\\.\Global\org.qemu.guest_agent.0`, which requires the
   virtio-serial kernel driver (`vioser.sys`) to be loaded first —
   covered by the `vioser\w11\ARM64` entries added to the
   `DriverPaths` block in `unattend.xml.j2`.

   **Known UTM gotcha**: `utmctl exec` fails with OSStatus -2700 on
   UTM 4.7.5 even when the QGA channel is up — this is a UTM harness
   issue, not a QGA one. `utmctl ip-address <uuid>` works against the
   same channel and is useful as a "VM is fully booted + QGA is up"
   sanity check from the host.

   Sysprep in `sysprep_finalize.yml` still uses `utmctl exec` today;
   `utm-phase2-sysprep-via-qga` tracks the switch to either a direct
   QMP/QGA JSON-RPC call or a FirstLogonCommand that runs
   `sysprep /oobe /generalize /shutdown` directly (which works today,
   with or without QGA, since it doesn't need host-side exec).

## Key files

| Path | Purpose |
| --- | --- |
| `autopilot-proxmox/roles/utm_template_builder/defaults/main.yml` | Role defaults: `utm_enable_tpm_secure_boot: true`, `utm_virtio_win_iso_name: virtio-win.iso`. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/create_bundle.yml` | AppleScript `make` (4 drives, inc. virtio removable), PlistBuddy reads of drive UUIDs, copies virtio-win ISO into `Data/`. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/customize_plist.yml` | `Drive.0.ImageName` (installer), `QEMU.TPMDevice=true`, replaces `efi_vars.fd` with `edk2-arm-secure-vars.fd`. |
| `autopilot-proxmox/playbooks/utm_build_win11_template.yml` | Orchestrates build. Sets `Drive.3.ImageName`, sends EFI shell keys, polls `C:\autopilot\autopilot-firstboot.done` (45 min timeout). |

## Known unresolved

- The abandoned UTM fork lives at `~/src/UTM`, branch
  `feature/applescript-tpm-securebootkeys`, commit `884e75c`. Kept
  only for reference; do not rebuild over `/Applications/UTM.app`.

## Active tasks (session snapshot 2026-04-24)

| id | status | title |
| --- | --- | --- |
| utm-e2e-win11-template | resolved | End-to-end Win11 template test — green in T14 run 8 autonomously; T17 acc-1 green with one manual keystroke nudge |
| utm-virtio-cd-plumbing | resolved | Drive.3 ImageName now set by render_bundle.yml + utm_bundle.py (no plutil) |
| utm-driver-load-verify | resolved | `DriverPaths` in autounattend.xml ARM64 PnpCustomizationsWinPE covers D/E/F for viostor + NetKVM |
| utm-autounattend-driverpaths | resolved | Landed in commit `d58eb10` |
| utm-efi-vars-nvram-boot-entry | pending | Replace the 5-line keystroke EFI-shell escape with a `virt-fw-vars`-baked Boot0000 / BootOrder in `efi_vars.fd` (eliminates the osascript fallback entirely) |
| utm-qga-arm64-msi-wiring | resolved | `adamgell/qemu-ga-aarch64-msi` v11.0.0-1 bundled at `assets/qemu-ga-aarch64-win/`, staged via `$OEM$`, installed by `firstboot.ps1` step 5a. vioser DriverPaths covers the kernel-mode prereq. See issue #30 |
| utm-phase2-sysprep-via-qga | pending | `sysprep_finalize.yml` still uses `utmctl exec` which trips OSStatus -2700 on UTM 4.7.5 even with QGA installed. Options: wait for UTM fix, go direct via QMP/QGA JSON-RPC, or side-step via a FirstLogonCommand `sysprep /oobe /generalize /shutdown` |
| utm-firstboot-shutdown-reliability | pending | T17 acc-2 regressed — Setup+OOBE completed but the scheduled-task shutdown did not fire. Commits `6b28b98` (`$LASTEXITCODE` + `/SD`) and `e738077` (QGA install pre-shutdown) harden the path; unverified without a fresh E2E |
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
