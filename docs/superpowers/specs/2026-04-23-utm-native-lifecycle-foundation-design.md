# UTM Native Lifecycle Foundation (Sub-project 1) — Design Spec

**Date:** 2026-04-23
**Status:** Draft, pending user review
**Owner:** Adam
**Follows:** `docs/UTM_MACOS_ARM64.md` (session handoff notes)

## 1. Context

The UTM track on `feature/utm-macos-arm64-support` works but leans on a brittle hybrid: AppleScript `make new virtual machine` creates a half-built bundle, then `plutil` / `PlistBuddy` patches fill in what AppleScript silently ignores (`Drive.N.ImageName`, `QEMU.TPMDevice`, SMBIOS, network overrides), `cp` overwrites `efi_vars.fd`, and `osascript` keystroke mashing is required to unstick EDK2 from the EFI shell at first boot. Each layer is a source of flakiness.

This sub-project replaces the hybrid with a cleaner foundation while keeping stock `/Applications/UTM.app` as the runtime. UTM keeps its nice UX (dynamic resolution, clipboard, Cocoa renderer, `com.apple.vm.networking` entitlement); lifecycle code stops fighting AppleScript.

Full Windows + Ubuntu lifecycle parity with the Proxmox track is the ultimate goal; this sub-project builds only the foundation plus one vertical slice (Win11 ARM64 template build). Ubuntu, clone / provisioning, autopilot / hash capture, and monitoring are each their own follow-up spec.

## 2. Goals

- Replace the AppleScript `make` + `plutil` patch flow with a single-shot Python plist generator.
- Stand up an unattended Windows 11 ARM64 template build that requires **zero interactive input** end-to-end, including virtio driver load during Setup.
- Eliminate the `plutil` / `PlistBuddy` layer from every UTM code path in the repo.
- Eliminate the UTM quit-and-relaunch-to-force-plist-reread step.
- Keep stock UTM.app as the VM runtime (no fork, no self-signed build, no entitlement loss).
- Provide the runtime primitives (`utmctl` wrapper, bundle writer, plist renderer, `qemu-img` wrapper) that follow-up sub-projects (Ubuntu, clone, autopilot, monitoring) will consume unchanged.

## 3. Non-goals

- Ubuntu ARM template build — sub-project 2.
- Clone / provisioning / SMBIOS-OEM / network override for UTM VMs — sub-project 3.
- Autopilot inject, hash capture, sequence orchestration on UTM — sub-project 4.
- Monitoring, VM screenshots, snapshot UI changes — sub-project 5.
- Upstream PRs to utmapp/UTM. Local fork is allowed if required, but not needed for this spec.
- Raw-QEMU runtime (no UTM.app). Considered and rejected — UTM's Cocoa renderer + networking entitlement are worth keeping.
- Bridged / host-only networking changes. Template build uses UTM's default shared-NAT mode; network overrides belong to sub-project 3.

## 4. Core concept

Write a valid `config.plist` in one shot from a Python dataclass tree, lay out the rest of the `.utm` bundle directory deterministically, hand it to UTM via `utmctl register`, then drive it via `utmctl`. The Python library owns bundle shaping. Ansible orchestrates. UTM runs.

Three places the hybrid approach hit friction — each gets a structural fix:

- **Bundle creation.** AppleScript `make` is replaced with a Python dict → `plistlib.dumps()` emission that UTM reads fresh on `utmctl register`. No post-hoc `plutil` calls exist.
- **EFI boot.** AAVMF on `qemu-aarch64 virt` does not auto-add removable USB CDs to the first-boot menu. We try `virt-fw-vars` to pre-populate `Boot0000` / `BootOrder` in the `efi_vars.fd` upfront (experimental, time-boxed); if the experiment doesn't converge inside the budget, fall back to a 5-line keystroke step instead of the current 30-line mash.
- **Virtio driver load.** `autounattend.xml` learns a `DriverPaths` block so Setup auto-loads `viostor\w11\ARM64` from the virtio-win CD instead of requiring a human click.

## 5. Architecture and components

### New code

| Path | Purpose |
|------|---------|
| `autopilot-proxmox/web/utm_bundle.py` | Library: dataclasses mirroring the UTM ConfigurationVersion 4 schema, `render_plist()`, `write_bundle()`, `utmctl` wrapper, `qemu-img` wrapper, `efi_vars` builder (via `virt-fw-vars` where used). CLI entrypoint `python -m autopilot.utm_bundle build` for Ansible to invoke. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/render_bundle.yml` | Three Ansible tasks — build spec JSON, shell out to the Python CLI, register with UTM. Replaces `create_bundle.yml` + `customize_plist.yml`. |
| `autopilot-proxmox/tests/test_utm_bundle.py` | pytest: plist shape, drive ordering, UUID casing, schema version, golden comparisons. |

### Modified code

| Path | Change |
|------|--------|
| `autopilot-proxmox/roles/utm_answer_iso/templates/unattend.xml.j2` | Add `<DriverPaths>` entries in the `windowsPE` pass pointing at `D:\viostor\w11\ARM64`, `E:\viostor\w11\ARM64`, `F:\viostor\w11\ARM64` + matching NetKVM paths. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/main.yml` | Replace the `create_bundle.yml` and `customize_plist.yml` includes with `render_bundle.yml`. |
| `autopilot-proxmox/playbooks/utm_build_win11_template.yml` | Delete the UTM quit-and-relaunch block. If the EFI-vars experiment converges: delete the keystroke block entirely. Otherwise: replace the 30-line keystroke dance with a 5-line minimal version behind `utm_boot_fallback_keystrokes: true`. |
| `requirements.txt` | Add `virt-firmware` (provides `virt-fw-vars`). Pin to a recent release. |

### Deleted code (after the new path proves itself)

| Path | Reason |
|------|--------|
| `autopilot-proxmox/roles/utm_template_builder/tasks/create_bundle.yml` | AppleScript `make` + PlistBuddy drive-UUID harvest. |
| `autopilot-proxmox/roles/utm_template_builder/tasks/customize_plist.yml` | Every `plutil -replace` / `plutil -insert`. |

### Unchanged (consumed as-is)

- `roles/utm_answer_iso/tasks/main.yml` and `firstboot.ps1.j2`
- `roles/oem_profile_resolver`
- `roles/utm_template_builder/tasks/start_and_wait.yml`, `sysprep_finalize.yml`
- Everything outside the UTM tree (Proxmox track, hash capture, sequence compiler, etc.)

### Boundary rules

- `utm_bundle.py` MUST NOT shell out to `osascript` or `plutil`. Plist emission is pure Python (`plistlib`).
- `utm_bundle.py` MAY shell out to `utmctl`, `qemu-img`, and `virt-fw-vars`.
- Ansible tasks MAY shell out to `osascript` ONLY for the keystroke fallback, behind a feature flag.

## 6. Data flow (end-to-end Win11 ARM64 template build)

```
playbook utm_build_win11_template.yml
  │
  ├─► role oem_profile_resolver       → facts: manufacturer, org_name, ...
  │
  ├─► role utm_answer_iso             → writes AUTOUNATTEND.iso (unattend.xml
  │                                     now includes DriverPaths for virtio)
  │                                     + firstboot.ps1 sentinel writer
  │
  ├─► role utm_template_builder (render_bundle.yml)
  │     │
  │     ├─► render spec JSON (vm_name, uuid, installer iso path,
  │     │                     answer iso path, virtio-win iso path,
  │     │                     cpu/memory/disk sizing, OEM inputs)
  │     │
  │     ├─► python -m autopilot.utm_bundle build --spec - --out <bundle path>
  │     │
  │     │   utm_bundle.py:
  │     │     1. read spec JSON from stdin
  │     │     2. mkdir <name>.utm/ and <name>.utm/Data/
  │     │     3. render config.plist via plistlib
  │     │     4. qemu-img create -f qcow2 Data/<disk-uuid>.qcow2 <size>G
  │     │     5. build Data/efi_vars.fd:
  │     │          - start from UTM's edk2-arm-secure-vars.fd
  │     │          - (experiment) use virt-fw-vars to add Boot0000
  │     │            + BootOrder pointing at the installer CD
  │     │          - fall back: copy unmodified
  │     │     6. copy installer.iso + AUTOUNATTEND.iso + virtio-win.iso into Data/
  │     │     7. emit JSON {"uuid", "bundle_path", "drive_uuids"} on stdout
  │     │
  │     └─► utmctl register <bundle_path>
  │
  ├─► role utm_template_builder (start_and_wait.yml — unchanged)
  │     │
  │     └─► utmctl start <uuid>
  │         (if EFI-vars experiment succeeded: boots installer directly)
  │         (if not: run minimal keystroke fallback — 5 lines)
  │
  ├─► poll loop (existing, unchanged)
  │     │
  │     └─► utmctl exec <uuid> -- powershell
  │              "Test-Path C:\autopilot\autopilot-firstboot.done"
  │         (45 min timeout, 10 s cadence)
  │
  └─► suspend + write autopilot-template.ready marker file
```

### Inputs → outputs

| Input | Source | Consumed by |
|-------|--------|-------------|
| `vm_name`, `utm_iso_name`, `vm_os_kind` | playbook vars | spec JSON |
| `utm_answer_admin_pass` | vault | answer ISO unattend |
| OEM profile (`manufacturer`, `org_name`, ...) | `oem_profile_resolver` | answer ISO unattend |
| `AUTOUNATTEND.iso` path | `utm_answer_iso` fact | spec JSON (Drive 2) |
| `virtio-win.iso` path | role default | spec JSON (Drive 3) |
| `edk2-arm-secure-vars.fd` | UTM.app `Contents/Resources/qemu/` | bundle `Data/` |
| VM UUID, drive UUIDs | generated in Python (not harvested from UTM) | plist + returned to Ansible as facts |

## 7. Plist generation

### Dataclass tree

```python
@dataclass
class BundleSpec:
    name: str                         # → Information.Name
    uuid: str                         # → Information.UUID (uppercased)
    system: SystemSpec
    qemu: QemuSpec
    drives: list[DriveSpec]           # order = boot order (bootindex auto-assigned)
    display: DisplaySpec              # parameterized; flex needed for Ubuntu
    network: NetworkSpec
    # Sharing, Sound, Input, Serial are baked-in defaults, promote to fields
    # as later sub-projects need them.

@dataclass
class SystemSpec:
    architecture: str = "aarch64"
    target: str = "virt"              # use "virt-8.2" or similar in practice
    memory_mib: int = 8192
    cpu_count: int = 4
    use_hypervisor: bool = True
    jit_cache_size: int = 0

@dataclass
class QemuSpec:
    uefi_boot: bool = True
    tpm_device: bool = True
    rtc_local_time: bool = True       # Windows prefers local-time RTC
    rng_device: bool = True
    balloon_device: bool = False
    debug_log: bool = False
    additional_arguments: list[str] = field(default_factory=list)

@dataclass
class DriveSpec:
    identifier: str                   # uppercased UUID
    image_type: str                   # "CD", "Disk"
    interface: str                    # "USB", "VirtIO", "IDE", "SCSI"
    interface_version: int = 1
    read_only: bool = False
    external: bool = False            # removable
    image_name: str | None = None     # filename inside Data/

@dataclass
class DisplaySpec:
    hardware: str = "virtio-ramfb-gl"
    dynamic_resolution: bool = True
    native_resolution: bool = True
    vga_ram_mib: int = 64

@dataclass
class NetworkSpec:
    hardware: str = "virtio-net-pci"
    mode: str = "shared"              # UTM default shared-NAT
    mac_address: str | None = None
```

### Render approach

Pure Python → `plistlib.dumps(..., fmt=plistlib.FMT_XML)`. No Jinja. The dataclasses serialize into a Python dict with exactly the PascalCase keys UTM's schema expects (`MemorySize`, `CPUCount`, `DynamicResolution`, `BridgeInterface`, `TPMDevice`, `ImageName`, etc. — pulled from `/Users/Adam.Gell/src/UTM/Configuration/UTMQemuConfiguration*.swift`). One `ConfigurationVersion: 4` constant lives at the top of `utm_bundle.py`.

### Baked-in defaults (constants in the renderer, promotable to dataclass fields later)

| Section | Default |
|---------|---------|
| `Input.UsbBusSupport` | `USB3.0` |
| `Input.HasUsbSupport` | `true` |
| `Input.MaximumUsbShare` | `3` |
| `Sharing.HasClipboardSharing` | `true` |
| `Sharing.DirectoryShareMode` | `none` (changed by later sub-projects if needed) |
| `Sound[0].Hardware` | `intel-hda` — omitted entirely on template builds (silent install) |
| `Serial` | empty array |

### Win11 ARM64 template drive list

| # | image_type | interface | external | image_name | purpose |
|---|------------|-----------|----------|------------|---------|
| 0 | CD | USB | true | `Win11_*.iso` | Installer (bootindex=0) |
| 1 | Disk | VirtIO | false | `<uuid>.qcow2` | System disk |
| 2 | CD | USB | true | `AUTOUNATTEND.iso` | Answer + firstboot |
| 3 | CD | USB | true | `virtio-win.iso` | Virtio drivers for Setup |

### Plist key correctness

Tests (Section 10) assert:
- Required keys present (`ConfigurationVersion`, `Backend`, `Information.UUID`, `System.*`, `Drive[]`, `Display[0]`, `Network[0]`).
- UUID fields uppercased (UTM rejects mixed-case; see commit 1eaa9d5).
- `ConfigurationVersion == 4`.
- Drive array preserves spec order.
- `QEMU.TPMDevice == true` when TPM requested.
- No keys we don't own (renderer doesn't emit unknown keys).

## 8. Unattended install fix

Two changes replace the interactive steps in today's Win11 ARM64 build.

### 8.1 EFI boot — experiment with fallback

**Root cause (from the current playbook comment):** EDK2/AAVMF on `qemu-aarch64 virt` does not auto-create NVRAM `Boot####` entries for removable USB CD devices on first boot. With the four-CD layout (installer + answer + virtio-win, all USB CDs), the firmware drops to the UEFI Shell.

**Experiment (preferred path):** pre-populate `efi_vars.fd` with an explicit `Boot0000` entry pointing at the installer CD and a `BootOrder` that lists it first, using `virt-fw-vars` (Python package `virt-firmware`, by Gerd Hoffmann).

```python
# sketch inside utm_bundle.py
from virt.firmware.efi.varstore import EdkVarStore  # exact import TBD per API

vars_in  = read_bytes(UTM_RESOURCES / "edk2-arm-secure-vars.fd")
vars_out = add_usb_cd_boot_entry(
    vars_in,
    description="Windows Setup",
    file_path="/EFI/BOOT/BOOTAA64.EFI",
    device_path=USB_CD_DEVICE_PATH,  # investigation artifact
)
write_bytes(bundle / "Data/efi_vars.fd", vars_out)
```

**Time budget for the experiment: one working day.** Exit criteria:

- Fresh bundle boots straight to Windows Setup three consecutive times with no keystrokes.
- Post-install reboot lands on the system disk (not back at the installer).

**Known risks:**
- UEFI Device Paths are topology-specific. A path crafted for `virt-8.2` may not match `virt-9.0`; pin the `System.target` and note the coupling.
- The underlying issue may be enumeration timing (BDS firing before `usb-storage` enumerates) rather than missing boot entries. If so, `Boot####` alone doesn't help and we may need to also set the `Timeout` UEFI variable or use a different bus for the installer.
- `virt-fw-vars` API surface is not widely documented; expect trial and error.

**Fallback if the experiment doesn't converge in budget:**

Replace the current 30-line keystroke mash with a 5-line minimal sequence, gated by `utm_boot_fallback_keystrokes: true`:

```applescript
tell application "UTM"
    set vm to virtual machine id "{{ _utm_assigned_uuid }}"
    input keystroke vm text "fs0:\\efi\\boot\\bootaa64.efi"
    key code 36          -- return
    delay 2
    input keystroke vm text " "   -- acknowledge "Press any key to boot from CD"
end tell
```

**Follow-up ticket if fallback is taken:** `utm-efi-vars-nvram-boot-entry` — continue the `virt-fw-vars` investigation outside the sub-project 1 deadline. Candidate follow-ups include setting the UEFI `Timeout` variable and testing whether moving the installer CD to VirtIO-SCSI (via `QEMU.AdditionalArguments` overriding UTM's default `lsi53c895a`) works with AAVMF's `VirtioScsiDxe`.

### 8.2 Virtio driver load during Windows Setup

`autounattend.xml` currently has no `DriverPaths`, so Setup reaches the disk picker, sees nothing (no virtio-blk in `boot.wim`), and requires a human click. Fix: add `DriverPaths` to the `windowsPE` pass in `roles/utm_answer_iso/templates/unattend.xml.j2`.

```xml
<settings pass="windowsPE">
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" ...>
        <DriverPaths>
            <PathAndCredentials wcm:action="add" wcm:keyValue="1">
                <Path>D:\viostor\w11\ARM64</Path>
            </PathAndCredentials>
            <PathAndCredentials wcm:action="add" wcm:keyValue="2">
                <Path>E:\viostor\w11\ARM64</Path>
            </PathAndCredentials>
            <PathAndCredentials wcm:action="add" wcm:keyValue="3">
                <Path>F:\viostor\w11\ARM64</Path>
            </PathAndCredentials>
            <PathAndCredentials wcm:action="add" wcm:keyValue="4">
                <Path>D:\NetKVM\w11\ARM64</Path>
            </PathAndCredentials>
            <PathAndCredentials wcm:action="add" wcm:keyValue="5">
                <Path>E:\NetKVM\w11\ARM64</Path>
            </PathAndCredentials>
            <PathAndCredentials wcm:action="add" wcm:keyValue="6">
                <Path>F:\NetKVM\w11\ARM64</Path>
            </PathAndCredentials>
        </DriverPaths>
    </component>
</settings>
```

Drive letters `D:`–`F:` cover the plausible enumeration range for the virtio-win CD depending on which USB slot it lands in. Setup silently skips missing paths.

## 9. Runtime control

Sub-project 1 keeps runtime control minimal. `utmctl` has `start`, `stop`, `status`, `exec`, `register`, `delete` — sufficient for the template-build flow.

### 9.1 `UtmctlClient` (in `utm_bundle.py`)

```python
class UtmctlClient:
    def __init__(self, utmctl_path: str = "/Applications/UTM.app/Contents/MacOS/utmctl"): ...
    def register(self, bundle_path: pathlib.Path) -> str: ...       # returns UUID
    def start(self, uuid: str) -> None: ...
    def stop(self, uuid: str, force: bool = False) -> None: ...
    def status(self, uuid: str) -> str: ...                         # "running", "stopped", ...
    def exec(self, uuid: str, cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess: ...
    def delete(self, uuid: str) -> None: ...
```

- Thin wrappers around `subprocess.run`. No retry logic in this sub-project — callers handle failure.
- JSON output parsed where `utmctl` supports it (status), plain text otherwise.

### 9.2 QMP / QGA — deferred

QEMU Machine Protocol (QMP) and Guest Agent (QGA) access are **not** in scope for sub-project 1. The template build flow uses `utmctl exec` for the sentinel poll, which already works. Sub-project 4 (autopilot / hash capture) will revisit this — QGA is probably the right primitive there for more reliable inside-guest calls.

### 9.3 Sentinel poll

Unchanged from today: `utmctl exec <uuid> -- powershell "Test-Path C:\autopilot\autopilot-firstboot.done"`, 270 retries × 10 s (45 min). Exists in `start_and_wait.yml`; that task is not touched by this sub-project.

## 10. Testing strategy

### 10.1 Unit tests (`tests/test_utm_bundle.py`)

Fast, pure-Python, no UTM or QEMU invocation.

- **Schema shape.** Build a `BundleSpec` for a Win11 template, render to dict, assert top-level keys (`ConfigurationVersion`, `Backend`, `Information`, `System`, `QEMU`, `Drive`, `Display`, `Network`, `Sharing`, `Input`) exist.
- **UUID casing.** All `Identifier` and `UUID` fields are uppercase.
- **Drive order preservation.** Four drives in → four drives out in the same order with correct `ImageType` / `Interface` / `ImageName`.
- **Win11-specific invariants.** `QEMU.TPMDevice == true`, `System.Architecture == "aarch64"`, `System.Hypervisor == true`, `QEMU.UEFIBoot == true`, `QEMU.RTCLocalTime == true`.
- **ConfigurationVersion pinning.** Test fails loudly if someone changes the constant without updating the schema.
- **Golden plist comparison.** Commit a `tests/fixtures/win11_template_expected.plist` — the exact bytes of a known-good render. Any unintentional schema drift breaks this test. Regenerate only with a PR comment explaining the change.
- **EFI vars build (if experiment succeeds).** Feed a known input `efi_vars.fd` + a spec; assert the output contains exactly one new `Boot####` entry with the expected description string.

### 10.2 Smoke test

A pytest-marked `slow` test that:
1. Builds a fresh bundle (but uses a dummy 1 MB ISO instead of the real Win11 installer).
2. Calls `utmctl register` on it.
3. Asserts `utmctl status <uuid>` returns a valid state.
4. Calls `utmctl delete`.

Skipped by default; runs when `TEST_UTM_SMOKE=1` is set. Requires UTM.app installed. ~30 s.

### 10.3 End-to-end validation

The full Win11 ARM64 template build is the E2E test. No automation — manual run of `ansible-playbook playbooks/utm_build_win11_template.yml -e vm_name=test-win11 -e utm_iso_name=Win11_25H2_English_Arm64.iso` on the Adam's macOS workstation. Completion criteria:
- No osascript keystrokes needed (EFI experiment succeeded) OR minimal 5-line fallback ran once.
- Windows Setup completes without any interactive input (including the disk picker).
- `autopilot-template.ready` marker file appears in the bundle after sysprep phase.

### 10.4 Plist compatibility check on UTM upgrade

When `/Applications/UTM.app` version changes, run the smoke test. If a rendered bundle no longer registers cleanly, bump `ConfigurationVersion` in `utm_bundle.py` and update any drifted keys. Document the UTM version coverage at the top of `utm_bundle.py`.

## 11. Migration and retirement

Two-phase commit, single branch (`feature/utm-macos-arm64-support` or a new child branch):

**Phase A — build alongside.**
- Add `utm_bundle.py` + template tests + `render_bundle.yml`.
- Leave `create_bundle.yml` and `customize_plist.yml` on disk, but stop including them from `main.yml`.
- Add `unattend.xml.j2` `DriverPaths`.
- Add `virt-firmware` to `requirements.txt`.
- Run an E2E template build. Confirm green.

**Phase B — retire old code.**
- Delete `create_bundle.yml`, `customize_plist.yml`.
- Delete UTM quit-and-relaunch block from `utm_build_win11_template.yml`.
- If EFI experiment succeeded: delete keystroke block. If not: replace with the 5-line fallback behind `utm_boot_fallback_keystrokes: true`.
- Update `docs/UTM_MACOS_ARM64.md` handoff notes — remove "Known unresolved: manual driver load"; remove references to the plutil / efi_vars.fd cp tricks; add a section describing the new `utm_bundle.py` entrypoint.
- Commit.

**No flag-guarded rollback.** If something breaks in Phase B, revert the specific commit. Old code is preserved in git.

**Existing bundles keep working.** Running VMs and already-registered bundles are untouched — the plist format is identical, only the code path that generates it changes. No user-facing migration needed.

## 12. Implementation task breakdown

To be consumed by `writing-plans` to produce `docs/superpowers/plans/2026-04-23-utm-native-lifecycle-foundation-plan.md`.

1. Skeleton `utm_bundle.py` with dataclasses and a stub `build` CLI that prints the spec JSON it received. Wire `render_bundle.yml` to call it. Unit tests for dataclass construction.
2. Plist renderer — dict construction + `plistlib.dumps`. Unit tests for schema shape, UUID casing, drive order, Win11 invariants. Commit the golden plist fixture.
3. Bundle writer — `mkdir`, `plistlib` write, `qemu-img create`, ISO copies, `efi_vars.fd` copy (unmodified). Smoke test.
4. `utmctl` wrapper — `register`, `start`, `stop`, `status`, `exec`, `delete`. Unit tests that mock `subprocess`.
5. Cut `main.yml` over from `create_bundle.yml`/`customize_plist.yml` to `render_bundle.yml`. First Phase A E2E build.
6. `autounattend.xml.j2` `DriverPaths` — XML edit, regenerate answer ISO, confirm Setup reaches OOBE without a driver click.
7. **Experiment:** `virt-fw-vars` integration. Time-box 1 working day. Two paths out — succeeds (delete all keystroke code) or doesn't (move to step 8).
8. Minimal keystroke fallback (only if step 7 didn't converge). Delete the 30-line keystroke block from the playbook; replace with the 5-line version.
9. Delete `create_bundle.yml` + `customize_plist.yml`. Update handoff notes. Phase B commit.

## 13. Open questions and risks

- **`virt-fw-vars` API specifics.** The exact call pattern for adding a `Boot0000` entry with a USB CD device path needs live investigation. Mitigation: time-boxed experiment (step 7), fallback planned.
- **UTM version drift.** ConfigurationVersion 4 is pinned to UTM 4.7.5. A UTM upgrade that bumps the config version will break our renderer. Mitigation: a version check in `utm_bundle.py` that refuses to run against an installed UTM with a higher `currentVersion`, with a clear "bump the constant and rerun tests" error message. Document UTM version coverage at the top of `utm_bundle.py`.
- **UUID-upper-casing drift.** UTM requires uppercase UUIDs in a few specific fields but not others (see commit 1eaa9d5). The golden-plist fixture catches regressions in either direction, but there's room for the exact rule to be miscoded. Mitigation: explicit unit tests per field.
- **VirtIO-win ARM64 driver stability.** Upstream Fedora virtio-win ARM64 builds are less battle-tested than x86. If `viostor\w11\ARM64` gives a Setup-time driver error, we fall back to attaching the system disk via `usb-storage` or `nvme` (both have in-box drivers in WinPE), losing VirtIO performance on the system disk. Track as a known limitation; not a blocker for the sub-project.
- **Ansible-to-Python fact handoff.** Spec JSON in → bundle JSON out pattern requires disciplined schema. A malformed or out-of-date spec JSON is a possible silent failure mode. Mitigation: schema-validate the input in the Python CLI (`pydantic` or hand-rolled), fail loudly with a helpful error.
- **Test flakiness around UTM.app's background state.** UTM may be mid-start or have a dialog open when `utmctl register` is called. Mitigation: `register` idempotent retry in the wrapper; documented "close modal dialogs before running the playbook" in the handoff notes.

## 14. Success criteria

Sub-project 1 is done when:
1. `ansible-playbook playbooks/utm_build_win11_template.yml -e vm_name=... -e utm_iso_name=...` produces a working Win11 ARM64 template with no human interaction from start to `autopilot-template.ready` marker, three runs in a row, on a clean host.
2. No `plutil` / `PlistBuddy` / `cp efi_vars.fd` calls remain in any UTM code path.
3. Keystroke automation either removed entirely (preferred) or reduced to the 5-line fallback behind a feature flag.
4. `tests/test_utm_bundle.py` green locally.
5. Handoff document updated to reflect the new architecture.
