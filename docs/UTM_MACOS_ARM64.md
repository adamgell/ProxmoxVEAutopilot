# UTM macOS ARM64 Windows 11 Template Builder

Session handoff notes for `feature/utm-macos-arm64-support`. Records the
architecture decisions, gotchas, and open tasks so another agent/model
can pick up without re-deriving context.

## Final architecture

- **Hypervisor**: official signed `/Applications/UTM.app` release.
  Do **not** run a locally-built UTM fork — it loses the restricted
  `com.apple.vm.networking` entitlement (needed for Shared/Host/Bridged
  networking) and `com.apple.vm.device-access`. Ad-hoc codesigning
  silently drops these; macOS denies vmnet at runtime.
- **Automation**: Ansible role `utm_template_builder` drives UTM via
  AppleScript (`osascript`) + direct `config.plist` edits (`plutil`,
  `PlistBuddy`).
- **TPM 2.0 + Secure Boot (aarch64)**: patched post-bundle-creation —
  *no UTM fork required*.
  - `plutil -replace QEMU.TPMDevice -bool YES config.plist`
  - Replace `Data/efi_vars.fd` with UTM's bundled
    `/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd`
    (preloaded MS UEFI CA keys).
  - UTM derives Secure Boot on aarch64 from
    `arch.hasSecureBootSupport && target.hasSecureBootSupport &&
    qemu.hasTPMDevice`, so TPMDevice=true is enough.
- **virtio-win ISO as a 4th CD drive** (required because Win11 ARM64
  Setup ships no virtio-blk driver):
  - Bundle gets a 4th `{removable:true}` drive via the AppleScript
    `make` call in `roles/utm_template_builder/tasks/create_bundle.yml`.
  - Playbook sets `Drive.3.ImageName = virtio-win.iso` via `plutil`.
  - ISO expected at `{{ utm_iso_dir }}/virtio-win.iso`
    (Fedora's `latest-virtio/virtio-win.iso`, contains
    `viostor/w11/ARM64/` and `NetKVM/w11/ARM64/`).

## Critical gotchas

1. **Role defaults don't leak to play scope**. Playbook-level tasks do
   not see `roles/*/defaults/main.yml`. Inline
   `{{ utm_virtio_win_iso_name | default('virtio-win.iso') }}` in any
   playbook task, or pass `-e`.
2. **Sandboxed Documents dir**. Official UTM stores VMs under
   `~/Library/Containers/com.utmapp.UTM/Data/Documents/`. Pass
   `-e utm_documents_dir=$HOME/Library/Containers/com.utmapp.UTM/Data/Documents`.
3. **UTM AppleScript is insert-only for drives**. No `update`/`eject`
   verb to swap CD media on a running VM. All drive sources must be
   set at bundle creation (via `make` + `plutil` patch) before start.
4. **`ImageName` ignored by `make`**. AppleScript `make` builds the
   Drive entries but does not honor the `image name` argument; set it
   afterward with `plutil -replace Drive.N.ImageName`.
5. **`com.apple.vm.networking` is Apple-restricted**. Self-serve
   developer accounts cannot grant it. Shared/Host/Bridged networking
   only works with the signed release build.
6. **EFI boot order**. UTM's default boot order for ARM64 Win11 often
   drops to the EFI shell. Playbook sends keystrokes
   `fs0:` then `efi\boot\bootaa64.efi` plus a "press any key" mash.

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
