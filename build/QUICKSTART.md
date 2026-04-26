# Build host quickstart

End-to-end sequence to bring a Windows-on-Proxmox build host online and produce your first PE WIM artifact. The full reference for each step lives in `build/README.md`; this doc is the operational checklist.

**Goal:** at the end of Part G you'll have a working PE ISO that boots in UTM and prints the placeholder bootstrap output — confirming the entire build pipeline works end to end.

---

## Decisions to make first

| Choice | Recommended | Why |
|---|---|---|
| Build host arch | **x86_64 Windows 11 Pro** | Most-tested ADK target, no Rosetta-equivalent quirks. Builds artifacts for any target arch — `Build-PeWim.ps1 architecture: arm64` works fine from an x86_64 builder. |
| Disk | **120+ GB** | ADK ~10 GB; Win11 ISO ~5 GB; virtio-win.iso ~600 MB; pwsh/dotnet zips ~100 MB; outputs accumulate. |
| Memory | **8–16 GB** | 8 minimum, 16 comfortable. |
| Network | **Bridge reachable from your Mac on port 22** | Only port we need open. |

---

## Part A — Provision the VM (Proxmox UI, ~20 min)

**1.** Upload the Windows 11 ISO and `virtio-win.iso` to a Proxmox storage if you haven't already.

**2.** In Proxmox: **Create VM**.
- General: name `buildhost-winpe`, OS type Windows 11 (or 10 / Server 2022 — same DISM)
- System: BIOS=OVMF (UEFI), Machine=q35, add EFI Disk on your storage, add TPM v2.0
- Hard Disk: SCSI Controller=VirtIO SCSI single, 120 GB on your fast pool, Discard=on, IO thread=on
- CPU: 4+ cores, type=host
- Memory: 8192–16384 MB, ballooning off
- Network: VirtIO, on the bridge that's reachable from your Mac
- **Don't start yet** — go to Hardware tab and add the virtio-win.iso as a second CD/DVD drive (you'll need it during install for the SCSI driver).

**3.** Start the VM. During Setup:
- When prompted "Where do you want to install Windows?": click **Load driver** → browse the virtio CD → `vioscsi\w11\amd64\` → install. Disk will appear.
- Skip the Microsoft account requirement: at the OOBE network screen, Shift+F10 → `oobe\bypassnro` → reboot, then choose "I don't have internet" → "Continue with limited setup."
- Create local Administrator account `buildadmin` (or whatever you prefer). Strong password.

**Checkpoint A:** You can RDP into `buildhost-winpe` as Administrator. Win11 desktop appears.

---

## Part B — Install pwsh 7 + ADK (~30 min, mostly downloads)

On the build host (Administrator PowerShell from Start menu):

**4.** Install pwsh 7:
```powershell
winget install --id Microsoft.PowerShell --source winget --accept-package-agreements --accept-source-agreements
```
Verify: open a new terminal, type `pwsh --version` → 7.x.x.

**5.** Install Windows ADK + WinPE add-on. Two installers, in order:
- Download "Windows ADK for Windows 11" from Microsoft: <https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install>
- Run `adksetup.exe`. Components: at minimum **Deployment Tools** + **Imaging and Configuration Designer (ICD)**. Install to default location (`C:\Program Files (x86)\Windows Kits\10\`).
- Run `adkwinpesetup.exe` (the WinPE add-on, separate download). Default location.

**Checkpoint B:** All three paths exist (verify in PowerShell):
```powershell
Test-Path 'C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Windows Preinstallation Environment\amd64\en-us\winpe.wim'
Test-Path 'C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Windows Preinstallation Environment\arm64\en-us\winpe.wim'
Test-Path 'C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools\arm64\Oscdimg\efisys_noprompt.bin'
```
All three should return `True`.

---

## Part C — OpenSSH + key auth (~15 min)

In an Administrator PowerShell:

**6.** Install OpenSSH Server:
```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
```

**7.** Set pwsh 7 as the default SSH shell (otherwise SSH lands in `cmd.exe` and quoting becomes a nightmare):
```powershell
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Program Files\PowerShell\7\pwsh.exe' -PropertyType String -Force
Restart-Service sshd
```

**8.** On your **Mac**, copy the public key:
```bash
cat ~/.ssh/id_ed25519.pub   # or id_rsa.pub
```

**9.** Back on the **build host**, paste it into BOTH locations (Windows quirk — Administrators-group keys must live under `ProgramData`):
```powershell
$key = '<paste-the-public-key-line-here>'
New-Item -ItemType Directory "$env:USERPROFILE\.ssh" -Force | Out-Null
Add-Content -Path "$env:USERPROFILE\.ssh\authorized_keys" -Value $key
Add-Content -Path 'C:\ProgramData\ssh\administrators_authorized_keys' -Value $key

# Lock down ACLs on the ProgramData copy (sshd refuses lax permissions)
icacls 'C:\ProgramData\ssh\administrators_authorized_keys' /inheritance:r
icacls 'C:\ProgramData\ssh\administrators_authorized_keys' /grant 'Administrators:F' 'SYSTEM:F'
```

**10.** Disable password auth in `C:\ProgramData\ssh\sshd_config` (edit with notepad as Admin):
```
PasswordAuthentication no
PubkeyAuthentication yes
```
Then `Restart-Service sshd`.

**Checkpoint C:** From your **Mac**, this works without a password prompt and lands in pwsh:
```bash
ssh buildadmin@<build-host-ip-or-hostname> 'pwsh -Command "Write-Host hello from $env:COMPUTERNAME"'
```
You should see `hello from BUILDHOST-WINPE` (or whatever your VM is named).

---

## Part D — Dev Drive + directory layout (~10 min)

**11.** Create a Dev Drive. Easiest path: Settings → System → Storage → Disks & volumes → **Create dev drive** → 64 GB on the system disk → letter `D:`. (If you have a second virtual disk attached, format it as ReFS instead.)

**12.** Verify trusted state:
```powershell
fsutil devdrv query D:
# If "Trusted: No":
fsutil devdrv setfiltered /unfiltered D:
fsutil devdrv query D:   # should now show Trusted: Yes
```

**13.** Create directory layout (use `D:\BuildRoot` to keep work on the Dev Drive; `inputs`/`outputs` can stay on `D:` too since it's all the same volume):
```powershell
$Root = 'D:\BuildRoot'
New-Item -ItemType Directory -Path "$Root\inputs\windows"  -Force
New-Item -ItemType Directory -Path "$Root\inputs\virtio"   -Force
New-Item -ItemType Directory -Path "$Root\inputs\runtime"  -Force
New-Item -ItemType Directory -Path "$Root\src"             -Force
New-Item -ItemType Directory -Path "$Root\outputs"         -Force
New-Item -ItemType Directory -Path "$Root\work"            -Force
```

If you stick with `C:\BuildRoot` instead of a Dev Drive, also do `Add-MpPreference -ExclusionPath 'C:\BuildRoot\work'` to keep Defender out of the WIM mount.

**Checkpoint D:** `dir D:\BuildRoot` shows the 6 subdirectories.

---

## Part E — Stage source artifacts (~20 min, mostly downloads)

**14.** Stage the Windows ISO (the same one you used to install the build host is fine, or download a fresh one) at:
```
D:\BuildRoot\inputs\windows\Win11_24H2_amd64.iso
```
The exact name doesn't matter — your build config will reference the path.

**15.** Stage the virtio-win ISO. Download the latest stable from <https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/> (e.g. `virtio-win.iso` ~600 MB) → `D:\BuildRoot\inputs\virtio\virtio-win-<version>.iso`.

**16.** Stage the pwsh 7 + .NET 8 zips (these go INTO the PE WIM):
```powershell
# pwsh 7 win-amd64
Invoke-WebRequest -OutFile 'D:\BuildRoot\inputs\runtime\PowerShell-7.4.6-win-x64.zip' `
  https://github.com/PowerShell/PowerShell/releases/download/v7.4.6/PowerShell-7.4.6-win-x64.zip

# pwsh 7 win-arm64 (for ARM64 PE builds)
Invoke-WebRequest -OutFile 'D:\BuildRoot\inputs\runtime\PowerShell-7.4.6-win-arm64.zip' `
  https://github.com/PowerShell/PowerShell/releases/download/v7.4.6/PowerShell-7.4.6-win-arm64.zip

# .NET 8 runtime (NOT desktop runtime) for both arches
# Get exact URLs from https://dotnet.microsoft.com/en-us/download/dotnet/8.0
# Pick "Binaries" / .zip not the installer.
```
Versions don't have to match what's printed above — pick the latest LTS. Just keep the filename pattern `PowerShell-*-win-<arch>.zip` and `dotnet-runtime-*-win-<arch>.zip`.

**16b. (Optional) Stage Win32-OpenSSH for in-PE remote debugging.** When this zip is configured in the build config, the resulting PE WIM ships with an SSH server that auto-starts on boot — you can SSH directly into the booted PE as Administrator (key auth, port 22) and watch the bootstrap interactively. Hugely useful when something fails inside PE and the placeholder Bootstrap drops to a debug shell. The build keys you authorize at build time are baked into the WIM.

Download from <https://github.com/PowerShell/Win32-OpenSSH/releases> (latest `OpenSSH-Win64-*.zip` for amd64 PE builds; `OpenSSH-Win64arm64-*.zip` for arm64). Stage in the same `inputs\runtime\` dir:

```powershell
# Replace the version with whatever's the latest release on the GitHub page
$ver = 'v9.8.1.0p1-Beta'
Invoke-WebRequest -OutFile "D:\BuildRoot\inputs\runtime\OpenSSH-Win64-$ver.zip" `
  "https://github.com/PowerShell/Win32-OpenSSH/releases/download/$ver/OpenSSH-Win64.zip"
Invoke-WebRequest -OutFile "D:\BuildRoot\inputs\runtime\OpenSSH-Win64arm64-$ver.zip" `
  "https://github.com/PowerShell/Win32-OpenSSH/releases/download/$ver/OpenSSH-Win64arm64.zip"
```

To enable in a build, add two fields to your `build/build-pe-wim.config.json`:

```json
{
  …existing fields…
  "opensshZip":           "C:/BuildRoot/inputs/runtime/OpenSSH-Win64-v9.8.1.0p1-Beta.zip",
  "opensshAuthorizedKey": "ssh-ed25519 AAAAC3Nz… you@your-mac"
}
```

Both fields are required if either is set. The authorized key is the public key (single line; the contents of `~/.ssh/id_ed25519.pub` from your Mac) that grants Administrator login to the PE. After PE boots, `ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new Administrator@<pe-vm-ip>` lands you in cmd.exe (PE's default shell); from there `pwsh` gets you to PowerShell 7.

Host keys are ephemeral (regenerated per build, baked into the WIM). Each new PE WIM has different host keys — first SSH connect from your Mac will TOFU-add the new fingerprint via `accept-new`.

**17.** Clone the repo to `D:\BuildRoot\src\` (`git for Windows` if not yet installed: `winget install Git.Git`):
```powershell
cd D:\BuildRoot\src
git clone https://github.com/adamgell/ProxmoxVEAutopilot.git .
git fetch origin feature/winpe-osd-build-pipeline
git checkout feature/winpe-osd-build-pipeline
```
Or merge the PR first, then just `git pull` on `main`.

**Checkpoint E:** `dir D:\BuildRoot\inputs\runtime` shows 4 zips. `dir D:\BuildRoot\inputs\windows` shows the Windows ISO. `dir D:\BuildRoot\inputs\virtio` shows the virtio-win ISO.

---

## Part F — Pre-flight check (~2 min)

**18.** From an elevated pwsh on the build host:
```powershell
cd D:\BuildRoot\src
.\build\PrePEFlight-Check.ps1 `
    -AdkRoot 'C:\Program Files (x86)\Windows Kits\10' `
    -BuildRoot 'D:\BuildRoot' `
    -WorkDriveLetter D
```
Expected: `ALL CHECKS PASSED` in green.

If anything fails, the script tells you what hint to follow. Address each, re-run until green.

---

## Part G — First real build from your Mac (~30 min — install.wim takes a while)

Back on your **Mac**, in the worktree (or the merged main if you've merged the PR):

**19.** Configure the PE WIM build:
```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-build-pipeline
cp build/build-pe-wim.config.example.json build/build-pe-wim.config.json
# Edit build/build-pe-wim.config.json:
# - buildHost: hostname or IP of the build VM
# - buildHostUser: buildadmin (or whatever you used)
# - buildRootRemote: D:/BuildRoot   (forward slashes for SSH side)
# - architecture: arm64 (or amd64 — your target, not the build host's)
# - virtioIsoPath / pwsh7Zip / dotnetRuntimeZip: D:/BuildRoot/inputs/...
# - orchestratorUrl: http://<your-mac-hostname>.local:5000
# - outputDir: D:/BuildRoot/outputs
```

**20.** Run the PE build (this is the smaller, faster artifact — good first try):
```bash
./tools/build-pe-wim.sh
```
Watch the output. It rsyncs the payload tree, ssh-invokes `Build-PeWim.ps1`, streams DISM logs, then scps the WIM/ISO/sidecar/log back. On success: `DONE`, with `registered pe-wim <sha>`.

**21.** Verify locally:
```bash
sqlite3 var/artifacts/index.db 'SELECT sha256, kind, size FROM artifacts;'
ls -la var/artifacts/staging/        # WIM, ISO, .json, .log
ls -la var/artifacts/store/          # <sha>.wim
```

**22.** Boot the ISO in UTM (per `build/SMOKE-TEST.md` Section 3):
- Create a new Win11 ARM64 VM in UTM
- Attach `var/artifacts/staging/winpe-autopilot-arm64-<sha>.iso` as CD/DVD
- Boot
- Should boot **without** a "Press any key" prompt
- After ~30s of WinPE init, the placeholder Bootstrap output appears on the VM console: PowerShell version, SMBIOS UUID, orchestrator URL, etc.

If you see that output: **the entire build pipeline works end to end**. Plan 1 is verified.

**23.** (Optional, only if you want full v1 capability before Plan 2 ships) Run the install.wim build — slower (~30 min) but proves driver injection:
```bash
cp build/build-install-wim.config.example.json build/build-install-wim.config.json
# Edit similarly: windowsIsoPath, virtioIsoPath, edition (e.g. "Windows 11 Pro"), architecture, outputDir
./tools/build-install-wim.sh
```

---

## When something goes wrong

`build/SMOKE-TEST.md` has a troubleshooting table at the bottom. Most common gotchas:

| Symptom | Likely cause | Fix |
|---|---|---|
| SSH lands in `cmd.exe` | DefaultShell registry key not set, or sshd not restarted | Re-run Step 7 |
| `Mount-DiskImage` fails | SSH session unelevated | Add `whoami /groups \| findstr S-1-16-12288` (the High Mandatory Level SID) to your SSH command — should be present |
| `Add-WindowsPackage` "not applicable" | Architecture mismatch (using amd64 OC packages for an arm64 PE build, or vice versa) | Double-check `architecture` in the build config |
| PE boots but "Press any key" still appears | ISO built without `efisys_noprompt.bin` | Double-check Phase 11 of `Build-PeWim.ps1` was reached (look at the end of the build log) |
| Build script hangs at `Mount-WindowsImage` | Defender realtime scanning the WIM mount | Move `BuildRoot\work\` to a Dev Drive, OR add `BuildRoot\work\` to Defender exclusions |
| `rsync` "permission denied" | `administrators_authorized_keys` ACLs wrong | Re-run the `icacls` lines in Step 9 |

---

## When Part F is green

Tell the controller (me) and we'll move on to **Plan 2** (orchestrator API endpoints — `/winpe/manifest`, `/winpe/content`, `/winpe/checkin`). The PR (#34) can stay in review until then; nothing else blocks on it.
