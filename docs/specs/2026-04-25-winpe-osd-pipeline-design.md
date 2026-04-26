# WinPE-driven OS deployment pipeline

**Date:** 2026-04-25
**Status:** approved; ready for implementation planning
**Scope (v1):** UTM + Windows 11 ARM64 on Apple Silicon
**Inspiration:** 2Pint Software DeployR (architectural patterns only; zero shipped 2Pint code)

## Motivation

Today the project provisions Windows VMs by booting Microsoft's stock install ISO with an `autounattend.xml` answer file plus an `$OEM$\$1\` payload baked into the ISO. The flow works, but every per-VM customization has to be expressed as either a Setup answer-file knob or a `FirstLogonCommand` script that runs after the OS is fully installed. Two consequences fall out:

1. **The install media gets re-customized per use case.** `remaster_win11_noprompt.sh` byte-patches the ISO to skip the BootMgr CD prompt; future per-template knobs (driver overrides, SMBIOS-conditional config, Audit-mode hooks) keep accreting in the answer-ISO build path. There's no clean separation between "the OS we install" and "the per-VM tasks we run."
2. **Setup is a black box.** Once `setup.exe` starts, we have no orchestrator visibility until first login. Failures during specialize or component install surface as a hung VM that the Jobs page can't diagnose.

DeployR's WinPE-first model addresses both: a custom WinPE artifact boots, identifies itself, fetches a per-VM task manifest over HTTP, applies a baseline WIM directly, stages files and offline-edits the target hives, then reboots into a Windows that's already wired up. Setup never runs; the orchestrator drives everything; the install media is fixed and per-VM customization is pure data.

This spec adopts that model.

## Goals (v1)

- Replace the `setup.exe`-driven path with a **WinPE → Apply-WindowsImage → reboot** path for VMs that opt in.
- Express per-VM deployment as a **declarative task manifest** served over HTTP from the existing Flask orchestrator.
- Keep the existing direct-ISO flow working as a legacy/fallback option (additive, not replacing).
- Land the build pipeline on a Windows VM (Proxmox-hosted) reachable over SSH, with reproducible PowerShell-driven WIM construction.

## Non-goals (v1)

- Proxmox VE boot delivery for the new path. UTM only in v1; PVE is phase 2.
- mDNS or DHCP-based orchestrator discovery. Hardcoded URL only in v1; mDNS in v1.5; DHCP option (252 or 60+43) later.
- Auth between PE and orchestrator. **LAN-trusted, plain HTTP**. Threat model is "single dev Mac running everything." HMAC plumbing is sketched as a v1.5 step.
- A captured/sysprepped golden image. v1's `install.wim` is the stock Microsoft WIM with virtio-win drivers DISM-injected. Pre-staged harness inside the WIM is v2; sysprepped golden image is v3+.
- "Rebuild" buttons in the web UI. Build invocation is CLI from the dev Mac in v1.
- x64 builds. ARM64 only in v1 (the only target UTM/Apple-Silicon supports). x64 follows the same pipeline.
- Replacing or refactoring the direct-ISO flow. It coexists.

## Architecture

```
                    ┌────────────────────────────────────────────────────────┐
                    │  Build host (Windows VM on Proxmox, Dev Drive ReFS)    │
                    │   Build-InstallWim.ps1   → install-<arch>-<sha>.wim    │
                    │   Build-PeWim.ps1        → winpe-autopilot-<arch>.wim  │
                    └─────────────────────────┬──────────────────────────────┘
                                              │ ssh (key auth, pwsh-default-shell, Administrator)
                                              │ scp (artifacts back)
                    ┌─────────────────────────▼──────────────────────────────┐
                    │  Orchestrator (existing Flask app, dev Mac for v1)     │
                    │   GET  /winpe/manifest/<smbios-uuid>                   │
                    │   GET  /winpe/content/<sha256>                         │
                    │   POST /winpe/checkin                                  │
                    │   var/artifacts/store/<sha256>.wim   (content-addr)    │
                    │   var/artifacts/index.db             (sqlite)          │
                    └─────────────────────────┬──────────────────────────────┘
                                              │ HTTP, no auth (LAN-trusted)
                    ┌─────────────────────────▼──────────────────────────────┐
                    │  Target VM (UTM in v1)                                 │
                    │   PE WIM ISO → wpeinit → pwsh Bootstrap.ps1            │
                    │   1. SMBIOS UUID identify                              │
                    │   2. GET manifest                                      │
                    │   3. execute steps + POST checkin per step             │
                    │   4. final reboot → installed Windows                  │
                    └────────────────────────────────────────────────────────┘
```

Three components, three repos-worth of changes (all in this monorepo):

| Component | Lives in | New code |
|---|---|---|
| Build pipeline | `build/` (new) | `Build-InstallWim.ps1`, `Build-PeWim.ps1`, `pe-payload/` |
| Orchestrator API | `autopilot-proxmox/web/` | `winpe_manifest.py`, `winpe_content.py`, `winpe_checkin.py`, `artifact_store.py` |
| PE runtime | `build/pe-payload/` | `Bootstrap.ps1`, `Modules/Autopilot.PETransport/`, `Modules/Autopilot.PESteps/` |

The orchestrator reuses existing pieces: `unattend_renderer.py` (renders per-VM `unattend.xml` for the `write-unattend` step), `sequence_compiler.py` and `sequences_db.py` (a "sequence" becomes a manifest the new endpoint serves), `oem_profile_resolver` (decides which OEM identity → which install.wim profile).

## Build pipeline

### Build host setup

A Windows 11 VM on Proxmox VE, named here as `buildhost`. One-time setup:

- **OpenSSH Server** (Windows feature, not the third-party builds): `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`. Service `sshd` set to Automatic.
- **Default shell = pwsh 7**: `New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value 'C:\Program Files\PowerShell\7\pwsh.exe' -PropertyType String -Force`. Without this, SSH lands in cmd.exe and PowerShell quoting becomes painful.
- **Key auth only**, password auth disabled in `C:\ProgramData\ssh\sshd_config`. Public key in **both** `~\.ssh\authorized_keys` (for the user) **and** `C:\ProgramData\ssh\administrators_authorized_keys` (Windows quirk: Administrators-group keys live there, not in the user's home).
- **Account = local Administrator** (or member of Administrators). DISM, `Mount-DiskImage`, `Add-WindowsDriver` need elevation; non-interactive SSH login from a member of Administrators is already elevated (no UAC dance).
- **Dev Drive (ReFS)** for `C:\BuildRoot\work\` (mount points, scratch). Defender uses performance mode for trusted Dev Drives, which makes mounted-WIM operations 5-10x faster. Verify trusted with `fsutil devdrv query <drive>`.
- **Windows ADK** + **WinPE add-on** installed from the default location.
- **Firewall**: open 22 on the management interface only.

### Directory layout on the build host

```
C:\BuildRoot\
├── src\                        # this repo, git-pulled
│   ├── build\
│   │   ├── Build-InstallWim.ps1
│   │   ├── Build-PeWim.ps1
│   │   └── pe-payload\         # rsynced per PE build (Bootstrap.ps1, modules, certs, Bootstrap.json)
├── inputs\                     # parked, not pushed per-build
│   ├── windows\Win11_*.iso
│   └── virtio\virtio-win-*.iso
├── outputs\                    # WIMs land here
│   ├── install-win11-arm64-<sha>.wim
│   ├── install-win11-arm64-<sha>.json     # sidecar metadata
│   ├── install-win11-arm64-<sha>.log
│   ├── winpe-autopilot-arm64-<sha>.wim
│   ├── winpe-autopilot-arm64-<sha>.json
│   └── winpe-autopilot-arm64-<sha>.log
└── work\                       # on Dev Drive; mount points + scratch
```

Source ISOs are staged once per release; never pushed over SSH per-build. Bumped manually when MS or Red Hat releases.

### `Build-InstallWim.ps1` — what it does

Inputs (a build-config JSON, passed by the caller):

```json
{
  "windowsIsoPath": "C:\\BuildRoot\\inputs\\windows\\Win11_24H2_ARM64.iso",
  "virtioIsoPath":  "C:\\BuildRoot\\inputs\\virtio\\virtio-win-0.1.266.iso",
  "edition":        "Windows 11 Enterprise",
  "architecture":   "arm64",
  "drivers":        ["vioserial", "viostor", "vioscsi", "NetKVM", "balloon", "vioinput"],
  "outputDir":      "C:\\BuildRoot\\outputs"
}
```

Steps (all PowerShell):

1. Acquire build lock at `C:\BuildRoot\work\.build.lock` (refuse if held — no parallel builds on one host).
2. Verify `outputDir` is on Dev Drive **OR** in Defender exclusions; warn if neither.
3. `Mount-DiskImage` the Windows ISO. Find the drive letter assigned.
4. `Mount-DiskImage` the virtio-win ISO. Find its drive letter.
5. Copy `<win-iso>:\sources\install.wim` to `C:\BuildRoot\work\install-staging.wim`.
6. Inspect with `Get-WindowsImage`. If multiple editions, pick `edition` from input. Capture index.
7. `Mount-WindowsImage -Path C:\BuildRoot\work\mount-install -ImagePath ... -Index <n>`.
8. For each driver in `drivers`: locate `<virtio>:\<driver>\w11\<arch>\` and `Add-WindowsDriver -Path ... -Driver ... -Recurse`.
9. `Dismount-WindowsImage -Save`.
10. `Export-WindowsImage` to `outputDir\install-<edition-slug>-<arch>-staging.wim` (this compacts the WIM; the unexported version is significantly larger).
11. Compute SHA256 of the exported WIM; rename to `install-<edition-slug>-<arch>-<sha>.wim`.
12. Write sidecar `install-...-<sha>.json`:
    ```json
    {
      "kind": "install-wim",
      "sha256": "<hex>",
      "size": 4500000000,
      "edition": "Windows 11 Enterprise",
      "architecture": "arm64",
      "sourceWindowsIso": {"path": "...", "sha256": "<hex>", "buildNumber": "26100.x"},
      "sourceVirtioIso":  {"path": "...", "sha256": "<hex>", "version": "0.1.266"},
      "driversInjected":  ["vioserial", "viostor", "vioscsi", "NetKVM", "balloon", "vioinput"],
      "buildHost": "buildhost",
      "buildTimestamp": "2026-04-25T...Z",
      "builderScript": "Build-InstallWim.ps1@<git-sha>"
    }
    ```
13. Write the cmtrace-format build log next to the artifact.
14. Dismount both ISOs; clean `work\mount-install`.
15. Release lock.

### `Build-PeWim.ps1` — what it does

Inputs:

```json
{
  "adkRoot":         "C:\\Program Files (x86)\\Windows Kits\\10",
  "architecture":    "arm64",
  "virtioIsoPath":   "C:\\BuildRoot\\inputs\\virtio\\virtio-win-0.1.266.iso",
  "drivers":         ["vioserial", "viostor", "vioscsi", "NetKVM", "balloon", "vioinput"],
  "pwsh7Zip":        "C:\\BuildRoot\\inputs\\runtime\\PowerShell-7.4.x-win-arm64.zip",
  "dotnetRuntimeZip":"C:\\BuildRoot\\inputs\\runtime\\dotnet-runtime-8.0.x-win-arm64.zip",
  "payloadDir":      "C:\\BuildRoot\\src\\build\\pe-payload",
  "orchestratorUrl": "http://autopilot.local:5000",
  "outputDir":       "C:\\BuildRoot\\outputs"
}
```

Steps:

1. Acquire build lock.
2. Copy ADK's `winpe.wim` (from `<adk>\Assessment and Deployment Kit\Windows Preinstallation Environment\<arch>\en-us\winpe.wim`) to `work\winpe-staging.wim`.
3. `Mount-WindowsImage` to `work\mount-pe`.
4. Add ADK packages from `<adk>\...\<arch>\WinPE_OCs\`:
   - `WinPE-WMI`
   - `WinPE-NetFX`
   - `WinPE-PowerShell`
   - `WinPE-StorageWMI`
   - `WinPE-EnhancedStorage`
   - `WinPE-DismCmdlets`
   - `WinPE-SecureStartup`
   - `WinPE-SecureBootCmdlets`
   Use `Add-WindowsPackage -PackagePath <path>.cab` for each; localized companions (`en-us\<pkg>_en-us.cab`) added immediately after.
5. Remove unused packages (size optimization), via `Remove-WindowsPackage`:
   - `Microsoft-Windows-WinPE-Speech-TTS-Package`
   - `Microsoft-Windows-WinPE-ATBroker-Package`
   - `Microsoft-Windows-WinPE-Narrator-Package`
   - `Microsoft-Windows-WinPE-AudioDrivers-Package`
   - `Microsoft-Windows-WinPE-AudioCore-Package`
   - `Microsoft-Windows-WinPE-SRH-Package`
   - `WinPE-HTA`
   - `WinPE-Scripting`
   - `WinPE-WDS-Tools`
6. Inject virtio drivers (`vioserial`, `viostor`, `vioscsi`, `NetKVM`, `balloon`, `vioinput`) via `Add-WindowsDriver -Path work\mount-pe -Driver <virtio>:\<driver>\w11\<arch> -Recurse -ForceUnsigned`.
7. Drop in **.NET 8 Runtime** (`Microsoft.NETCore.App`, extracted from zip) at `work\mount-pe\Program Files\dotnet\`. Just the base runtime — pwsh 7 itself does not require the Desktop Runtime (WPF/WinForms). If a future PE UI ever lands, swap to `windowsdesktop-runtime` then; until then, the smaller download is correct.
8. Drop in **PowerShell 7** (extracted from zip) at `work\mount-pe\Program Files\PowerShell\7\`.
9. **Nuke PS 5.1 binaries** (DeployR pattern):
   ```powershell
   takeown /f "$peMount\Windows\System32\WindowsPowerShell\v1.0\*.*"
   icacls "$peMount\Windows\System32\WindowsPowerShell\v1.0\*.*" /grant everyone:f
   Get-ChildItem "$peMount\Windows\System32\WindowsPowerShell\v1.0\*.*" -File | Remove-Item -Force
   ```
   `-File` and non-recursive `*.*` removes `powershell.exe` + the few binaries in the v1.0 root, but **keeps `v1.0\Modules\` intact**. pwsh 7 still loads `Storage`, `Dism`, `Microsoft.PowerShell.Management`, etc. from there because `PSModulePath` includes the legacy path.
10. Offline registry edits, via `reg.exe load HKLM\PESystem ...\config\SYSTEM` + `reg.exe load HKLM\PESoftware ...\config\SOFTWARE`:
    - `HKLM\PESoftware\dotnet\Setup\InstalledVersions\<arch>\sharedhost\Path = X:\Program Files\dotnet\`
    - `HKLM\PESoftware\dotnet\Setup\InstalledVersions\<arch>\sharedhost\Version = <discovered>`
    - `HKLM\PESystem\ControlSet001\Control\Session Manager\Environment\Path` += `;X:\Program Files\dotnet\;X:\Program Files\PowerShell\7`
    - `HKLM\PESystem\...\Environment\PSModulePath` += `;%ProgramFiles%\PowerShell\;%ProgramFiles%\PowerShell\7\;%SystemRoot%\system32\config\systemprofile\Documents\PowerShell\Modules\`
    - `HKLM\PESystem\...\Environment\APPDATA = %SystemRoot%\System32\Config\SystemProfile\AppData\Roaming` (and HOMEDRIVE, HOMEPATH, LOCALAPPDATA — needed so PS module loading and `Invoke-RestMethod` cookie storage don't crash on the missing-profile path)
    - `HKLM\PESystem\...\Environment\POWERSHELL_UPDATECHECK = LTS`
    - `HKLM\PESystem\ControlSet001\Services\Tcpip\Parameters\TcpTimedWaitDelay = 30`
    - `HKLM\PESystem\...\Tcpip\Parameters\MaxUserPort = 65534`
    - Unload both hives.
11. Stage payload at `work\mount-pe\autopilot\` (the `X:\autopilot\` runtime root):
    ```
    \autopilot\
    ├── Bootstrap.ps1
    ├── Bootstrap.json   # rendered from template with orchestratorUrl baked in
    ├── Modules\
    │   ├── Autopilot.PETransport\
    │   └── Autopilot.PESteps\
    ├── certs\           # empty in v1
    └── tools\
        └── cmtrace.exe  # optional; copied from build host C:\Windows\System32\
    ```
12. Stage `winpeshl.ini` at `work\mount-pe\Windows\System32\winpeshl.ini`:
    ```ini
    [LaunchApps]
    %SYSTEMROOT%\System32\wpeinit.exe
    ```
13. Stage `unattend.xml` at `work\mount-pe\unattend.xml`:
    ```xml
    <?xml version="1.0" encoding="utf-8"?>
    <unattend xmlns="urn:schemas-microsoft-com:unattend">
      <settings pass="windowsPE">
        <component name="Microsoft-Windows-Setup" processorArchitecture="arm64"
                   publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
                   xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
          <EnableNetwork>false</EnableNetwork>
          <RunSynchronous>
            <RunSynchronousCommand wcm:action="add">
              <Description>Autopilot PE Bootstrap</Description>
              <Order>1</Order>
              <Path>"X:\Program Files\PowerShell\7\pwsh.exe" -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File X:\autopilot\Bootstrap.ps1</Path>
            </RunSynchronousCommand>
          </RunSynchronous>
        </component>
      </settings>
    </unattend>
    ```
    `EnableNetwork=false` because Bootstrap.ps1 brings up its own networking before the first HTTP call (lets us control timing and retry on flaky DHCP).
14. Replace `winpe.jpg` background (optional; brand asset).
15. `Dismount-WindowsImage -Save`.
16. `Export-WindowsImage` to compact.
17. Hash, rename, write sidecar JSON, write log, release lock.
18. Generate ISO via `oscdimg`:
    ```
    oscdimg -m -o -u2 -udfver102 -bootdata:1#pEF,e,bEfisys_noprompt.bin "<media>" "winpe-autopilot-arm64-<sha>.iso"
    ```
    `efisys_noprompt.bin` = no "Press any key" at firmware level (matches your existing `cdboot_noprompt.efi` outcome via the DeployR pattern). ARM64 is UEFI-only so `-bootdata:1#pEF...` is sufficient (no BIOS El Torito needed).

### Dev-Mac side: build invocation

Two scripts in the repo, e.g. `tools/build-install-wim.sh` and `tools/build-pe-wim.sh`. Each:

1. Validate local config (`build/build-install-wim.config.json` or `build/build-pe-wim.config.json`).
2. For PE only: `rsync -av --delete build/pe-payload/ buildhost:C:/BuildRoot/src/build/pe-payload/`.
3. `ssh buildhost pwsh -File C:/BuildRoot/src/build/Build-{Install,Pe}Wim.ps1 -Config -` and pipe the JSON config on stdin (avoids quoting hell).
4. Stream stdout/stderr to local terminal. Non-zero exit → fail loudly.
5. `scp buildhost:C:/BuildRoot/outputs/<latest>.{wim,iso,json,log} ./var/artifacts/staging/`.
6. `python -m autopilot register-artifact --path ./var/artifacts/staging/<file> --sidecar ./var/artifacts/staging/<sidecar.json>` — the bridge into the orchestrator: validates the sidecar, hashes the WIM, copies into `var/artifacts/store/<sha256>.wim`, upserts metadata into `var/artifacts/index.db`. After this, `/winpe/content/<sha>` can serve it.

No orchestrator UI surface in v1. Build is something you do deliberately from your Mac.

## PE runtime

### Bootstrap flow

```
Power-on
  └─ Firmware boots winpe-autopilot-arm64-<sha>.iso (efisys_noprompt → no prompt)
  └─ boot.wim loads
  └─ winpeshl.ini runs:
       wpeinit.exe                          # PnP, network stack, registers \unattend.xml
  └─ Setup processes \unattend.xml (windowsPE pass):
       RunSynchronousCommand → pwsh Bootstrap.ps1
  └─ Bootstrap.ps1:
       1. Start-Transcript X:\Windows\Temp\autopilot-pe.log
       2. Read X:\autopilot\Bootstrap.json   (orchestratorUrl, pollIntervalSec, debug)
       3. Wait for network: poll Get-NetIPAddress for IPv4 (timeout 60s)
       4. Resolve identity: $uuid = (Get-CimInstance Win32_ComputerSystemProduct).UUID
       5. GET <orchestratorUrl>/winpe/manifest/<uuid>   → manifest.json
       6. for $step in $manifest.steps:
            POST checkin (status=starting, step.id)
            try {
                Invoke-Step $step                  # dispatches by $step.type
                POST checkin (status=ok, step.id)
            } catch {
                POST checkin (status=error, step.id, error_message)
                if ($manifest.onError -eq 'halt') { drop to debug shell; exit }
                if ($manifest.onError -eq 'continue') { continue }
            }
       7. Final step is reboot/shutdown; PE goes down.
```

### `Bootstrap.json` schema

Baked into the WIM at build time. One file per orchestrator deployment.

```json
{
  "version": 1,
  "orchestratorUrl": "http://autopilot.local:5000",
  "networkTimeoutSec": 60,
  "manifestRetries": 3,
  "manifestRetryBackoffSec": 5,
  "checkinRetries": 2,
  "debug": false
}
```

### Step types (v1)

Ten primitives. Each lives in `Modules/Autopilot.PESteps/` as `Invoke-<Type>Step`.

| Type | Inputs | Effect |
|---|---|---|
| `partition` | `layout: "uefi-standard"` | Clear disk 0; create GPT with EFI (260MB FAT32) + MSR (16MB) + Windows (rest, NTFS). Returns `{esp: "S:", windows: "W:"}` (drive letters assigned dynamically; emitted to checkin). |
| `apply-wim` | `content: {sha256, size}`, optional `index: <int>`, optional `target: "W:"` | Stream `/winpe/content/<sha>` to disk; validate sha; `Expand-WindowsImage` to target volume. Validates expanded size against sidecar's `expandedSizeBytes`. |
| `stage-files` | `content: {sha256, size}` (zip blob), `target: "W:\Program Files\Autopilot"` | Fetch zip; verify sha; `Expand-Archive` into target path on the offline volume. |
| `set-registry` | `hive: "SYSTEM"\|"SOFTWARE"\|"DEFAULT"`, `target: "W:"`, `keys: [{path, name, type, value}]` | `reg load HKLM\PEStaging <volume>\Windows\System32\config\<hive>` → set keys → `reg unload`. `DEFAULT` hive at `<volume>\Users\Default\NTUSER.DAT`. |
| `write-unattend` | `content: {sha256, size}`, `target: "W:\Windows\Panther\unattend.xml"` | Fetch rendered unattend.xml from orchestrator; write to Panther path. Sysprep specialize/oobeSystem passes pick it up at next boot. |
| `schedule-task` | `target: "W:"`, `taskXml: <inline>`, `name: "..."` | Write task XML to `<volume>\Windows\System32\Tasks\<name>` AND inject the matching TaskCache registry entries on the offline SOFTWARE hive (more reliable than `schtasks /create` against an offline image, which works inconsistently). |
| `bcdboot` | `windows: "W:"`, `esp: "S:"` | Run `bcdboot W:\Windows /s S: /f UEFI`. Makes target volume bootable. |
| `inject-driver` | `target: "W:"`, `content: {sha256, size}` (zip of inf+sys) | Fetch driver bundle; `Add-WindowsDriver -Path <target>\Windows -Driver <extracted-path> -Recurse -ForceUnsigned`. Mostly unused in v1 since drivers are baked into install.wim; escape hatch for per-VM driver overrides. |
| `reboot` / `shutdown` | (none) | `wpeutil reboot` / `wpeutil shutdown`. Terminal step. |
| `log` | `message: "..."` | Adds a marker line to local transcript and the next checkin's `log_tail`. Debugging aid. |

All steps have implicit common fields: `id` (string, must be unique within the manifest) and optional `description` (free-form, surfaced in checkin payloads).

### Manifest schema

```json
{
  "version": 1,
  "vmUuid": "<smbios-uuid>",
  "generatedAt": "2026-04-25T...Z",
  "onError": "halt",
  "steps": [
    {"id": "partition", "type": "partition", "layout": "uefi-standard"},
    {"id": "apply",  "type": "apply-wim",
     "content": {"sha256": "<hex>", "size": 4500000000}},
    {"id": "stage", "type": "stage-files",
     "content": {"sha256": "<hex>", "size": 12345},
     "target": "W:\\Program Files\\Autopilot"},
    {"id": "panther", "type": "write-unattend",
     "content": {"sha256": "<hex>", "size": 3456}},
    {"id": "regname", "type": "set-registry",
     "hive": "SYSTEM", "target": "W:",
     "keys": [
       {"path": "Setup", "name": "ComputerName",
        "type": "REG_SZ", "value": "AUTOPILOT-X1"}
     ]},
    {"id": "regrun", "type": "set-registry",
     "hive": "SOFTWARE", "target": "W:",
     "keys": [
       {"path": "Microsoft\\Windows\\CurrentVersion\\RunOnce",
        "name": "AutopilotKick", "type": "REG_SZ",
        "value": "powershell -File \"C:\\Program Files\\Autopilot\\bootstrap.ps1\" -Method FirstBoot"}
     ]},
    {"id": "boot",   "type": "bcdboot", "windows": "W:", "esp": "S:"},
    {"id": "rb",     "type": "reboot"}
  ]
}
```

`onError` is `"halt"` (drop to debug shell) or `"continue"` (log, post error checkin, continue).

**Debug shell** = `Start-Process pwsh -NoNewWindow -Wait -ArgumentList '-NoExit', '-File', 'X:\autopilot\Debug.ps1'` (a sibling script that prints diagnostic info — UUID, orchestratorUrl, last manifest, last checkin error — and leaves an interactive prompt on the VM console). Mirrors DeployR's `Debug.ps1` pattern. Unused unless `onError=halt` fires.

### Checkin payload

```json
{
  "vmUuid": "<smbios-uuid>",
  "stepId": "apply",
  "status": "ok",
  "timestamp": "2026-04-25T...Z",
  "durationSec": 84.2,
  "logTail": "<last ~2KB of the local transcript>",
  "errorMessage": null,
  "extra": {
    "esp": "S:",
    "windows": "W:"
  }
}
```

Fire-and-forget; the orchestrator persists into `device_history_db.py` for the Jobs page to render.

## Orchestrator API

Three new routes in the existing Flask app, all under `/winpe/`.

### `GET /winpe/manifest/<smbios-uuid>`

Looks up the VM by SMBIOS UUID in `sequences_db` (or a new `winpe_targets` table linking UUID → install.wim sha + payload zip sha + unattend params + step list). Renders the manifest:

- Resolves which `install.wim` (by sha256) — keyed off the OEM profile / template.
- Renders the per-VM `unattend.xml` via existing `unattend_renderer.py` (computer name, FirstLogonCommands pointing at the staged harness, OEM identity validation, autologon for first-boot script, etc.). Hashes the rendered output, stores in a content cache, references by sha.
- Bundles the per-VM `stage-files` zip (harness scripts, certs, JSON config) into a content blob; hashes; caches.
- Returns the assembled manifest JSON.

Content-addressed caching: identical (template, params) tuples produce identical sha256 → cache hit. Cache lives in `var/artifacts/cache/<sha>` with LRU eviction.

404 if the UUID is unknown. 503 if the orchestrator isn't fully initialized (e.g., install.wim not yet registered).

### `GET /winpe/content/<sha256>`

Serves a content-addressed blob from the artifact store. Two storage tiers:

1. `var/artifacts/store/<sha256>.{wim,zip,xml}` — registered build artifacts (install.wim, pe.wim) + any permanent content. Populated by `register-artifact`.
2. `var/artifacts/cache/<sha256>.{zip,xml}` — orchestrator-rendered per-VM blobs (stage-files zips, unattend.xml). Populated lazily on manifest render. LRU-evicted.

`HEAD` supported (PE uses it to decide cache vs fetch). Range requests not needed in v1 (LAN, single-shot). 404 if sha not found.

Content-Type:
- `.wim` → `application/octet-stream`
- `.zip` → `application/zip`
- `.xml` → `application/xml`

### `POST /winpe/checkin`

Accepts the checkin payload; persists in `device_history_db`. Surfaces in the Jobs page as per-step progress for the deployment job.

No locking; checkins are idempotent on `(vmUuid, stepId, timestamp)`.

## Artifact storage

```
var/artifacts/
├── store/                       # registered build artifacts (permanent)
│   └── <sha256>.wim             # install.wim + pe.wim
├── cache/                       # orchestrator-rendered per-VM blobs (LRU)
│   └── <sha256>.{zip,xml}
├── staging/                     # scp landing zone before register-artifact
└── index.db                     # sqlite
```

`index.db` schema (one table per kind, plus a unified view):

```sql
CREATE TABLE artifacts (
    sha256          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,  -- install-wim / pe-wim / stage-zip / unattend-xml / driver-zip
    size            INTEGER NOT NULL,
    relative_path   TEXT NOT NULL,  -- store/<sha>.wim or cache/<sha>.zip
    metadata_json   TEXT NOT NULL,  -- the sidecar JSON
    registered_at   TEXT NOT NULL,
    last_served_at  TEXT
);

CREATE TABLE winpe_targets (
    vm_uuid          TEXT PRIMARY KEY,        -- SMBIOS UUID
    install_wim_sha  TEXT NOT NULL REFERENCES artifacts(sha256),
    template_id      TEXT NOT NULL,           -- existing sequence template
    params_json      TEXT NOT NULL,           -- per-VM render inputs (computer name, OEM, etc.)
    created_at       TEXT NOT NULL,
    last_manifest_at TEXT
);
```

`register-artifact` CLI inserts into `artifacts`. The clone provisioning flow (existing `proxmox_vm_clone` / `utm_vm_clone` roles) inserts into `winpe_targets` when a VM is cloned that should boot via PE.

## Failure modes

| Failure | Behavior |
|---|---|
| Network not up after `networkTimeoutSec` | Retry once, then drop to debug shell with diagnostic message. Orchestrator never sees it; operator opens the VM console. |
| `manifest` 404 | Orchestrator doesn't know this UUID. Drop to debug shell with the UUID and orchestratorUrl printed. |
| `content` 404 mid-deploy | Step fails. Behavior follows `onError`. With `"halt"` (default), debug shell + last-known checkin posted. |
| sha256 mismatch on fetched content | Step fails immediately, doesn't apply corrupt content. Debug shell. |
| `Apply-WindowsImage` fails (corrupt WIM, target volume too small, IO error) | Debug shell. Manual recovery from there. |
| `bcdboot` fails | Step fails. Manifest stops. The target volume has the OS but no boot entry — operator can recover by booting back into PE, manually running bcdboot, or letting the orchestrator re-issue the manifest with only the `bcdboot` + `reboot` steps. |
| Build host SSH unreachable | `tools/build-*.sh` fails on step 3. Local-side CLI surfaces the SSH error directly. No orchestrator state changes. |
| install.wim file deleted from `var/artifacts/store/` | `/winpe/content/<sha>` returns 404. Detected at deploy time. Re-run `register-artifact` to repopulate. |
| Orchestrator restarts mid-deploy | PE keeps going (it has the whole manifest). Checkins fail with connection errors but PE doesn't halt unless `onError=halt` and a step itself failed. When orchestrator returns, gap in Jobs-page progress is visible but deploy completes. |

## Non-functional notes

- **Concurrency**: build host accepts one build at a time (file lock). Orchestrator can serve content concurrently — Flask + sendfile, no in-process locks. Per-VM manifests are independent.
- **Throughput**: `apply-wim` over LAN is the long pole (~30-90s for a 4GB WIM at gigabit). Single-VM deploy time target: <5 min from PE boot to "installed Windows reboots." Stretch target: <3 min.
- **PE WIM size budget**: ~400 MB exported. Beyond that, ISO size becomes annoying for UTM attach + USB delivery.
- **install.wim freshness**: rebuild quarterly when MS releases a new feature update, or when virtio-win bumps a major version. Trigger is manual.

## Implementation phases

The work splits into four roughly-independent tracks; tracks 1 and 2 can run in parallel.

### Phase 1 — build pipeline + artifact store

- [ ] Stand up Windows 11 build VM on Proxmox; install ADK + WinPE add-on; configure OpenSSH + Dev Drive.
- [ ] `Build-InstallWim.ps1` (drivers integrated, no harness).
- [ ] `Build-PeWim.ps1` (with payload tree + `Bootstrap.json` baked).
- [ ] `tools/build-install-wim.sh` and `tools/build-pe-wim.sh` (dev-Mac wrappers).
- [ ] `var/artifacts/` layout + `index.db` schema.
- [ ] `python -m autopilot register-artifact` CLI.
- **Exit criteria**: from the dev Mac, `tools/build-pe-wim.sh` produces a registered PE WIM artifact. `tools/build-install-wim.sh` produces a registered install.wim. Both reproducible.

### Phase 2 — orchestrator API

- [ ] `web/winpe_manifest.py` (route + manifest renderer).
- [ ] `web/winpe_content.py` (route + content-addressed serving + cache).
- [ ] `web/winpe_checkin.py` (route + persistence into `device_history_db`).
- [ ] `web/artifact_store.py` (store + cache + index.db helpers).
- [ ] `winpe_targets` table + clone-flow integration (set up the row when a PE-flow VM is cloned).
- [ ] Manifest rendering reuses `unattend_renderer.py`.
- **Exit criteria**: `curl <orch>/winpe/manifest/<known-uuid>` returns a valid manifest. `curl <orch>/winpe/content/<known-sha>` streams the artifact. Checkins persist and show in the Jobs page.

### Phase 3 — PE runtime

- [ ] `Bootstrap.ps1` (identify, network wait, fetch manifest, dispatch).
- [ ] `Modules/Autopilot.PETransport/` (Invoke-Manifest, Get-Content, Post-Checkin; retries; sha verify).
- [ ] `Modules/Autopilot.PESteps/` (one cmdlet per step type).
- [ ] Local transcript + cmtrace log.
- [ ] Debug shell on `onError=halt`.
- **Exit criteria**: a manually-crafted manifest deploys a Win11 ARM64 VM end-to-end on UTM. Resulting OS boots, runs FirstLogonCommand from staged unattend, surfaces in the existing hash-capture flow.

### Phase 4 — operator integration

- [ ] Provisioning UI gains a "use PE flow" checkbox per template.
- [ ] Jobs page surfaces per-step progress from checkins.
- [ ] Operator-facing docs (`docs/WINPE_FLOW.md`).
- **Exit criteria**: a non-author can clone a VM via the web UI with the PE flow enabled, and watch it deploy on the Jobs page.

## Open questions / deferred

These were considered and deliberately left out of v1.

- **Auth.** v1 is LAN-trusted plain HTTP. Plumb HMAC-SHA256 of `(timestamp + uuid + path)` with a shared secret in `Bootstrap.json` when the orchestrator leaves the dev Mac. Per-VM tokens (token attached to `winpe_targets`, served via a per-VM config volume) are the natural graduation. mTLS is the long-term answer but materially more lifecycle work — defer unless a customer demands it.
- **Discovery.** Hardcoded URL in `Bootstrap.json` for v1. mDNS via `_autopilot._tcp.local` for v1.5 (zero-config on macOS LAN; works on Proxmox if avahi runs). DHCP option 252 (or vendor-class 60+43) for environments where the orchestrator can't be on a known hostname — likely needed before a public release.
- **Proxmox VE boot delivery.** UTM attaches the PE ISO via `utmctl` in v1. Proxmox needs either ISO upload via API + cdrom attach (simple, mirrors UTM) or iPXE chainload from a TFTP/HTTP boot path (real PXE story). Phase 5 once the v1 flow is solid.
- **install.wim level III** (pre-staged harness inside install.wim). Saves the `stage-files` step and avoids the per-VM zip render. Add when the harness is stable enough that "rebuild install.wim to ship a harness change" doesn't feel like a tax.
- **Captured/sysprepped golden image (level IV).** Cuts deploy from ~5 min to ~2-3 min by skipping specialize altogether. Build-time investment is real (a build VM, audit-mode customization, sysprep, capture). Worth it once VM throughput becomes a bottleneck.
- **x64.** Same pipeline; just point `Build-*.ps1` at x64 ADK paths and x64 virtio drivers and pwsh/.NET zips.
- **Re-fetch manifest mid-deploy.** Useful if a future step needs to branch on hardware discovered earlier in the manifest. Add a `re-fetch-manifest` step type and let PE call back to a per-VM endpoint that returns a continuation manifest.
- **`Rebuild` button** in the web UI that SSHes the build host and runs the script async. Operationally clean; not needed v1.
- **WiFi profile staging.** Only matters for physical hardware. Skip until physical is on the roadmap.
- **BitLocker.** `WinPE-EnhancedStorage` is included in v1's PE WIM so the option exists, but no `bitlocker` step type until a use case lands.

## References

- DeployR `PEPrep.ps1`, `BootstrapPE.ps1`, `winpeshl.ini`, `Unattend_PE_*.xml` — the patterns adopted here. Code is not used; design is.
- Microsoft ADK / WinPE docs.
- Existing repo: `autopilot-proxmox/scripts/remaster_win11_noprompt.sh` (the noprompt pattern via `cdboot_noprompt.efi`); we replace it with `efisys_noprompt.bin` at oscdimg time.
- Existing repo: `autopilot-proxmox/web/unattend_renderer.py`, `sequence_compiler.py`, `sequences_db.py` — reused.
