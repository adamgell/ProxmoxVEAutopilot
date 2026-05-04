# WinPE-orchestrated Proxmox deploy

Date: 2026-05-04 (revised v2 after operator review)
Status: draft (awaiting operator review)

## 1. Goal

Add a second provisioning path to ProxmoxVEAutopilot that boots a target Proxmox VM into a custom Windows PE image, partitions and applies a stock Windows install image directly to disk, injects boot-critical and QGA-critical drivers, stages the Autopilot configuration JSON offline, writes BCD, and hands off to Specialize/OOBE/FirstLogon for the existing in-OS task sequence to run unchanged.

This is a **blank-disk image-apply path**, not a variant of the existing clone-and-inject path. The two paths are peers: today's clone path clones a sysprepped template and offline-injects `C:\Windows\Panther\unattend.xml`; the new WinPE path creates a configured-but-empty VM, boots WinPE, and lays down Windows from a stock install.wim. They share the sequence engine, OEM/SMBIOS facts, and the post-OOBE FLC pipeline; they do not share the disk-content lifecycle.

In one sentence: WinPE becomes a peer "phase 0" in front of the sequence engine, alongside Specialize / OOBE / FirstLogon, on a blank cloned-from-empty-template VM, and the existing clone path is unchanged.

This spec covers all three operator-stated features (image-apply deploy, pre-OS hash capture, full task-sequence orchestration). Hash capture in WinPE is gated to milestone 2 because the WMI provider it depends on is not guaranteed in WinPE; milestone 1 ships the deploy path with hash capture remaining in OOBE (today's behavior).

## 2. Non-goals

The following are explicitly out of scope; each is a separate future effort:

- Golden-image capture / sysprep workflow. The applied WIM is `sources/install.wim` extracted from a stock Windows 11 ISO already uploaded to Proxmox storage.
- PXE / iPXE / DHCP options / TFTP / WDS. Boot delivery is virtual CD-ROM attach via Proxmox API.
- Physical machines. Proxmox VMs only.
- ARM64 deploy. Existing UTM ARM64 path is unchanged. WinPE deploy here is x64 only.
- Replacing the existing clone-and-inject path. That path remains and is the default until the WinPE path is operator-validated end to end.
- Refactoring the existing FLC / autopilot_inject / autounattend pipeline.

## 3. Architecture overview

### 3.1 Existing clone-and-inject path (unchanged)

```
Operator -> Flask -> Ansible (proxmox_vm_clone) -> Proxmox /cluster/nextid
   -> clone sysprepped template -> offline-inject C:\Windows\Panther\unattend.xml
   -> start VM -> Setup specialize-online -> OOBE -> FirstLogon (FLC sequence)
   -> autopilot_inject role writes AutopilotConfigurationFile.json via QGA
   -> hash capture script in FLC
```

Setup's windowsPE pass does not run; Panther's unattend drives specialize directly.

### 3.2 New WinPE blank-disk path (this spec)

```
Operator -> Flask -> Ansible (proxmox_vm_winpe) -> Proxmox /cluster/nextid
   -> clone configured-but-empty WinPE template -> attach 3 ISOs (WinPE, Windows source, VirtIO)
   -> set boot order [ide2, scsi0] -> start VM
   -> WinPE boots -> Invoke-AutopilotWinPE.ps1 -> Flask /winpe/* endpoints
   -> partition disk (Recovery before C:) -> apply install.wim -> inject boot drivers from VirtIO ISO
   -> validate boot drivers present -> stage AutopilotConfigurationFile.json offline
   -> stage post-WinPE unattend offline -> bake BCD
   -> POST /winpe/done -> Flask detaches ide2 + sata0 (Windows source); VirtIO ISO stays
   -> wpeutil reboot -> Specialize (unattend installs QGA from VirtIO ISO) -> OOBE -> FirstLogon
   -> existing cleanup_answer_media equivalent detaches VirtIO ISO after specialize
```

Two attached templates exist:

| Template | Purpose | Contents |
|---|---|---|
| `windows11_clone_template_vmid` (today) | Clone path source | Sysprepped Windows 11; baked-in autounattend bundle |
| `winpe_blank_template_vmid` (new) | WinPE path source | Empty scsi0 disk on the right storage; same NIC/SCSI/UEFI/SMBIOS-bench config; no OS |

Cloning from the blank template (instead of `qm create` from scratch) lets us reuse the entire `proxmox_vm_clone` role with one new flag (`_skip_panther_injection: true`); we keep the `/cluster/nextid` allocation, the OEM profile resolution, the per-VM SMBIOS file generation, and the chassis-override path identically. The role's existing Panther offline-injection step becomes a no-op when the flag is set.

### 3.3 Phase boundaries

| Phase | Today (clone path) | New (WinPE path) |
|---|---|---|
| 0 windowsPE | not run | `Invoke-AutopilotWinPE.ps1` |
| Specialize | autounattend (Panther) | post-WinPE unattend (Panther) |
| OOBE | autounattend (Panther) | post-WinPE unattend (Panther) |
| FirstLogon | FLC list compiled by `sequence_compiler.py` | unchanged |
| Post-provision | Ansible reboot-cycle waits | unchanged |

The unattend XML staged by phase 0 contains all passes EXCEPT windowsPE (since Setup's windowsPE pass is bypassed entirely). The template is named `autounattend.post_winpe.xml.j2` for clarity (the older draft called it "Specialize-only," which was misleading because it also includes oobeSystem and FirstLogonCommands).

The sequence engine remains the single source of truth for "what gets done to a VM." We add a second compile target that emits a phase-0 action list consumable by the WinPE agent, and a second Jinja template the renderer picks when `provision_path = "winpe"`.

## 4. Data flow (one provisioning run)

1. Operator picks sequence S in Flask UI, picks `boot_mode = winpe`, clicks Provision.
2. Flask creates a `provisioning_runs` row with `state = "queued"`, `vmid = NULL`, `vm_uuid = NULL`, `provision_path = "winpe"`. The compiler runs:
   - `compile_winpe(S, resolver=...)` -> `CompiledWinPEPhase` (action list).
   - `compile(S, resolver=...)` -> `CompiledSequence` (existing struct).
   The unattend renderer is called with `phase_layout = "post_winpe"` and emits the post-WinPE template; bytes are persisted at `/var/lib/autopilot/runs/<run_id>/unattend.xml`. (Existing `answer_iso_cache` is bypassed for the WinPE path because the unattend ships via HTTP, not via an ISO.)
3. Flask launches `provision_proxmox_winpe.yml` with `-e run_id=<id>` and the same vault/inventory wiring as the clone path. Ansible:
   - Calls `/cluster/nextid`, gets the VMID.
   - Runs `oem_profile_resolver` (produces `_vm_identity.uuid`, `_smbios1_string` or `_smbios_args`, OEM facts).
   - POSTs `{run_id, vmid, vm_uuid: _vm_identity.uuid}` to Flask `/winpe/run/<run_id>/identity`. Flask updates the run row. From this point the `vm_uuid` stored in the run row is canonical and is used for `/winpe/register` matching.
   - Includes `proxmox_vm_clone` with `_template_vmid_override = winpe_blank_template_vmid` and `_skip_panther_injection = true`. Cloning happens, `update_config.yml` runs, SMBIOS args are set just like today.
   - Attaches three ISOs:
     - `ide2 = winpe-autopilot-amd64-<sha>.iso,media=cdrom`
     - `sata0 = <windows_source_iso>,media=cdrom`
     - `ide3 = <virtio_iso>,media=cdrom`
   - Sets `boot=order=ide2;scsi0`.
   - Starts the VM.
4. WinPE boots. `startnet.cmd` (baked into the WIM) runs `wpeinit`, then `drvload` for any virtio drivers baked into the WIM (defensive; primary driver source is the attached VirtIO ISO during phase 0), then launches `X:\autopilot\Invoke-AutopilotWinPE.ps1`.
5. Agent reads SMBIOS UUID via WMI (`Win32_ComputerSystemProduct.UUID`) and the primary MAC, POSTs `/winpe/register {vm_uuid, mac, build_sha}`, receives `{run_id, bearer_token, actions[]}`. Server matches `vm_uuid` against `provisioning_runs.vm_uuid`; mismatch returns 404.
6. Agent loops over `actions[]`. Each call brackets the action with `POST /winpe/step/<step_id>/result` (running, ok, error, with elapsed seconds and stdout/stderr tail). Step result responses include a refreshed bearer token (rolling 60-minute window from each step result). Actions:
   - `partition_disk`: diskpart script. **Layout matches existing**: GPT, EFI 100 MB FAT32, MSR 16 MB, **Recovery 1 GB (BEFORE Windows)**, then Windows NTFS (rest). This preserves the existing resize behavior (C: is last on disk).
   - `apply_wim`: `dism /apply-image` from the locally-attached Windows source ISO at D: (or E:/F:; agent probes drive letters for `\sources\install.wim`).
   - `inject_drivers`: `dism /image:V:\ /add-driver /driver:<virtio-iso-path> /recurse` against the locally-attached VirtIO ISO. Drivers staged: vioscsi, NetKVM, Balloon, vioserial, vioinput, viogpudo. Mirrors the `<DriverPaths>` set today's autounattend.xml stages from the VirtIO ISO at D:/E:/F:.
   - `validate_boot_drivers`: hard check that all of vioscsi.inf, vioser.inf (vioserial), netkvm.inf are present in the OS driver store under V:\Windows\System32\DriverStore\FileRepository. If any missing, fail the run; do not continue. (vioscsi missing = won't boot; vioserial missing = QGA won't work and Ansible's reboot-cycle waiter will hang.)
   - `stage_autopilot_config`: HTTPS GET `/winpe/autopilot-config/<run_id>` (returns the AutopilotConfigurationFile.json content for this run; only present when `autopilot_enabled = true` on the compiled sequence). Agent writes to `V:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json`, `mkdir -p` first. This replaces the QGA-based `autopilot_inject` role for the WinPE path.
   - `bake_boot_entry`: `bcdboot V:\Windows /s S: /f UEFI`.
   - `stage_unattend`: HTTPS GET `/winpe/unattend/<run_id>` (post-WinPE unattend), write to `V:\Windows\Panther\unattend.xml`.
   - `capture_hash` (M2 only, gated by `hash_capture_phase = "winpe"` on the sequence): invokes `Get-WindowsAutopilotInfo.ps1` (baked into the WIM); POSTs the hash JSON to `/winpe/hash`. If the WMI provider isn't usable, the script's existing failure mode is a non-zero exit with a message about MDM_DevDetail_Ext01; the action records the error and the run fails closed (no silent fallback). M1 omits this action entirely; hash capture stays in the FLC pipeline. See section 5.1 on the OA3Tool option being evaluated alongside.
7. Agent POSTs `/winpe/done {run_id}`. Flask calls Proxmox API with `body: { delete: "ide2,sata0" }` (per the existing `cleanup_answer_media.yml` pattern) AND sets `boot=order=scsi0`. The VirtIO ISO at ide3 stays attached because Specialize uses it to install QGA. Returns 200.
8. Agent runs `wpeutil reboot`.
9. VM reboots. Boot order is now `scsi0` only. UEFI boots Windows. Specialize runs against the staged `Panther\unattend.xml`, which:
   - Loads VirtIO drivers from ide3 (just like today's autounattend, since the post-WinPE template inherits the OEM driver paths block).
   - Installs QGA via the same FirstLogonCommand or specialize-pass logic the existing template uses.
   - Reads `C:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json` (already staged by phase 0) when Autopilot is enabled.
10. OOBE runs. FirstLogon runs the FLC sequence the existing engine compiled.
11. Post-OOBE: existing `cleanup_answer_media.yml` (or its equivalent invoked by the new playbook) detaches ide3 with `body: { delete: "ide3" }`. `wait_reboot_cycle.yml` and the existing hash file watcher run unchanged.

## 5. Components

### 5.1 WinPE x64 build pipeline

State today: a Windows 11 dev VM under UTM at `F:\BuildRoot` produces ARM64 WinPE artifacts plus one amd64 build from 2026-04-25. The build pipeline lives in that VM, not in this repo.

What we need:

- Promote the build pipeline scripts into `tools/winpe-build/` in this repo so they are versioned. Initial check-in is whatever currently lives in the VM, even if rough.
- Parameterize architecture: `build-winpe.ps1 -Arch amd64|arm64`. Output: `winpe-autopilot-<arch>-<sha>.{wim,iso,json,txt}`.
- Reproducibility: pin ADK version, virtio driver version, PowerShell module versions. Build manifest in the JSON file: input hashes, ADK version, package list, output SHA256.
- Bake-ins for amd64:
  - WinPE optional packages: `WinPE-WMI`, `WinPE-NetFX`, `WinPE-Scripting`, `WinPE-PowerShell`, `WinPE-StorageWMI`, `WinPE-DismCmdlets`, `WinPE-SecureStartup`.
  - PowerShell scripts: `Get-WindowsAutopilotInfo.ps1` (community version, current).
  - Drivers loaded into boot.wim via `dism /add-driver`: NetKVM (so the agent has a NIC), vioscsi (defensive, in case primary is virtio-scsi), vioserial. The main driver source is the attached VirtIO ISO during phase 0; bake-ins are a fallback that ensures WinPE itself can come up and reach the network even without an attached VirtIO ISO.
  - Custom: `X:\autopilot\Invoke-AutopilotWinPE.ps1`, `X:\autopilot\config.json` (Flask base URL placeholder; default `http://192.168.2.4:5000`).
  - `startnet.cmd`: runs `wpeinit`, then optional `drvload` for any virtio drivers found at `X:\autopilot\drivers\`, then launches the agent.
- Output upload: `make publish` uploads the produced ISO to Proxmox ISO storage as `winpe-autopilot-amd64-<sha>.iso`. Flask reads the most-recently-published artifact via `pvesh get /nodes/<node>/storage/<store>/content`.

Hash-capture mechanism evaluation (M2 deliverable): three candidates, validated against the actual amd64 build before merging M2:

1. `Get-WindowsAutopilotInfo.ps1` (script reads `root/cimv2/mdm/dmmap:MDM_DevDetail_Ext01`). Documented for full-OS / OOBE; community reports of WinPE working with WinPE-WMI + WinPE-Scripting + WinPE-MDAC packages but not guaranteed.
2. `OA3Tool.exe` from ADK (OEM-side hash generator). Designed to run in WinPE; emits an OA3 XML report containing the hardware hash. Adds an ADK-licensed binary to the bake-in.
3. Direct SMBIOS read + manual hash composition. Documented format but historically brittle.

M2 ships whichever validates first; a sequence flag selects between them per run if more than one works. M1 does not depend on this work.

### 5.2 In-WinPE agent (`Invoke-AutopilotWinPE.ps1`)

Single PowerShell file, baked into the WIM. Responsibilities:

- Boot logging to `X:\Windows\Temp\autopilot-winpe.log` (tee'd to console).
- Read SMBIOS UUID and primary MAC.
- Wait for network: retry `Test-NetConnection <flask_host>:5000` for up to 60 s before bailing.
- Register: `POST /winpe/register {vm_uuid, mac, build_sha}`. Response: `{run_id, bearer_token, actions[]}`. Bearer is HMAC-signed `{run_id, expires_at}`, valid 60 minutes; refreshed on every step-result POST.
- Action dispatcher: per action call `Invoke-Action-<kind>`; bracket each call with a `POST /winpe/step/<id>/result` (running, ok, error).
- On any action failure: stop the run, do **not** call `/winpe/done`, leave all attached ISOs and VM at the WinPE prompt with the log on screen and on-disk for operator inspection.
- On success: `POST /winpe/done`, then `wpeutil reboot`.

The script is self-contained (no external module installs at runtime). Actions for M1: `partition_disk`, `apply_wim`, `inject_drivers`, `validate_boot_drivers`, `stage_autopilot_config` (when applicable), `bake_boot_entry`, `stage_unattend`. Action `capture_hash` is wired in M2.

### 5.3 Flask endpoints (new, prefix `/winpe/`)

All endpoints accept JSON. Bearer auth required except `/winpe/register` (uses VM UUID match instead) and `/winpe/run/<id>/identity` (called by Ansible from inside the cluster, IP-pinned to a configured allowlist).

| Endpoint | Method | Purpose |
|---|---|---|
| `/winpe/run/<run_id>/identity` | POST | Ansible writes `{vmid, vm_uuid}` after `/cluster/nextid` and `oem_profile_resolver` complete. Updates `provisioning_runs` and flips state to `awaiting_winpe`. |
| `/winpe/register` | POST | VM identity -> run lookup. Body: `{vm_uuid, mac, build_sha}`. Server matches `vm_uuid` (canonical, from Ansible). Response: `{run_id, bearer_token, actions[]}`. |
| `/winpe/sequence/<run_id>` | GET | Re-fetch action list (idempotent). |
| `/winpe/autopilot-config/<run_id>` | GET | Returns the AutopilotConfigurationFile.json content for this run. 404 if `autopilot_enabled = false`. |
| `/winpe/unattend/<run_id>` | GET | Returns the post-WinPE unattend.xml that was rendered at compile time. |
| `/winpe/hash` | POST | M2: receives hash JSON, persists into existing hash store. |
| `/winpe/step/<step_id>/result` | POST | Step state telemetry. Updates run timeline. Response includes refreshed bearer token. |
| `/winpe/done` | POST | Triggers Proxmox API call to detach `ide2` + `sata0` and set boot order to `scsi0`. VirtIO ISO at ide3 stays. Marks run `awaiting_specialize`. |

Bearer token: stateless HMAC-signed `{run_id, expires_at}` with a per-install secret stored in `vault.yml`. 60-minute initial validity; every step-result POST returns a new token with reset expiry.

### 5.4 Sequence compiler additions (`web/sequence_compiler.py`)

Add `compile_winpe(sequence, *, resolver=None) -> CompiledWinPEPhase`. Existing `compile()` is unchanged for the clone path.

```python
@dataclass
class CompiledWinPEPhase:
    actions: list[dict] = field(default_factory=list)   # each: {kind, params}
    requires_windows_iso: bool = True
    requires_virtio_iso: bool = True
    expected_reboot_count: int = 1
    autopilot_config_payload: Optional[dict] = None     # filled when autopilot_enabled
```

The compiler always emits these phase-0 actions (in order) when called for a WinPE run:
`partition_disk`, `apply_wim`, `inject_drivers`, `validate_boot_drivers`, optional `stage_autopilot_config` (when `autopilot_enabled`), `bake_boot_entry`, `stage_unattend`. Plus `capture_hash` in M2 when `hash_capture_phase = "winpe"`.

These step kinds are compiler-injected, not operator-authored. The sequence editor never surfaces them.

Operator-authored step kinds keep their existing routing (FLC for run_script / install_app / intune_register; Specialize for set_computer_name / local_admin / domain_join). Routing is unchanged from today's clone path; only the windowsPE pass is replaced.

### 5.5 Unattend renderer additions (`web/unattend_renderer.py`)

Add `autounattend.post_winpe.xml.j2`. It is the existing template with the `<settings pass="windowsPE">` block removed (no DiskConfiguration, no ImageInstall, no UserData ProductKey, no PnpCustomizationsWinPE; those were Setup's job). Specialize, oobeSystem, and FirstLogonCommands blocks are byte-identical to today; the existing OEM/PnpCustomizationsNonWinPE blocks for vioserial driver-store staging stay (so QGA still works once Specialize runs).

Renderer entry point grows a `phase_layout` arg:

```python
def render(compiled: CompiledSequence, *, phase_layout: str = "full") -> bytes:
    # phase_layout in {"full", "post_winpe"}
```

`full` is the default and matches today's clone-path output byte-for-byte. `post_winpe` is selected by the WinPE provisioning playbook.

### 5.6 `sequences_db.py` schema additions

```sql
ALTER TABLE task_sequences ADD COLUMN hash_capture_phase TEXT NOT NULL DEFAULT 'oobe'
  CHECK (hash_capture_phase IN ('winpe','oobe'));

CREATE TABLE IF NOT EXISTS provisioning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vmid INTEGER,                              -- nullable; populated by Ansible after /cluster/nextid
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id),
    provision_path TEXT NOT NULL CHECK (provision_path IN ('clone','winpe')),
    state TEXT NOT NULL,                       -- queued|awaiting_winpe|awaiting_specialize|firstlogon|done|failed
    vm_uuid TEXT,                              -- set by Ansible from _vm_identity.uuid
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_provisioning_runs_vm_uuid_state
    ON provisioning_runs(vm_uuid, state);

CREATE TABLE IF NOT EXISTS provisioning_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES provisioning_runs(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    phase TEXT NOT NULL,                       -- winpe|specialize|oobe|firstlogon
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,                       -- pending|running|ok|error
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
```

Existing `vm_provisioning` table stays for backward compatibility; new code writes to both during M1, reads from `provisioning_runs` after.

`vmid` is nullable because Ansible owns VMID allocation (`/cluster/nextid`). Flask creates the run row at submit time with `vmid = NULL, vm_uuid = NULL`, and Ansible POSTs identity once both are known.

Migration: on `init()`, `ALTER TABLE` adds `hash_capture_phase` (idempotent via `PRAGMA table_info`). New tables are `IF NOT EXISTS`.

### 5.7 Provisioning playbook (`playbooks/_provision_proxmox_winpe_vm.yml`)

Mirrors the structure of `playbooks/_provision_clone_vm.yml`. Key differences:

- Uses `proxmox_vm_clone` role with `_template_vmid_override = winpe_blank_template_vmid` and `_skip_panther_injection = true` (new variable; defaults to false; existing role gates the existing inject_unattend include on it).
- After `oem_profile_resolver` and `/cluster/nextid`: POSTs `/winpe/run/<run_id>/identity {vmid, vm_uuid: _vm_identity.uuid}` to Flask. This is the canonical UUID source per memory; we never parse `qm config`.
- Attaches three ISOs via `update_config.yml` (or a sibling `attach_winpe_media.yml`):
  - `ide2 = winpe-autopilot-amd64-<sha>.iso,media=cdrom`
  - `sata0 = <windows_source_iso>,media=cdrom`
  - `ide3 = <virtio_iso>,media=cdrom`
  - Uses the existing API token + form-urlencoded pattern.
- Sets `boot = order=ide2;scsi0`.
- Starts the VM.
- Replaces the `wait_for_specialize_complete` task with `wait_for_run_state(awaiting_specialize | failed)` polling Flask.
- After the run reaches `awaiting_specialize`, follows the existing `wait_reboot_cycle` + hash-file pattern from the clone path. Detaches ide3 via the existing `cleanup_answer_media.yml` (or a parameterized variant) using `body: { delete: "ide3" }`.

Top-level wrapper `provision_proxmox_winpe.yml` is the entry point invoked by Flask, mirroring `provision_clone.yml`.

### 5.8 Web UI changes (minimal)

- Sequence editor: add `Hash capture phase` dropdown (`oobe` / `winpe`). Default `oobe`. M2 enables `winpe` value; M1 hides it or grays it out.
- Provisioning page (`/devices/<vmid>/provision`): add `Boot mode` toggle (`Clone` / `WinPE`). Default `Clone`. WinPE option hidden if no `winpe-autopilot-amd64-*.iso` is present in Proxmox ISO storage OR no `winpe_blank_template_vmid` is configured.
- New page `/runs/<run_id>` showing the timeline of `provisioning_run_steps` with per-step state, elapsed time, and error tail. Same data source for both paths so the existing clone path also gets a visible timeline as a side benefit.

## 6. Boot media management

Proxmox VM lifecycle for a WinPE run:

| Stage | ide2 | sata0 | ide3 (auto-attached by clone role) | scsi0 | Boot order |
|---|---|---|---|---|---|
| Pre-clone | n/a | n/a | n/a | n/a | n/a |
| Cloned, ISOs attached, before start | WinPE | Windows source | VirtIO | empty disk | `ide2;scsi0` |
| Phase 0 running | same | same | same | same | same |
| `/winpe/done` callback | detached (`delete: ide2`) | detached (`delete: sata0`) | VirtIO | OS disk (now populated) | `scsi0` |
| Specialize/OOBE | empty | empty | VirtIO | OS disk | `scsi0` |
| Post-Specialize cleanup | empty | empty | detached (`delete: ide3`) | OS disk | `scsi0` |

Detach pattern uses the existing `cleanup_answer_media.yml` form: read config, conditionally `body: { delete: "<slot>" }`. Two detaches happen at different times: ide2+sata0 by Flask on `/winpe/done` (so reboot lands on scsi0 cleanly), ide3 by Ansible after Specialize so QGA-from-VirtIO-ISO has a chance to install.

If the agent crashes after attach but before `/winpe/done`, all three ISOs stay attached and the VM stays at the WinPE prompt. Operator sees the run as `failed` in the UI with the last step's error tail. Recovery is to re-run provisioning (which re-issues the playbook) or roll back the clone.

## 7. Network and security

Network: VM gets DHCP from the Proxmox SDN. Flask host (`192.168.2.4:5000`) is on the same L2 segment per existing infra topology. WinPE agent reaches it directly. No DNS dependency: agent uses an IP literal from `X:\autopilot\config.json`.

Security boundaries (deliberately minimal, internal-network model):

- Bearer tokens HMAC-signed `{run_id, expires_at}`. 60-minute validity, refreshed on every step result. No CSRF/cookie surface.
- VM identity is the canonical SMBIOS UUID written by Ansible into the run row before VM start. `/winpe/register` only succeeds when the SMBIOS UUID reported by the VM matches a row in `awaiting_winpe` state.
- `/winpe/run/<id>/identity` is IP-allowlisted to controller hosts (Ansible runs from there).
- WIM is read directly from the locally-attached Windows source ISO, not over the network.
- AutopilotConfigurationFile.json crosses the local SDN unencrypted (HTTP). MITM on the local SDN is out of scope; the threat model is the same as today's clone-path autopilot_inject role. A future spec can move to mTLS.

## 8. Failure modes

| Failure | Detection | Behavior |
|---|---|---|
| WinPE can't reach Flask | retry timeout in agent | abort phase 0; leave VM at WinPE prompt; on-screen error |
| Disk too small for WIM | dism error | abort; clear log entry pointing at WIM size vs disk size |
| Driver injection partial or `validate_boot_drivers` fails | dism warnings or check fails | **abort run, no continue.** vioscsi missing means won't boot; vioserial missing means QGA won't work and Ansible's reboot-cycle waiter hangs. No silent fallback. |
| `stage_autopilot_config` fails when `autopilot_enabled` | non-200 from Flask | abort; do not let the VM come up Autopilot-enabled-but-misconfigured. |
| `/winpe/done` call fails | server 5xx or network | agent retries 3x then aborts and stays at prompt (do NOT reboot, because boot order still has WinPE first; reboot would loop). |
| Reboot lands on WinPE again | agent re-registers, server sees state already past `awaiting_winpe` | server returns 409, agent halts. |
| Hash capture (M2) fails | non-zero exit from chosen tool | record step error, fail run. No silent fallback to OOBE; operator decides whether to retry or rerun in `hash_capture_phase = "oobe"` mode. |
| Specialize-pass driver install fails post-WinPE | existing detection (QGA never comes up) | Same recovery as today's clone path. |
| QGA never registers post-Specialize | existing QGA wait timeout | likely vioserial issue surfaced too late; M1 should have caught this in `validate_boot_drivers`. If it slipped past, treat as today: fail the run, operator inspects. |

There is no silent fallback path in WinPE phase 0. A failed WinPE run stays failed; operator decides whether to retry or fall back to clone mode for that VM.

## 9. Testing

Unit:

- `sequence_compiler.compile_winpe` for each step kind: routing, parameter resolution, credential lookups, `autopilot_config_payload` population.
- `unattend_renderer.render(..., phase_layout="post_winpe")` byte-exact fixture.
- Bearer token signing + verify, including refresh-on-step-result.
- `/winpe/register` UUID match logic (correct + mismatched + state-wrong + missing identity yet).
- `/winpe/run/<id>/identity` IP allowlist + idempotency.

Integration:

- Flask test client exercises the full `/winpe/*` surface against an in-memory sqlite.
- Pester test for `Invoke-AutopilotWinPE.ps1` against a mock HTTP server.

End-to-end (the M1 merge gate):

- One real run on pve1: blank template clone + WinPE boot + apply + drivers + reboot + Specialize + OOBE + FirstLogon, asserting QGA registers within the existing wait timeout and the FLC sequence runs to completion. The M1 merge does NOT depend on hash capture in WinPE.

The existing clone-path test suite is unmodified.

## 10. Migration

- Existing sequences are unchanged. `hash_capture_phase` defaults to `oobe`. `provision_path` is per-run, defaulting to `clone`.
- Existing `vm_provisioning` table is read-only legacy; new code writes `provisioning_runs`. A back-compat shim populates `vm_provisioning` on `provisioning_runs` insert for the lifetime of M1.
- New step kinds (`partition_disk`, `apply_wim`, etc.) are compiler-injected, not operator-authored. The sequence editor is unchanged in M1.
- `winpe_blank_template_vmid` is a new operator-set inventory variable; if unset, the WinPE option is hidden in the UI.

## 11. Known unknowns

- Whether the chosen hash-capture mechanism (Get-WindowsAutopilotInfo / OA3Tool / direct SMBIOS) actually works in our amd64 WinPE bake. Validation happens in M2; M1 ships without it.
- DHCP option 252 (Flask base URL discovery) is a future nice-to-have. Initial implementation hardcodes the Flask host in `config.json`.
- Whether reading the WIM from an attached ISO matches dism throughput expectations on slower Proxmox storage backends. If not, the fallback HTTP path from `/winpe/wim/<edition>` (not in M1; a future spec) becomes the answer.
- Whether the post-WinPE template's existing `Microsoft-Windows-PnpCustomizationsNonWinPE` driver-store staging actually picks up vioserial when the windowsPE pass is gone. Our current understanding is yes (offlineServicing pass runs against the OS image at first boot regardless of whether windowsPE ran), but this is something the M1 e2e test must confirm.

## 12. Out of scope (re-stated for the implementation plan)

Anything in section 2, plus:

- Refactoring the existing FLC pipeline.
- Changes to the existing Ansible clone playbooks.
- Changes to the existing UTM ARM64 path.
- Replacement of `vm_provisioning` table.
- HTTP-served install.wim (on-roadmap fallback only).

## 13. Milestones

**M1 -- blank-disk WinPE deploy with QGA up at first logon**

Ships:

- `tools/winpe-build/` (promoted from the build VM, parameterized for amd64).
- `Invoke-AutopilotWinPE.ps1` agent and config.json.
- New Flask endpoints (all of section 5.3 except `/winpe/hash`).
- `compile_winpe` and `CompiledWinPEPhase` in sequence_compiler.
- `autounattend.post_winpe.xml.j2` and `phase_layout` in unattend_renderer.
- Schema migration (provisioning_runs, provisioning_run_steps, hash_capture_phase column).
- `_skip_panther_injection` flag on proxmox_vm_clone role.
- `_provision_proxmox_winpe_vm.yml` and `provision_proxmox_winpe.yml` playbooks.
- `winpe_blank_template_vmid` inventory variable.
- UI: Boot mode toggle on provision page; runs/<id> timeline.
- Hash capture stays in OOBE FLC (existing path).
- Phase-0 actions: partition_disk, apply_wim, inject_drivers, validate_boot_drivers, stage_autopilot_config (when applicable), bake_boot_entry, stage_unattend.

Merge gate: one real e2e run on pve1, QGA registers within existing timeout, FLC sequence completes, hash captured by FLC step as today.

**M2 -- pre-OS hash capture in WinPE**

Ships:

- `Get-WindowsAutopilotInfo.ps1` baked into the WIM (or OA3Tool, whichever validates).
- `capture_hash` action wired into `Invoke-AutopilotWinPE.ps1`.
- `/winpe/hash` endpoint.
- `Hash capture phase` dropdown enabled in sequence editor.
- Per-VM toggle between WinPE and OOBE hash capture (so failures are recoverable without code changes).

Merge gate: e2e run with `hash_capture_phase = "winpe"` succeeds, hash matches the OOBE-captured value for the same hardware identity.

**M3 -- additional WinPE actions (optional, post-M2)**

Candidate scope: BIOS settings via vendor tools, driver pre-staging from a curated repo, captured-image apply, parallel multi-disk apply. Each gets its own mini-spec.
