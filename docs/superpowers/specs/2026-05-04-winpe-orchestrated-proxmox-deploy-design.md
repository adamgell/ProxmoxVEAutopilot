# WinPE-orchestrated Proxmox deploy

Date: 2026-05-04
Status: draft (awaiting operator review)

## 1. Goal

Add a second provisioning path to ProxmoxVEAutopilot that boots a target Proxmox VM into a custom Windows PE image, captures the Autopilot hardware hash, applies the Windows install image directly to disk, injects drivers, writes BCD, and hands off to Specialize/OOBE/FirstLogon for the existing in-OS task sequence to run unchanged.

The new path replaces the windowsPE pass of `setup.exe` (partition + image-apply + initial unattend) but leaves Specialize and FirstLogon (and therefore the existing FLC-based sequence engine) intact. It is selectable per provisioning run; the existing autounattend-only path stays supported and is not removed.

In one sentence: WinPE becomes a peer "phase 0" in front of the sequence engine, alongside the Specialize / OOBE / FirstLogon phases the engine already targets.

This spec covers all three operator-stated features (image-apply deploy, pre-OS hash capture, full task-sequence orchestration) as a single shipping unit.

## 2. Non-goals

The following are explicitly out of scope for this spec; each is a separate future effort:

- Golden-image capture / sysprep workflow. The applied WIM is `sources/install.wim` extracted from a stock Windows 11 ISO already uploaded to Proxmox storage. Custom captured images are deferred.
- PXE / iPXE / DHCP options / TFTP / WDS. Boot delivery is virtual CD-ROM attach via Proxmox API.
- Physical machines. Proxmox VMs only.
- ARM64 deploy. Existing UTM ARM64 path is unchanged. WinPE deploy in this spec is x64 only. (The existing build VM produces ARM64 WinPE artifacts; reusing that pipeline for x64 is sub-section 5.1.)
- Replacing the existing autounattend-injection path. That path remains and is the default until the WinPE path is operator-validated end to end.

## 3. Architecture overview

Today, the existing flow is:

```
Operator -> Flask -> Ansible -> Proxmox clone -> VM boots stock ISO + injected autounattend.xml
   -> windowsPE pass (Setup.exe partitions + applies install.wim) -> Specialize -> OOBE -> FirstLogon (FLC sequence)
```

New flow when `provision_path = "winpe"`:

```
Operator -> Flask -> Ansible -> Proxmox clone -> VM attached: WinPE ISO + Windows source ISO
   -> WinPE boots -> Invoke-AutopilotWinPE.ps1 -> Flask /winpe/* endpoints
   -> capture hash -> partition -> apply install.wim -> inject drivers -> bake BCD -> stage Specialize-only autounattend
   -> Flask /winpe/done detaches both ISOs and updates boot order via Proxmox API
   -> wpeutil reboot -> Specialize -> OOBE -> FirstLogon (FLC sequence runs unchanged)
```

Phase boundaries:

| Phase | Today | New (WinPE path) |
|---|---|---|
| 0 windowsPE | `setup.exe` driven by injected autounattend | `Invoke-AutopilotWinPE.ps1` driven by orchestrator step list |
| Specialize | autounattend Specialize pass | Specialize-only autounattend dropped onto OS partition by phase 0 |
| OOBE | autounattend OOBE pass | same Specialize-only autounattend covers OOBE |
| FirstLogon | FLC list compiled by `sequence_compiler.py` | unchanged |
| Post-provision | Ansible waits for hash file via QGA | unchanged |

The sequence engine (`sequences_db.py`, `sequence_compiler.py`, `unattend_renderer.py`) remains the single source of truth for "what gets done to a VM." We add a second compile target that emits a phase-0 step list consumable by the WinPE agent, and a Specialize-only Jinja template the renderer can pick when the WinPE path is selected.

## 4. Data flow (one provisioning run)

1. Operator picks sequence S in Flask UI, picks `boot_mode = winpe`, clicks Provision.
2. Flask `compile_sequence(S, target=winpe)` returns:
   - `winpe_actions`: ordered list of phase-0 actions (capture_hash, partition_disk, apply_wim, inject_drivers, bake_boot_entry, stage_unattend) with parameters resolved.
   - `compiled_sequence` (existing struct), but unattend renderer emits `autounattend.specialize-only.xml.j2` instead of the full template (no windowsPE pass).
3. Flask persists the run record and stages the rendered Specialize-only autounattend at `/var/lib/autopilot/runs/<run_id>/unattend.xml`.
4. Ansible playbook `_provision_proxmox_winpe_vm.yml` clones the template VM, sets the per-VM SMBIOS args, attaches the WinPE ISO at `ide2` and the Windows source ISO at `sata0`, sets boot order to `[ide2, scsi0]`, starts the VM. (Existing `_provision_clone_vm.yml` is left alone.)
5. WinPE boots. `startnet.cmd` (baked into the WIM) launches `X:\autopilot\Invoke-AutopilotWinPE.ps1`.
6. Agent reads SMBIOS UUID via WMI, POSTs `/winpe/register {vm_uuid, mac}`, receives `{run_id, bearer_token, actions[]}`.
7. Agent loops over `actions[]`. For each action it POSTs progress to `/winpe/step/<step_id>/result`. Actions and what they do:
   - `capture_hash`: invokes `Get-WindowsAutopilotInfo.ps1` (baked into WIM), POSTs the hash JSON to `/winpe/hash`.
   - `partition_disk`: diskpart script: GPT, EFI 100 MB FAT32, MSR 16 MB, OS NTFS (rest minus 1 GB), WinRE 1 GB.
   - `apply_wim`: streams the WIM from the attached Windows source ISO (D:) using `dism /apply-image`. (Source is the locally-attached ISO; HTTP serving from Flask is a fallback only.)
   - `inject_drivers`: `dism /image:V:\ /add-driver /driver:E:\ /recurse` against a virtio driver path resolved from the WIM bake-in (NetKVM at minimum; viostor/vioscsi are in-box on Win11 but added defensively).
   - `bake_boot_entry`: `bcdboot V:\Windows /s S: /f UEFI`.
   - `stage_unattend`: HTTPS GET `/winpe/unattend/<run_id>` (Specialize-only autounattend), write to `V:\Windows\Panther\unattend.xml`.
8. Agent POSTs `/winpe/done {run_id}`. Flask calls Proxmox API to detach `ide2` + `sata0` and set boot order to `[scsi0]`. Returns 200.
9. Agent runs `wpeutil reboot`.
10. VM reboots. UEFI picks scsi0 (the only boot entry now). Specialize runs against the staged `Panther\unattend.xml`. OOBE runs. FirstLogon runs the FLC sequence the existing engine compiled. Existing `wait_for_hash_file` Ansible task remains as a belt-and-suspenders fallback if `produces_autopilot_hash` is set and `hash_capture_phase = "oobe"`.

## 5. Components

### 5.1 WinPE x64 build pipeline

State today: a Windows 11 dev VM under UTM at `F:\BuildRoot` produces ARM64 WinPE artifacts (multiple builds since 2026-04-26) plus one amd64 build from 2026-04-25. The build pipeline lives in that VM, not in this repo.

What we need:

- Promote the build pipeline scripts into `tools/winpe-build/` in this repo so they are versioned. Initial check-in is whatever currently lives in the VM, even if rough.
- Parameterize architecture: `build-winpe.ps1 -Arch amd64|arm64`. Output filename `winpe-autopilot-<arch>-<sha>.{wim,iso,json,txt}`.
- Reproducibility: pin ADK version, pin virtio driver version (the ISO already on Proxmox storage is the source), pin PowerShell module versions for `Get-WindowsAutopilotInfo`. Build manifest in the JSON file: input hashes, ADK version, package list, output SHA256.
- Bake-ins for amd64:
  - WinPE optional packages: `WinPE-WMI`, `WinPE-NetFX`, `WinPE-Scripting`, `WinPE-PowerShell`, `WinPE-StorageWMI`, `WinPE-DismCmdlets`, `WinPE-SecureStartup`.
  - PowerShell modules: `Get-WindowsAutopilotInfo` (current Microsoft community script).
  - Drivers: virtio NetKVM (so the VM has a NIC in WinPE), virtio viostor + vioscsi (defensive; Win11 has them in-box).
  - Custom: `X:\autopilot\Invoke-AutopilotWinPE.ps1`, `X:\autopilot\config.json` (Flask base URL placeholder, replaced at boot from DHCP option 252 if set, else hardcoded `http://192.168.2.4:5000`).
  - `startnet.cmd` runs `wpeinit` then launches the agent.
- Output upload: a `make publish` target uploads the produced ISO to Proxmox ISO storage as `winpe-autopilot-amd64-<sha>.iso`. Flask reads the most-recently-published artifact via `pvesh get /nodes/<node>/storage/<store>/content`.

### 5.2 In-WinPE agent (`Invoke-AutopilotWinPE.ps1`)

Single PowerShell file, baked into the WIM. Responsibilities:

- Boot logging to `X:\Windows\Temp\autopilot-winpe.log` (tee'd to console).
- Read SMBIOS UUID and primary MAC (via `Get-CimInstance Win32_ComputerSystemProduct` and `Get-NetAdapter`).
- Wait for network: retry `Test-NetConnection <flask_host>:5000` for up to 60 s before bailing.
- Register: `POST /winpe/register {vm_uuid, mac, build_sha}`. Response: `{run_id, bearer_token, actions[]}`. Bearer is short-lived (15 min), per-run, signed.
- Action dispatcher: for each action, call `Invoke-Action-<kind>`; bracket each call with a `POST /winpe/step/<id>/result` (running, ok, error, with stderr/stdout tail and elapsed seconds).
- On any action failure: stop the run, do **not** call `/winpe/done`, leave both ISOs attached and the VM at the WinPE prompt with the log on screen and on-disk for operator inspection. Operator can SSH to the Proxmox host or use VNC. (No silent retries; no stealth fallback to the autounattend path.)
- On success: `POST /winpe/done`, then `wpeutil reboot`.

The script is self-contained (no external module installs at runtime). All required modules are baked into the WIM at build time.

### 5.3 Flask endpoints (new, prefix `/winpe/`)

All endpoints accept JSON, all require bearer auth except `/winpe/register` and `/winpe/wim/...`:

| Endpoint | Method | Purpose |
|---|---|---|
| `/winpe/register` | POST | VM identity -> run lookup. Body: `{vm_uuid, mac, build_sha}`. Response: `{run_id, bearer_token, actions[]}`. Looks up `vm_provisioning` row by SMBIOS UUID match, finds the run with `provision_path = "winpe"` and `state = "awaiting_winpe"`. |
| `/winpe/sequence/<run_id>` | GET | Re-fetch action list (idempotent). |
| `/winpe/hash` | POST | Receives hash JSON `{serialNumber, hardwareHash, productKeyID, ...}`, persists into existing hash store, marks `hash_capture_phase_done = "winpe"`. |
| `/winpe/unattend/<run_id>` | GET | Returns the Specialize-only autounattend.xml that was staged at compile time. |
| `/winpe/wim/<edition>` | GET | (Fallback only.) HTTP range-served install.wim extracted from a stock Windows ISO already uploaded to Proxmox storage. Primary path streams from the locally-attached ISO inside WinPE. |
| `/winpe/step/<step_id>/result` | POST | Step state telemetry. Updates run timeline. |
| `/winpe/done` | POST | Triggers Proxmox API call to detach `ide2` + `sata0`, set boot order to `[scsi0]`, marks run `awaiting_specialize`. |

Bearer tokens are HMAC-signed `{run_id, expires_at}` with a per-install secret stored in `vault.yml`. Tokens are not stored server-side (stateless verify).

### 5.4 Sequence compiler additions (`web/sequence_compiler.py`)

Add a peer `compile_winpe(sequence, *, resolver=None) -> CompiledWinPEPhase`. Existing `compile()` is unchanged for the answerfile path.

```python
@dataclass
class CompiledWinPEPhase:
    actions: list[dict] = field(default_factory=list)   # each: {kind, params}
    requires_windows_iso: bool = True
    expected_reboot_count: int = 1                      # WinPE -> Specialize counts as one
```

Step kinds and their phase routing (the compiler consults a `_PHASE_OF` table):

| Step kind | Today | WinPE path |
|---|---|---|
| `set_oem_hardware` | ansible_vars (clone-time) | unchanged |
| `autopilot_entra` | FLC | **winpe** (capture_hash) + post-OOBE register (FLC, unchanged) |
| `local_admin` | unattend Specialize | unattend Specialize (unchanged) |
| `set_computer_name` | unattend Specialize | unattend Specialize (unchanged) |
| `domain_join` (future) | unattend Specialize | unattend Specialize |
| `run_script` | FLC | FLC (unchanged) |
| `install_app` | FLC | FLC (unchanged) |
| `intune_register` | FLC | FLC (unchanged) |

WinPE-path-only kinds (compiler-injected, never operator-authored, but visible in the run timeline):

- `partition_disk` (always present when `provision_path = "winpe"`)
- `apply_wim` (always present)
- `inject_drivers` (always present)
- `bake_boot_entry` (always present)
- `stage_unattend` (always present)
- `capture_hash` (present when `produces_autopilot_hash = 1` AND `hash_capture_phase = "winpe"`)

Operator authors a sequence the same way they do today; the compiler decides which steps need a WinPE-phase action and which need an FLC entry.

### 5.5 Unattend renderer additions (`web/unattend_renderer.py`)

Add `autounattend.specialize-only.xml.j2`. It is the existing template with the `<settings pass="windowsPE">` block removed and with the disk-config / image-install sections deleted. The Specialize and OOBE blocks are byte-identical to today (so existing FLC sequences render the same FirstLogonCommands).

Renderer entry point grows a `phase_layout` arg:

```python
def render(compiled: CompiledSequence, *, phase_layout: str = "full") -> bytes:
    # phase_layout in {"full", "specialize_only"}
```

`full` is the default and matches today's output byte-for-byte. `specialize_only` is selected by the WinPE provisioning playbook.

### 5.6 `sequences_db.py` schema additions

```sql
ALTER TABLE task_sequences ADD COLUMN hash_capture_phase TEXT NOT NULL DEFAULT 'oobe'
  CHECK (hash_capture_phase IN ('winpe','oobe'));

CREATE TABLE IF NOT EXISTS provisioning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vmid INTEGER NOT NULL,
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id),
    provision_path TEXT NOT NULL CHECK (provision_path IN ('answerfile','winpe')),
    state TEXT NOT NULL,             -- queued|awaiting_winpe|awaiting_specialize|firstlogon|done|failed
    vm_uuid TEXT,                    -- captured at clone time, used by /winpe/register
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS provisioning_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES provisioning_runs(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    phase TEXT NOT NULL,             -- winpe|specialize|oobe|firstlogon
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,             -- pending|running|ok|error
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
```

Existing `vm_provisioning` table stays for backward compatibility; new code reads `provisioning_runs` instead.

Migration: on `init()`, `ALTER TABLE` adds `hash_capture_phase` (idempotent via `PRAGMA table_info`). New tables are `IF NOT EXISTS`.

### 5.7 Provisioning playbook (`_provision_proxmox_winpe_vm.yml`)

Mirrors `_provision_clone_vm.yml`. Differences:

- Drops the `inject autounattend ISO` step (no per-VM unattend ISO when `provision_path = "winpe"`; the unattend is staged by the WinPE agent itself via `/winpe/unattend/<run_id>`).
- Adds `attach WinPE ISO at ide2` and `attach Windows source ISO at sata0`.
- Sets boot order to `ide2,scsi0`.
- Writes `vm_uuid` (read back from `qm config <vmid>` post-clone) into the `provisioning_runs` row so `/winpe/register` can match.
- Does not start QGA-wait until phase 0 reports `done` via `/winpe/done` (the run-state machine in Flask gates the playbook's `wait_for_run_state` task).

Top-level wrapper `provision_proxmox_winpe.yml` is the entry point invoked by Flask, mirroring `provision_clone.yml`.

### 5.8 Web UI changes (minimal)

- Sequence editor: add a `Hash capture phase` dropdown on the sequence-level edit page (`winpe` / `oobe`). Default `oobe` so existing sequences are unchanged.
- Provisioning page (`/devices/<vmid>/provision`): add a `Boot mode` toggle (`Answer file` / `WinPE`). Default `Answer file`. WinPE option is hidden if no `winpe-autopilot-amd64-*.iso` is present in Proxmox ISO storage.
- New page `/runs/<run_id>` showing the timeline of `provisioning_run_steps` with per-step state, elapsed time, and error tail. Same data source for both paths so the existing answerfile path also gets a visible timeline as a side benefit.

## 6. Boot media management

Proxmox VM lifecycle for a WinPE run:

| Stage | ide2 | sata0 | scsi0 | Boot order |
|---|---|---|---|---|
| Pre-provision | empty | empty | OS disk | `scsi0` |
| Phase 0 (clone done, before start) | WinPE ISO | Windows source ISO | OS disk | `ide2,scsi0` (boots WinPE first) |
| Phase 0 (running) | same | same | same | same |
| `/winpe/done` callback | detached | detached | OS disk | `scsi0` |
| Specialize/OOBE | empty | empty | OS disk | `scsi0` |

The detach + reorder is one Proxmox API call from Flask:

```
PUT /nodes/<node>/qemu/<vmid>/config?ide2=none&sata0=none&boot=order=scsi0
```

If the agent crashes after attach but before `/winpe/done`, both ISOs stay attached and the VM stays at the WinPE prompt. Operator sees the run as `failed` in the UI with the last step's error tail. Recovery is to re-run provisioning (which re-issues `/winpe/done` lookups and detaches ISOs) or to roll back the clone.

## 7. Network and security

Network: VM gets DHCP from the Proxmox SDN. Flask host (`192.168.2.4:5000`) is on the same L2 segment per existing infra topology. WinPE agent reaches it directly. No DNS dependency: agent uses an IP literal from `X:\autopilot\config.json`.

Security boundaries (deliberately minimal, internal-network model):

- Bearer tokens are HMAC-signed `{run_id, expires_at}`. No CSRF/cookie surface.
- VM identity is the SMBIOS UUID, written by Proxmox at clone time and recorded in the `provisioning_runs` row before the VM is started. `/winpe/register` only succeeds when the SMBIOS UUID matches a row in `awaiting_winpe` state.
- WIM and unattend payloads cross the local SDN unencrypted (HTTP). MITM on the local SDN is out of scope; the threat model is the same as today's autounattend ISO injection. A future spec can move to mTLS.
- Tokens are returned only over HTTPS in production. Initial implementation is HTTP for parity with today's app deployment; switch is independent of this spec.

## 8. Failure modes

| Failure | Detection | Behavior |
|---|---|---|
| WinPE can't reach Flask | retry timeout in agent | abort phase 0; leave VM at WinPE prompt; on-screen error |
| Hash capture script fails | non-zero exit from `Get-WindowsAutopilotInfo` | record step error; if `hash_capture_phase = "winpe"` is mandatory, abort; if optional, continue and let OOBE FLC try |
| Disk too small for WIM | dism error | abort; clear log entry pointing at WIM size vs disk size |
| Driver injection partial | dism warnings | log warning, continue (NetKVM is the only one that strictly matters; Win11 has viostor/vioscsi in-box) |
| `/winpe/done` call fails | server 5xx or network | agent retries 3x then aborts and stays at prompt (do NOT reboot, because boot order still has WinPE first; reboot would loop) |
| Reboot after detach lands on WinPE again | agent re-registers, server sees state already past `awaiting_winpe` | server returns 409, agent halts and prints diagnostic |
| FLC phase fails after handoff | existing behavior, no change | unchanged |

There is no silent fallback path. A WinPE-mode run that fails stays failed; operator decides whether to retry or fall back to answerfile mode.

## 9. Testing

Unit:

- `sequence_compiler.compile_winpe` for each step kind: routing, parameter resolution, credential lookups.
- `unattend_renderer.render(..., phase_layout="specialize_only")` byte-exact fixture.
- Bearer token signing + verify.
- `/winpe/register` UUID match logic (correct + mismatched + state-wrong).

Integration:

- Flask test client exercises the full /winpe/* surface against an in-memory sqlite.
- Pester test for `Invoke-AutopilotWinPE.ps1` against a mock HTTP server.
- One end-to-end test on pve1 against a real cloned VM, asserting that a WinPE-mode provisioning run reaches FirstLogon and emits the expected hash file. This is the single integration test that gates the merge.

The existing answerfile-path test suite is unmodified.

## 10. Migration

- Existing sequences are unchanged. `hash_capture_phase` defaults to `oobe`. `provision_path` is per-run, defaulting to `answerfile`.
- Existing `vm_provisioning` table is read-only legacy; new code writes `provisioning_runs`. A back-compat shim populates `vm_provisioning` on `provisioning_runs` insert until any consumer is migrated.
- New step kinds (`partition_disk`, `apply_wim`, etc.) are compiler-injected, not operator-authored. Existing sequence editor does not surface them.

## 11. Known unknowns (deliberately listed, not blockers)

- Whether `Get-WindowsAutopilotInfo` runs cleanly against WMI in our exact WinPE bake. Microsoft documents WinPE-WMI and WinPE-Scripting as required; community reports note edge cases on TPM-related properties. Mitigation: the `hash_capture_phase = "oobe"` fallback flag exists from day one. First integration run will validate.
- Whether NetKVM auto-loads on boot inside WinPE without operator-side `drvload`. We will bake the driver into `boot.wim` via `dism /add-driver /forceunsigned` at build time and let WinPE's PnP do the rest. If that fails, `startnet.cmd` runs `drvload` explicitly before launching the agent.
- DHCP option 252 (Flask base URL discovery) is listed as a future nice-to-have. Initial implementation hardcodes the Flask host in `config.json`; it works because every deploy in scope is on the same SDN.
- Whether streaming the WIM from the locally-attached ISO matches `dism /apply-image` performance expectations. Fallback path (HTTP from Flask) exists in code but only used if attached-ISO read fails.

## 12. Out of scope (re-stated for the implementation plan)

Anything in section 2, plus:

- Refactoring the existing FLC pipeline.
- Changes to the existing Ansible answerfile playbooks.
- Changes to the existing UTM ARM64 path.
- Replacement of `vm_provisioning` table.
