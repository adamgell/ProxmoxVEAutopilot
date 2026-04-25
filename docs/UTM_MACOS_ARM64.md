# UTM macOS ARM64 Windows 11 Template Builder

Session handoff notes for `feature/utm-macos-arm64-support`. Records the
architecture decisions, gotchas, and open tasks so another agent/model
can pick up without re-deriving context.

## Sub-project 1 status (2026-04-24)

Sub-project 1's foundation is in place and 95% of the goal is met:

- T1-T13 green: Python library (`web/utm_bundle.py`) builds valid
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
- T17 resolved (3-in-a-row spec bar met): acc-1, acc-2, and acc-3 all
  reached `stopped` autonomously with the current firstboot pipeline.
  Two root-cause fixes landed in this session:
    1. acc-2 regression cause was firstboot.ps1 written UTF-8 without
       a BOM. Windows PowerShell 5.1 (which FirstLogonCommand invokes)
       defaults to the system ANSI codepage on a BOM-less .ps1. The
       new QGA + UTM Guest Tools install blocks added em-dash chars to
       firstboot.ps1.j2; PS 5.1 mis-decoded the multi-byte UTF-8 bytes
       and threw `MissingEndCurlyBrace` at unrelated lines. FLC's
       `powershell.exe -File C:\autopilot\firstboot.ps1` exited
       non-zero before any code ran, so no firstboot.log, no scheduled
       shutdown task, VM never halted. Fix `23ee992`: answer_iso.py
       writes firstboot.ps1 with `encoding="utf-8-sig"` (explicit BOM),
       em-dashes purged from all Windows-bound template files,
       regression tests in `test_answer_iso_encoding.py`. acc-2 re-run
       stopped after 128 poll attempts (~21 min).
    2. ARM64 QGA prerequisite: `Microsoft-Windows-PnpCustomizationsWinPE`
       only injects drivers into Setup's WinPE environment, not into
       the OS driver store. vioserial therefore did not persist into
       the installed Windows even though it was listed in DriverPaths.
       Fix `d192deb`: added `Microsoft-Windows-PnpCustomizationsNonWinPE`
       in the offlineServicing pass with vioserial + viorng paths. acc-3
       run verified end-to-end: setupact.log shows DISM imported
       `vioser.inf` to the offline driver store; first boot saw
       `VirtIO Serial Driver` Status=OK and `sc.exe query QEMU-GA`
       reported STATE=4 RUNNING. acc-3 stopped after 88 poll attempts
       (~15 min, ~6 min faster than acc-2 because the false-failure
       1603 path shortens once the service actually reaches RUNNING).

See follow-ups: `utm-efi-vars-nvram-boot-entry`,
`utm-phase2-sysprep-via-qga`, `utm-qga-msi-install-start-cosmetic-1603`.

## Final architecture (sub-project 1 complete)

- **Hypervisor**: official signed `/Applications/UTM.app` release.
  Do **not** run a locally-built UTM fork - it loses the restricted
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
  set by the renderer - no post-hoc plutil patching.
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
  x86/x64 QGA only), so the playbook can't use `utmctl exec` - it polls
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
6. **ARM64 Windows QGA + UTM Guest Tools - both bundled**. Fedora's
   virtio-win pack still ships x86/x64 qemu-ga MSIs only. UTM's
   guest-tools ships ARM64 Spice vdagent but no QGA. We bundle both:

   - **QGA** - `adamgell/qemu-ga-aarch64-msi` (QGA 11.0.0, MSYS2
     `clangarm64`, WiX-packaged) at
     `autopilot-proxmox/assets/qemu-ga-aarch64-win/qemu-ga-aarch64.msi`.
     Provides host-to-guest orchestration: `utmctl ip-address`,
     QMP/QGA JSON-RPC, etc.
   - **UTM Guest Tools** - `utm-guest-tools-0.1.271.exe` at
     `autopilot-proxmox/assets/utm-guest-tools-win/`. NSIS installer
     that deploys Spice vdagent (clipboard + dynamic-resolution
     resize), spice-webdavd (folder sharing), and a few extras.

   Both are staged into `$OEM$\$1\autopilot\` by `answer_iso.py` and
   installed by `firstboot.ps1` - QGA via `msiexec /i /quiet
   /norestart` (step 5a), guest tools via NSIS `/S` (step 5b).
   Disable either by setting `qemu_ga_msi_path` or
   `utm_guest_tools_exe_path` to an empty string in the profile.

   **Prerequisite** (already wired): the MSI is user-mode only and
   opens `\\.\Global\org.qemu.guest_agent.0`, which requires the
   virtio-serial kernel driver (`vioser.sys`) to be loaded first -
   covered by the `vioserial\w11\ARM64` entries (the directory is
   `vioserial` on the virtio-win ISO; the files inside it are
   `vioser.sys/.inf/.cat`) added to the `DriverPaths` block in
   `unattend.xml.j2`.

   **Known UTM gotcha**: `utmctl exec` fails with OSStatus -2700 on
   UTM 4.7.5 even when the QGA channel is up - this is a UTM harness
   issue, not a QGA one. `utmctl ip-address <uuid>` works against the
   same channel and is useful as a "VM is fully booted + QGA is up"
   sanity check from the host.

   Sysprep in `sysprep_finalize.yml` still uses `utmctl exec` today;
   `utm-phase2-sysprep-via-qga` tracks the switch to either a direct
   QMP/QGA JSON-RPC call or a FirstLogonCommand that runs
   `sysprep /oobe /generalize /shutdown` directly (which works today,
   with or without QGA, since it doesn't need host-side exec).

7. **FirstLogonCommand runs Windows PowerShell 5.1, which needs a
   UTF-8 BOM on .ps1 files**. FLC invokes `powershell.exe` (5.1), not
   `pwsh.exe` (7). PS 5.1 reads a BOM-less .ps1 as the system ANSI /
   OEM codepage, not UTF-8. Any multi-byte char (em-dash, en-dash,
   arrow, smart quote) mis-decodes and the parser throws
   `MissingEndCurlyBrace` on unrelated lines. FLC's `-File` invocation
   then exits non-zero silently: no log, no side effects, no task.
   Two defenses in place: `answer_iso.py` writes firstboot.ps1 with
   `encoding="utf-8-sig"` (BOM), and `test_answer_iso_encoding.py`
   forbids em/en-dashes, arrows, and smart quotes in the Windows-
   bound template files (`firstboot.ps1.j2`, `unattend.xml.j2`,
   `autounattend.xml[.j2]`). Repo rule: no em-dashes anywhere, ever.

8. **PnpCustomizationsWinPE does not persist drivers to the OS driver
   store**. The windowsPE pass `Microsoft-Windows-PnpCustomizationsWinPE`
   only stages drivers into Setup's WinPE environment for boot-time
   discovery of disks + NICs. Non-boot-critical drivers (vioserial,
   viorng, vioinput, etc.) are NOT carried forward into the installed
   OS. Symptom seen on acc-2: stock Win11 ARM64 installs to a UTM VM,
   but `Get-PnpDevice` shows `VirtIO Serial Driver` missing and
   qemu-ga-aarch64.msi's QEMU-GA service blocks indefinitely on
   CreateFile for `\\.\Global\org.qemu.guest_agent.0`. Fix is the
   `Microsoft-Windows-PnpCustomizationsNonWinPE` block under
   `<settings pass="offlineServicing">` in `unattend.xml.j2`. It runs
   inside WinPE's servicing context against the offline image (drive
   letters match windowsPE so the D:/E:/F: multi-path guard works) and
   stages the INF into the OS driver store via DISM. PnP auto-installs
   at first boot. See `https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/how-configuration-passes-work`.

9. **QGA MSI (adamgell/qemu-ga-aarch64-msi v11.0.0-1) returns 1603
   even on a fully successful install**. The WiX has
   `<ServiceControl Start="install" Wait="yes" />` which tells MSI to
   synchronously start the service at install time. qemu-ga.exe takes
   ~110-130s to bind to the virtio-serial endpoint and reach
   SERVICE_RUNNING (because the host-side org.qemu.guest_agent.0
   chardev is not active until just after first boot). MSI's wait
   expires before that, MSI marks "Error 1920 service failed to start"
   and rolls back the product registration, but the service binaries
   are open at that point so they survive. Net post-install state on
   acc-3: `sc.exe query QEMU-GA` -> RUNNING, vioserial bound, full
   QGA functionality, but MSI exit 1603 leaks up to firstboot.ps1 as
   a WARNING. Workaround: firstboot.ps1 already treats MSI non-zero
   on QGA as a warning and continues (template still builds). Real
   fix is upstream in the QGA MSI repo: drop `Start="install"` so MSI
   does not synchronously wait at install time. Tracked as
   `utm-qga-msi-install-start-cosmetic-1603`.

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
| utm-e2e-win11-template | resolved | End-to-end Win11 template test. T14 run 8 green autonomously. T17 acc-1 (post BOM fix, manual patch), acc-2 (post BOM fix, automated, ~21 min), and acc-3 (post offlineServicing fix, automated, ~15 min) all reached `stopped` state. 3-in-a-row spec bar met. |
| utm-virtio-cd-plumbing | resolved | Drive.3 ImageName now set by render_bundle.yml + utm_bundle.py (no plutil) |
| utm-driver-load-verify | resolved | `DriverPaths` in autounattend.xml ARM64 PnpCustomizationsWinPE covers D/E/F for viostor + NetKVM |
| utm-autounattend-driverpaths | resolved | Landed in commit `d58eb10` |
| utm-efi-vars-nvram-boot-entry | pending | Replace the 5-line keystroke EFI-shell escape with a `virt-fw-vars`-baked Boot0000 / BootOrder in `efi_vars.fd` (eliminates the osascript fallback entirely) |
| utm-qga-arm64-msi-wiring | resolved | `adamgell/qemu-ga-aarch64-msi` v11.0.0-1 bundled at `assets/qemu-ga-aarch64-win/`, staged via `$OEM$`, installed by `firstboot.ps1` step 5a. `vioserial` DriverPaths covers the kernel-mode prereq. See issue #30 |
| utm-guest-tools-wiring | resolved | UTM Guest Tools 0.1.271 bundled at `assets/utm-guest-tools-win/`, staged via `$OEM$`, installed silently (`/S`) by `firstboot.ps1` step 5b. Adds Spice vdagent for clipboard + dynamic-resolution resize |
| utm-phase2-sysprep-via-qga | pending | `sysprep_finalize.yml` still uses `utmctl exec` which trips OSStatus -2700 on UTM 4.7.5 even with QGA installed. Options: wait for UTM fix, go direct via QMP/QGA JSON-RPC, or side-step via a FirstLogonCommand `sysprep /oobe /generalize /shutdown` |
| utm-firstboot-shutdown-reliability | resolved | Root cause was firstboot.ps1 written UTF-8 without BOM; PS 5.1 mis-parsed the multi-byte em-dashes and FLC exited before scheduling shutdown. Fix `23ee992`: BOM-writing + em-dash purge + regression tests. acc-2 re-run reached `stopped` autonomously after ~21 min (128 poll attempts). |
| utm-qga-arm64-driver-store-staging | resolved | vioserial driver now staged into OS driver store via `Microsoft-Windows-PnpCustomizationsNonWinPE` in the offlineServicing pass (commit `d192deb`). Verified on acc-3: setupact.log line 5507 logs DISM importing `vioser.inf` to the offline driver store; post-install `pnputil /enum-drivers` shows oem0/oem6 (vioser.inf), `Get-PnpDevice` shows `VirtIO Serial Driver` OK, `sc.exe query QEMU-GA` reports RUNNING. |
| utm-qga-msi-install-start-cosmetic-1603 | pending | QGA MSI returns 1603 even though the QEMU-GA service ends up registered, auto-start, and RUNNING post-install. Cause: `<ServiceControl Start="install" Wait="yes" />` in the WiX (`adamgell/qemu-ga-aarch64-msi/build/wix/qemu-ga.wxs`) makes MSI synchronously start the service at install time. qemu-ga.exe takes ~110-130s to bind to the virtio-serial endpoint and reach SERVICE_RUNNING; MSI's wait expires earlier and rolls back the product registration, but the service binaries are already in use and survive. Net result is a misleading WARNING in our firstboot logs. Fix is in the QGA MSI repo: drop `Start="install"` so MSI does not synchronously wait at install time; service still auto-starts on next boot. Tracking the rebuild + asset bump separately. |
| utm-e2e-sequence-full | pending | Full sequence E2E on UTM (clone -> autopilot inject -> hash capture -> Intune) |
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
