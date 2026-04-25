# UTM End-to-End Sequence Runbook

Audience: the operator (you, three weeks from now) running a complete
clone -> autopilot inject -> hash capture -> Intune upload sequence on a
UTM-built Windows 11 ARM64 template, on macOS Apple Silicon.

This runbook documents the wiring as it currently exists on the
`feature/utm-macos-arm64-support` branch. Every behavior claim cites a
file path and line number so you can verify against source rather than
trust this document.

## Status snapshot

The full sequence is NOT yet validated end-to-end against a real Intune
tenant on a UTM clone. Individual stages have been verified. See the
gap analysis at the bottom for the YELLOW/RED status of each step.

Before this runbook is "ready to drive E2E" the wiring gap in
`autopilot-proxmox/playbooks/_provision_utm_clone_vm.yml` (line 4
literally says "Autopilot inject is Phase 3", and the file does not
include the `autopilot_inject` role) must be closed. See gap RED-1.

## Prerequisites

### Host (macOS Apple Silicon)

- macOS 14+ on Apple Silicon (M1/M2/M3/M4).
- UTM 4.7.5+ installed at `/Applications/UTM.app` (signed release build,
  not a locally re-signed fork; see gotcha #4 in
  `docs/UTM_MACOS_ARM64.md` lines 108-110).
- `pwsh` (PowerShell Core) on PATH for the Intune upload step. Modules:

  ```sh
  pwsh -Command "Install-Module -Name Microsoft.Graph.Authentication -Force"
  pwsh -Command "Install-Module -Name WindowsAutopilotIntune -MinimumVersion 5.4.0 -Force"
  ```

  Source: `autopilot-proxmox/playbooks/upload_hashes.yml` lines 9-10.
- Ansible 2.16+ in a venv per `autopilot-proxmox/requirements.txt`.
- `qemu-img`, `xorriso`, and the macOS `plutil` binaries available
  (Homebrew: `brew install qemu xorriso`).

### Template VM (already built per sub-project 1)

- `utmctl list` shows the template VM in `stopped` state.
- `{bundle}/autopilot-template.ready` marker file exists.
  Source: `autopilot-proxmox/roles/utm_vm_clone/tasks/main.yml` lines
  156-164.
- QGA installed and bound: `vioser.sys` loaded, `QEMU-GA` service
  reaches `RUNNING` after first boot. Verified on acc-3 per
  `docs/UTM_MACOS_ARM64.md` lines 47-51.

### Intune / Entra prerequisites

- An Entra app registration with the following Microsoft Graph
  application permissions (admin-consented):
  - `DeviceManagementServiceConfig.ReadWrite.All`
  - `DeviceManagementManagedDevices.ReadWrite.All`
- Client secret generated for the app registration.
- Tenant ID, app (client) ID, and client secret entered into
  `autopilot-proxmox/inventory/group_vars/all/vault.yml` keys
  `vault_entra_app_id`, `vault_entra_tenant_id`,
  `vault_entra_app_secret`. Wired through to env vars
  `ENTRA_APP_ID/TENANT_ID/APP_SECRET` by
  `autopilot-proxmox/playbooks/upload_hashes.yml` lines 32-35.
- The vault file should be encrypted with `ansible-vault encrypt`
  before any push to a remote.

### Repo / inventory state

- `hypervisor_type: utm` in
  `autopilot-proxmox/inventory/group_vars/all/vars.yml` line 9.
- `utm_utmctl_path: /Applications/UTM.app/Contents/MacOS/utmctl`
  (line 15).
- `utm_documents_dir` resolves to
  `~/Library/Containers/com.utmapp.UTM/Data/Documents` (line 24).
- `utm_exec_scratch_dir: 'C:\Users\Public'` (line 19) -- whitespace-free
  path used by `_utm_guest_exec.yml` for stdout/stderr redirect files.
- `hash_output_dir: "{{ playbook_dir }}/../output/hashes"` (line 92)
  resolves to `autopilot-proxmox/output/hashes/` on the controller.
- A real `AutopilotConfigurationFile.json` deployed to
  `autopilot-proxmox/files/AutopilotConfigurationFile.json` (the
  current placeholder will not register devices to a real tenant -- see
  user memory `project_autopilot_profile.md`).

## Data flow

1. Template build (sub-project 1, complete):
   `playbooks/utm_build_win11_template.yml` -> `roles/utm_template_builder`
   produces a `stopped` Win11 ARM64 VM with QGA + UTM Guest Tools
   installed and a sentinel `C:\autopilot\autopilot-firstboot.done`.

2. Clone:
   `playbooks/provision_utm_clone.yml` ->
   `_provision_utm_clone_vm.yml` ->
   `roles/utm_vm_clone/tasks/main.yml`. The role applies SMBIOS +
   network overrides via `utm_vm_smbios_override` and
   `utm_vm_network_override`, boots the clone, and waits on the guest
   agent via the dispatcher in
   `roles/common/tasks/wait_guest_agent.yml`. The wait probes BOTH the
   SPICE file channel AND the exec channel
   (`roles/common/tasks/_utm_wait_guest_agent.yml` lines 14-73) because
   on Win11 ARM64 they come up minutes apart.

3. Autopilot inject (Proxmox-only today, see RED-1):
   On Proxmox, `playbooks/_provision_clone_vm.yml` line 18-24 includes
   `roles/autopilot_inject` after the post-TPM 60s pause. The role
   reads `files/AutopilotConfigurationFile.json` from the controller
   and writes it to `C:\Windows\Provisioning\Autopilot\` in the guest
   via the `guest_file_write.yml` dispatcher
   (`roles/autopilot_inject/tasks/main.yml` lines 6-26). The role is
   hypervisor-agnostic; only the calling playbook is missing the
   include.

4. Hash capture:
   `roles/hash_capture/tasks/main.yml` runs three sub-tasks:
   - `push_scripts.yml` writes `Get-WindowsAutopilotInfo.ps1` and a
     templated `CaptureHash.ps1` wrapper into `C:\ProgramData\APHVTools\`.
   - `execute_capture.yml` runs `powershell.exe -ExecutionPolicy Bypass
     -Command "& 'C:\ProgramData\APHVTools\CaptureHash.ps1'"` via the
     `guest_exec.yml` dispatcher; the wrapper invokes
     `Get-WindowsAutopilotInfo.ps1` which writes a CSV named
     `<serial>_hwid.csv` to `C:\ProgramData\APHVTools\HardwareHashes\`.
     A sentinel string `HASH_CAPTURED:<path>` is emitted on stdout
     (`tasks/push_scripts.yml` line 64).
   - `retrieve_csv.yml` runs `Get-Content -Path '<csv>' -Raw` via the
     dispatcher and saves the contents to
     `autopilot-proxmox/output/hashes/<vm_name>_hwid.csv` on the
     controller.

5. Intune upload:
   `playbooks/upload_hashes.yml` shells out to
   `scripts/upload_hashes.ps1` (pwsh). The script imports
   `Microsoft.Graph.Authentication` + `WindowsAutopilotIntune`,
   authenticates with `Connect-MgGraph -ClientSecretCredential`, and
   calls `Import-AutopilotCSV` per CSV in parallel jobs
   (`scripts/upload_hashes.ps1` lines 35-47). Success is registered as
   a new device entry under Intune > Devices > Enrollment > Windows >
   Windows Autopilot devices, keyed by the hardware hash.

## Step-by-step playbook invocations

All commands are run from `autopilot-proxmox/`. Replace
`win11-arm64-template` and `myclone-01` with your template / clone
names.

### Step 1: Verify the template is ready

```sh
/Applications/UTM.app/Contents/MacOS/utmctl list | grep win11-arm64-template
ls "$HOME/Library/Containers/com.utmapp.UTM/Data/Documents/win11-arm64-template.utm/autopilot-template.ready"
```

Both must succeed, status must read `stopped`.

### Step 2: Clone + boot + wait for guest agent

```sh
ansible-playbook playbooks/provision_utm_clone.yml --ask-vault-pass \
  -e hypervisor_type=utm \
  -e utm_template_vm_name=win11-arm64-template \
  -e vm_name=myclone-01 \
  -e capture_hardware_hash=false
```

Expected log markers:
- `UTM clone: perform the clone` -- `clone_vm.yml` runs `utmctl clone`.
- `UTM clone: verify cloned VM is stopped before editing plist`
  (`tasks/main.yml` line 38).
- `UTM clone: apply SMBIOS overrides to bundle` and `apply network
  overrides`.
- `UTM clone: start the new VM` (`start_vm.yml`).
- `UTM wait: probe SPICE file channel` -> `probe exec+redirect channel`
  -> `verify sentinel round-trip via file pull`. Each retries up to
  `guest_agent_timeout_seconds / guest_agent_poll_interval_seconds`.
- Playbook exits PLAY RECAP `failed=0`.

### Step 3: Inject Autopilot config + capture hash

Today the cleanest UTM-aware path is `retry_inject_hash.yml` which is
hypervisor-agnostic (it routes through the dispatcher hubs):

```sh
ansible-playbook playbooks/retry_inject_hash.yml --ask-vault-pass \
  -e hypervisor_type=utm \
  -e vm_vmid=myclone-01 \
  -e vm_name=myclone-01
```

Expected log markers:
- `Wait for guest agent` (re-uses the dispatcher; idempotent).
- `Read Autopilot config from controller` (slurps
  `files/AutopilotConfigurationFile.json`).
- `Create Autopilot directory in guest` -- `cmd.exe /c mkdir
  C:\Windows\Provisioning\Autopilot`. May report `ignore_errors: true`
  if the dir exists -- this is intentional
  (`autopilot_inject/tasks/main.yml` line 20).
- `Write Autopilot config to guest` -> `Autopilot config injected`.
- `Push capture scripts to guest` -- writes
  `Get-WindowsAutopilotInfo.ps1` and `CaptureHash.ps1`.
- `Run CaptureHash.ps1 in guest` -- expect ~60-120s on ARM64.
- Assert `'HASH_CAPTURED:' in out-data` succeeds
  (`execute_capture.yml` lines 16-23).
- `Save hardware hash CSV to controller` writes
  `output/hashes/myclone-01_hwid.csv`.

Verify CSV on controller:

```sh
ls -la output/hashes/
head -2 output/hashes/myclone-01_hwid.csv
```

The CSV must have a header row `Device Serial Number,Windows Product
ID,Hardware Hash[,Group Tag]` and exactly one data row.

### Step 4: Upload hash to Intune

```sh
ansible-playbook playbooks/upload_hashes.yml --ask-vault-pass
```

Expected log markers:
- `Check for hash files` -> `_hash_files.matched > 0`.
- pwsh subprocess prints `Found N hash file(s) to upload in parallel`.
- `Starting upload: myclone-01_hwid.csv` per file.
- `OK: myclone-01_hwid.csv` per file.
- `--- Upload Summary ---` with `Failed: 0`.

Verify in Intune:
1. Sign in to https://intune.microsoft.com.
2. Devices -> Enrollment -> Windows -> Windows Autopilot devices.
3. Find the device by serial number (matches
   `(Get-CimInstance Win32_BIOS).SerialNumber` from the guest, which
   is whatever `utm_vm_smbios_override` injected).

## Failure-mode catalogue

### F1: `utmctl exec` returns OSStatus -2700

Symptom: any `utmctl exec` call fails with stderr containing "QEMU
guest agent is not running or not installed on the guest" even though
the QGA service is RUNNING in the VM.

Diagnosis: known UTM 4.7.5 harness issue (gotcha #6 in
`docs/UTM_MACOS_ARM64.md` lines 145-149). `utmctl ip-address <vm>`
typically still works against the same channel.

Mitigations:
- Hash capture and autopilot_inject route through
  `_utm_guest_exec.yml` which DOES work (it uses
  `utmctl exec --cmd cmd.exe /c ...` with file redirect). The known
  break is in `sysprep_finalize.yml`
  (tracked as `utm-phase2-sysprep-via-qga`).
- If the dispatcher path itself starts failing, re-run the
  `wait_guest_agent.yml` block; it will fail-loud and tell you which
  channel did not come back.

### F2: QGA service in 1603 / Error 1920 state after firstboot

Symptom: `firstboot.log` in the guest shows MSI exit code 1603
following `qemu-ga-aarch64.msi` install.

Diagnosis: cosmetic only -- known issue
`utm-qga-msi-install-start-cosmetic-1603` (gotcha #9 in
`docs/UTM_MACOS_ARM64.md` lines 186-202). The service still ends up
RUNNING; firstboot.ps1 already treats QGA MSI non-zero as a warning.

Verify recovery: `sc.exe query QEMU-GA` from inside the guest must
report `STATE: 4 RUNNING`. If not, the offlineServicing pass did not
import vioserial -- see F4.

### F3: Guest agent never responds

Symptom: `_utm_wait_guest_agent.yml` fails with all three probes
returning rc!=0 or empty stderr round-trip.

Diagnosis steps:
1. `utmctl status <vm>` -- must be `started`.
2. `utmctl ip-address <vm>` -- if no IP, networking failed.
3. From inside the VM (use UTM's display window), check
   `C:\autopilot\firstboot.log` and confirm
   `C:\autopilot\autopilot-firstboot.done` exists.
4. `Get-PnpDevice -FriendlyName 'VirtIO Serial Driver'` from inside
   the VM -- must report `Status: OK`.

### F4: Stock Win11 ARM64 install missing vioserial

Symptom: `Get-PnpDevice` shows `VirtIO Serial Driver` missing; QGA MSI
installs but service blocks indefinitely on `CreateFile` for
`\\.\Global\org.qemu.guest_agent.0`.

Diagnosis: the offlineServicing pass did not run
`Microsoft-Windows-PnpCustomizationsNonWinPE` against the vioserial
INF. See gotcha #8 in `docs/UTM_MACOS_ARM64.md` lines 170-184.

Fix: rebuild the template -- this should not regress on the current
branch (commit `d192deb`).

### F5: EFI shell drop on first boot

Symptom: clone boots into the EFI shell instead of running Windows
Setup / OOBE.

Diagnosis: AAVMF did not auto-add the USB CD to BootOrder. Today the
fallback is a 5-line `osascript input keystroke` block gated on
`utm_boot_fallback_keystrokes: true`. See gotcha #5 in
`docs/UTM_MACOS_ARM64.md` lines 111-116.

Fix: the structural cure (write Boot0000 + BootOrder into
`efi_vars.fd` via `virt-fw-vars`) is tracked as
`utm-efi-vars-nvram-boot-entry`.

### F6: pwsh module install fails (no internet on macOS host)

Symptom: `Install-Module -Name WindowsAutopilotIntune` reports "PSGallery
is not registered" or proxy failure.

Fix:
```sh
pwsh -Command "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted"
pwsh -Command "Register-PSRepository -Default"
```

### F7: `Connect-MgGraph` returns `Insufficient privileges`

Symptom: `upload_hashes.ps1` prints `FAILED: ... Insufficient
privileges to complete the operation`.

Diagnosis: the Entra app registration is missing
`DeviceManagementServiceConfig.ReadWrite.All` (or admin consent was
not granted). Re-check Entra admin center > App registrations >
<your app> > API permissions.

### F8: `Import-AutopilotCSV` returns "ZtdDeviceAlreadyAssigned"

Symptom: per-CSV upload step reports the device is already in the
tenant.

Diagnosis: a previous run already uploaded this hash. Either delete
the device from Intune (Windows Autopilot devices > select > Delete)
and re-run, or treat as success.

### F9: AutopilotConfigurationFile.json placeholder still in repo

Symptom: clone enrolls but never moves past the OOBE sign-in screen,
or fails with "Tenant ID is invalid".

Diagnosis: the file at
`autopilot-proxmox/files/AutopilotConfigurationFile.json` still has
placeholder values. See user memory `project_autopilot_profile.md`.

Fix: replace with a real exported profile from Intune > Windows
Autopilot deployment profiles > <profile> > Export JSON.

## What good looks like

End state when the sequence is fully successful:

1. `utmctl status myclone-01` returns `started`.
2. The clone has rebooted into the OOBE flow specifically configured
   by the Autopilot profile (organization branding + sign-in URL
   match the tenant).
3. `autopilot-proxmox/output/hashes/myclone-01_hwid.csv` exists on
   controller and contains exactly one data row.
4. Intune > Devices > Enrollment > Windows > Windows Autopilot
   devices shows `myclone-01` (matched by serial number) with
   `Profile status: Assigned` against the deployment profile that
   `AutopilotConfigurationFile.json` was generated from.
5. Re-running the same sequence with `vm_name=myclone-02` produces a
   second device with a distinct serial (the SMBIOS override role
   produced unique values).

## Gap analysis

| Step | Status | Notes |
| --- | --- | --- |
| Template build | GREEN | T17 acc-1/2/3 all reached `stopped` autonomously. `docs/UTM_MACOS_ARM64.md` line 222. |
| `utm_vm_clone` (clone + SMBIOS + network + boot) | GREEN | Role wired and demonstrated on UTM. `roles/utm_vm_clone/tasks/main.yml` lines 34-114. |
| `wait_guest_agent` UTM dispatcher | GREEN | Dual-channel probe verified. `_utm_wait_guest_agent.yml` lines 14-90. |
| `guest_exec` / `guest_file_write` UTM dispatchers | YELLOW | Dispatchers proven (`_test_utm_dispatch.yml` exists), but not yet exercised end-to-end inside the autopilot_inject + hash_capture roles on a UTM clone. |
| `autopilot_inject` role | YELLOW | Role is hypervisor-agnostic per `docs/UTM_AUDIT_AUTOPILOT_INJECT.md`. Not yet validated against a UTM clone with real `AutopilotConfigurationFile.json`. |
| `_provision_utm_clone_vm.yml` autopilot wiring | RED-1 | File literally states "Autopilot inject is Phase 3" at line 4 and does NOT include `autopilot_inject`. Workaround: drive via `retry_inject_hash.yml` per Step 3 above. |
| `hash_capture` role | YELLOW | Proxmox-validated; `docs/UTM_AUDIT_HASH_CAPTURE.md` finds no UTM-blocking issue but flags the live capture as "validate before treating as production-ready". Default `hash_output_dir` already fixed for macOS. |
| `upload_hashes.yml` + `upload_hashes.ps1` | YELLOW | Code complete and hypervisor-agnostic (runs on the controller, doesn't touch UTM). Never actually run against a real Intune tenant from this repo per project notes. |
| Real `AutopilotConfigurationFile.json` | RED-2 | Repo has a placeholder; per user memory `project_autopilot_profile.md` it must be replaced before any production enrollment test. |
| Entra credentials in vault | YELLOW | `vault.yml` has values populated (verified at lines 9-11). Vault file is unencrypted in the working tree -- encrypt before push. |
| `utmctl exec` OSStatus -2700 in `sysprep_finalize.yml` | RED-3 | Tracked as `utm-phase2-sysprep-via-qga`. Does not block clone -> hash -> upload because that path uses `--cmd` redirect, but it does mean the template build's final sysprep step today depends on a workaround (FirstLogonCommand-driven sysprep). |

Counts: GREEN 3, YELLOW 5, RED 3.

## Pointers for future work

- Close RED-1: edit `playbooks/_provision_utm_clone_vm.yml` to mirror
  `_provision_clone_vm.yml` lines 18-29. The role is already
  hypervisor-agnostic so this is a 12-line include block plus a
  `vm_vmid` set_fact.
- Close RED-2: replace
  `autopilot-proxmox/files/AutopilotConfigurationFile.json` with a
  real exported profile.
- Close RED-3: track upstream UTM 4.7.x for the OSStatus -2700 fix or
  pivot `sysprep_finalize.yml` to a FirstLogonCommand-driven sysprep.
- After all three RED items are closed, the YELLOW items resolve to
  GREEN by simply running this runbook end to end against a real
  tenant.
