# PE Launcher (.NET 8 Console App) Design

## Goal

Replace the cmd.exe/batch/startnet.cmd boot chain with a single .NET 8 console app (`launcher.exe`) that owns the entire PE bootstrap lifecycle: window management, wpeinit, networking, manifest orchestration, content download with progress, step dispatch, and reboot.

## Architecture

**Entry point:** `winpeshl.ini [LaunchApps]` → `X:\autopilot\launcher.exe`

No cmd.exe, no batch files, no startnet.cmd. The launcher is a framework-dependent .NET 8 console app (small — .exe + .dll) that uses the runtime already baked into the WIM at `X:\Program Files\dotnet\`.

### Responsibility Split

**C# (launcher.exe):**
- Window lifecycle — always-on-top, maximized console
- Guard — scan volumes for existing Windows, auto-shutdown
- wpeinit — invoke and show progress
- Network wait — poll ipconfig, call `wpeutil InitializeNetwork`
- Identity — SMBIOS UUID via `Win32_ComputerSystemProduct` WMI
- Manifest fetch — `HttpClient` GET with retry/backoff
- Content download — `HttpClient` with byte-level progress (critical for the 6.5GB WIM)
- Checkins — `HttpClient` POST per step start/complete
- Orchestration loop — step dispatch, error handling, display updates
- Payload staging — copy files to target drive before reboot
- Reboot/shutdown — invoke `wpeutil` directly

**PowerShell (existing step cmdlets, invoked per-step):**
- `Invoke-PartitionStep` — diskpart/GPT layout
- `Expand-WindowsImage` — DISM WIM apply
- `Invoke-SetRegistryStep` — offline registry hive load/edit
- `Invoke-BcdbootStep` — EFI boot config + BOOTAA64.EFI fallback
- `Invoke-InjectDriverStep` — DISM driver injection

## Display Layout

```
╔══════════════════════════════════════════════════════════════╗
║  Autopilot PE Bootstrap                                      ║
╠══════════════════════════════════════════════════════════════╣
║  UUID:    9A0EB631-7299-4504-B45A-29C5F75BCF56              ║
║  Vendor:  QEMU  │  Model: QEMU Virtual Machine              ║
║  IP:      192.168.64.20  │  Host: MINWINPC                  ║
║  Server:  http://bigmac26.local:5050                        ║
╠══════════════════════════════════════════════════════════════╣
║  [✓] Partition          7s    ESP=S: Windows=W:             ║
║  [▸] Apply WIM         1:42   ████████████░░░░░░  67%  4.3GB║
║  [ ] Write unattend                                          ║
║  [ ] Set registry                                            ║
║  [ ] Configure boot                                          ║
║  [ ] Stage payload                                           ║
║  [ ] Reboot                                                  ║
╠══════════════════════════════════════════════════════════════╣
║  Downloading install.wim (6.5 GB) ...                        ║
╚══════════════════════════════════════════════════════════════╝
```

- Header: machine identity (populated after WMI + network init)
- Step list: `[ ]` pending, `[▸]` active, `[✓]` done, `[✗]` error
- Active step shows elapsed time + progress bar (byte-level for downloads)
- Bottom status bar for current activity
- No scrolling log — structured view. Full transcript at `X:\Windows\Temp\autopilot-pe.log`

## Boot Flow

```
UEFI → winpeshl.exe → winpeshl.ini → launcher.exe
  │
  ├─ Phase 0: Window setup (maximize, always-on-top, title)
  │
  ├─ Phase 1: Guard check
  │   ├─ Win32_Volume WMI scan for Label="Windows", DriveType=3
  │   ├─ If ntoskrnl.exe found → display "Windows installed — shutting down"
  │   └─ wpeutil shutdown → exit
  │
  ├─ Phase 2: wpeinit
  │   ├─ Process.Start("wpeinit"), show spinner
  │   └─ Wait for exit
  │
  ├─ Phase 3: Network
  │   ├─ Loop: parse ipconfig for non-APIPA IPv4
  │   ├─ Call wpeutil InitializeNetwork on each retry
  │   └─ Display IP when found
  │
  ├─ Phase 4: Identity
  │   ├─ Win32_ComputerSystemProduct WMI → UUID, Vendor, Model
  │   └─ Populate header
  │
  ├─ Phase 5: Manifest
  │   ├─ GET /winpe/manifest/{uuid}
  │   ├─ Retry with backoff (configurable from Bootstrap.json)
  │   └─ Display step list
  │
  ├─ Phase 6: Execute steps (loop)
  │   ├─ POST checkin (starting)
  │   ├─ Download content if step has content block (byte-level progress)
  │   ├─ Dispatch to pwsh for Windows-native steps
  │   ├─ POST checkin (ok/error)
  │   └─ Update display
  │
  ├─ Phase 7: Stage payload
  │   ├─ Copy Bootstrap.json + Collect-HardwareHash.ps1 → W:\autopilot\
  │   └─ Display confirmation
  │
  └─ Phase 8: Reboot
      └─ wpeutil reboot
```

## Step Dispatch

### C# native (no pwsh):
- **Content download** — `HttpClient` with `Stream.CopyToAsync`, byte-level progress via `IProgress<long>`. Downloads to target partition (`W:\`) for large files (WIMs), `X:\Windows\Temp\` for small blobs.
- **write-unattend / stage-files** — download content blob, write to target path
- **reboot / shutdown** — `Process.Start("wpeutil", "reboot|shutdown")`
- **log** — display message

### pwsh dispatch (one invocation per step):
```csharp
var psi = new ProcessStartInfo {
    FileName = @"X:\Program Files\PowerShell\7\pwsh.exe",
    Arguments = $"-NoProfile -Command \"{command}\"",
    RedirectStandardOutput = true,
    RedirectStandardError = true,
};
```

- **partition** → `Import-Module ...; Invoke-PartitionStep -Layout uefi-standard`
- **apply-wim** (expand only, after C# downloads the WIM) → `Expand-WindowsImage -ImagePath ... -Index 1 -ApplyPath W:`
- **set-registry** → `Import-Module ...; Invoke-SetRegistryStep -Hive ... -Target ... -Keys ...`
- **bcdboot** → `Import-Module ...; Invoke-BcdbootStep -Windows W: -Esp S:`
- **inject-driver** → `Import-Module ...; Invoke-InjectDriverStep ...`

Launcher captures stdout/stderr, parses exit code, updates display, sends checkin.

## Error Handling

- If a step fails and `onError=halt`: display shows error in red, launcher blocks forever. Console stays visible for SSH debug.
- If `onError=continue`: log the error, advance to next step.
- Network retries: manifest fetch retries with exponential backoff (3 attempts). Content download retries on transient HTTP errors.
- pwsh dispatch: non-zero exit code = step failure. stderr captured and included in checkin error_message.

## Project Structure

```
build/launcher/
├── Launcher.csproj
├── Program.cs           # Entry point, phase orchestration
├── Display.cs           # Console UI rendering (box drawing, progress bars)
├── Guard.cs             # Volume scan + auto-shutdown
├── WpeInit.cs           # wpeinit + network wait + ipconfig parsing
├── Identity.cs          # SMBIOS via WMI (Win32_ComputerSystemProduct)
├── Orchestrator.cs      # HTTP: manifest fetch, content download, checkins
├── StepRunner.cs        # Step loop, pwsh dispatch, step type routing
└── Models/
    └── Manifest.cs      # JSON deserialization: manifest, steps, content refs
```

## Build Integration

**Phase added to Build-PeWim.ps1** (between .NET runtime extraction and payload staging):

```powershell
# ---- Phase 4b: Build launcher ----
dotnet publish "$payloadDir/../launcher" -c Release -r win-arm64 --no-self-contained -o "$tempLauncherOut"
Copy-Item "$tempLauncherOut/*" -Destination $payloadTarget -Force
```

Produces `launcher.exe` + `launcher.dll` in `X:\autopilot\`.

**Prerequisite:** .NET 8 SDK on the Windows build host.

**winpeshl.ini** (written by Build-PeWim.ps1):
```ini
[LaunchApps]
X:\autopilot\launcher.exe
```

**startnet.cmd** — no longer written. Build script removes the startnet.cmd override.

## Configuration

Launcher reads `X:\autopilot\Bootstrap.json` (same file Bootstrap.ps1 used):

```json
{
  "orchestratorUrl": "http://bigmac26.local:5050",
  "networkTimeoutSec": 60,
  "manifestRetries": 5,
  "manifestRetryBackoffSec": 3
}
```

No new config fields needed. The launcher is a drop-in replacement.

## What Gets Removed

- `build/pe-payload/start.cmd` — no longer needed
- `build/pe-payload/winpeshl.ini` — replaced by build-script-generated version
- startnet.cmd override in Build-PeWim.ps1 — replaced by winpeshl.ini pointing to launcher.exe
- Bootstrap.ps1's orchestration logic (guard, network wait, manifest fetch, step loop, staging, reboot) — moved to C#
- Bootstrap.ps1's module imports and `Invoke-BootstrapManifest` function — replaced by StepRunner.cs

**What stays:**
- Bootstrap.ps1 can remain as a fallback/debug tool (invoke manually from cmd)
- PowerShell step modules (Autopilot.PESteps, Autopilot.PETransport) — launcher invokes individual cmdlets
- Collect-HardwareHash.ps1 — runs on first Windows boot, unrelated to PE
- Debug.ps1 — useful for manual SSH debugging

## Target Architecture

- **Runtime:** .NET 8 framework-dependent (win-arm64)
- **Publish:** `dotnet publish -c Release -r win-arm64 --no-self-contained`
- **Output size:** ~200KB (exe + dll), uses runtime at `X:\Program Files\dotnet\`
