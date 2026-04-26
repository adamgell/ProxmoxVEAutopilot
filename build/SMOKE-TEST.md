# Smoke test: end-to-end build pipeline

Run this once after build host setup to confirm the pipeline produces correct artifacts.

## Prerequisites

- Build host configured per `build/README.md`.
- `build/PrePEFlight-Check.ps1` passes on the build host.
- Repo cloned to `C:\BuildRoot\src\` on the build host.
- Dev Mac can `ssh ${BUILD_USER}@${BUILD_HOST} pwsh -Command 'Write-Host hi'` and see `hi`.

## 1. Build the PE WIM (small, ~10 min)

On the dev Mac:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
cp build/build-pe-wim.config.example.json build/build-pe-wim.config.json
# Edit build-pe-wim.config.json: set buildHost, buildHostUser, paths.
./tools/build-pe-wim.sh
```

Expected output ends with `DONE` and `registered pe-wim <sha>`.

### Verify

```bash
ls -la var/artifacts/staging/        # contains the WIM, ISO, sidecar, log
ls -la var/artifacts/store/          # contains <sha>.wim
sqlite3 var/artifacts/index.db 'SELECT sha256, kind, size FROM artifacts;'
# Should list one row with kind=pe-wim.
```

## 2. Build install.wim (large, ~30 min)

```bash
cp build/build-install-wim.config.example.json build/build-install-wim.config.json
# Edit. Set windowsIsoPath, virtioIsoPath to the real paths on the build host.
./tools/build-install-wim.sh
```

Expected: `DONE` and `registered install-wim <sha>` after ~30 min.

### Verify

```bash
sqlite3 var/artifacts/index.db 'SELECT sha256, kind, size FROM artifacts ORDER BY registered_at;'
# Two rows: pe-wim and install-wim.
```

## 3. Boot the PE ISO in UTM (smoke test of the placeholder Bootstrap.ps1)

1. Find the PE ISO at `var/artifacts/staging/winpe-autopilot-arm64-<sha>.iso`.
2. Create a new Win11 ARM64 VM in UTM with the PE ISO attached as CD/DVD.
3. Boot. Should boot through to PE without prompts.
4. PE should display the placeholder bootstrap output:

   ```
   ========================================
   Autopilot PE bootstrap (placeholder)
   ========================================
   PowerShell version : 7.4.x
   Edition            : Core
   OS                 : Microsoft Windows ...
   Architecture       : ARM64
   Hostname           : MININT-...
   SMBIOS UUID        : <uuid>
   ...
   Orchestrator URL   : http://autopilot.local:5000
   ```

5. Pwsh prompt is interactive. Type `exit` or `wpeutil reboot` to leave.

If you see this output, **the build pipeline is working end-to-end** and Plan 1 is complete.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `tools/build-pe-wim.sh` fails on rsync with "Permission denied" | `administrators_authorized_keys` ACLs wrong | Re-run icacls steps in `build/README.md` §4 |
| ssh hangs after key auth | OpenSSH default shell still cmd.exe | Re-set `HKLM:\SOFTWARE\OpenSSH\DefaultShell` and `Restart-Service sshd` |
| Build script fails at `Mount-DiskImage` | ISO path on build host wrong, or running unelevated | Check `windowsIsoPath` exists on build host; verify SSH session is elevated (`whoami /groups | findstr S-1-16-12288`) |
| Build script fails at `Add-WindowsPackage` with "specified package is not applicable" | Wrong arch — `WinPE_OCs\<pkg>.cab` from amd64 path used for arm64 build | Double-check `architecture` in the build config |
| PE boots but prints nothing visible | `unattend.xml` not picked up | Check the WIM's `\unattend.xml` exists with the `RunSynchronousCommand`; verify `winpeshl.ini` in `\Windows\System32` |
| PE "Press any key to boot from CD/DVD" prompt appears | ISO built without `efisys_noprompt.bin` | Re-check the oscdimg arguments in `Build-PeWim.ps1` Phase 11 |

---

## Plan 3 — real bootstrap interpreter end-to-end

After Plan 3 lands, the placeholder Bootstrap.ps1 is replaced with the real
manifest interpreter. This runbook validates it deploys a real-ish manifest
end-to-end.

### Prerequisites

- Plan 1 + Plan 2 + Plan 3 branches all merged or stacked-and-checked-out.
- Build host configured per `build/README.md`.
- A registered `install.wim` artifact (sha known) — from a prior `tools/build-install-wim.sh` run.
- Orchestrator running locally on the dev Mac at `http://<mac-hostname>.local:5000` (or wherever
  `build-pe-wim.config.json` points).

### 1. Register a winpe_target via the orchestrator API

From the orchestrator host:

```python
from web.winpe_targets_db import WinpeTargetsDb
from pathlib import Path

db = WinpeTargetsDb(Path("var/artifacts/index.db"))
db.register(
    vm_uuid="REAL-VM-UUID-FROM-UTM",
    install_wim_sha="<sha of registered install.wim>",
    template_id="win11-arm64-baseline",
    params={"computer_name": "AUTOPILOT-SMOKE-01"},
)
```

(The vm_uuid you pass must match the SMBIOS UUID UTM assigns to the VM you'll boot.
Use UTM's "Edit → Information → Hardware UUID" to find it, OR boot once with the
old PE and read it from `wmic csproduct get UUID`.)

### 2. Build the new PE WIM (with the real Bootstrap.ps1)

Same as before:

```bash
./tools/build-pe-wim.sh
```

The new artifact ships the real Bootstrap.ps1 + module tree under `X:\autopilot\Modules\`.

### 3. Boot in UTM

Attach the new ISO to a Win11 ARM64 VM whose SMBIOS UUID matches the one you registered
in step 1. Boot.

### 4. Expected behavior

Within ~30s of boot, you should see in the cmd.exe console:

```
========================================
Autopilot PE bootstrap
========================================
Orchestrator URL: http://your-mac.local:5000
Wait-PeNetwork...
Got IP: 192.168.x.y
Initialize-SshHostKeys...
Initialize-SshHostKeys: regenerated keys in X:\ProgramData\ssh; sshd Running
Identity: REAL-VM-UUID-FROM-UTM (vendor=... name=...)
Fetching manifest...
Manifest: 6 steps, onError=halt
LogStep: ...   (or PartitionStep, ApplyWimStep, etc., as the real manifest dictates)
...
Bootstrap complete.
```

The orchestrator-side `winpe_checkins` table will accumulate one row per step:

```bash
cd autopilot-proxmox
sqlite3 ../var/artifacts/checkins.db \
    "SELECT step_id, status, duration_sec FROM winpe_checkins WHERE vm_uuid='REAL-VM-UUID-FROM-UTM' ORDER BY timestamp;"
```

### 5. SSH into the running PE

After `Initialize-SshHostKeys` reports `sshd Running`:

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new Administrator@<pe-vm-ip>
```

Should land in cmd.exe; `pwsh` to switch. `type X:\Windows\Temp\autopilot-pe.log` shows the
full transcript.

### Plan 3 exit criterion

A real manifest deploys end-to-end on a UTM VM, all per-step checkins land in `winpe_checkins`,
the resulting target volume boots into Windows, FirstLogonCommand executes the staged
harness — connecting to the existing hash-capture flow.
