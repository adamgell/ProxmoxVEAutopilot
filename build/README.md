# Build host setup runbook

One-time setup for the Windows-on-Proxmox VM that builds `install.wim` and the PE WIM.
Run **`build/PrePEFlight-Check.ps1`** at the end to verify.

## 1. Base OS

- Windows 11 (any recent edition with ADK support).
- 8+ GB RAM, 100+ GB disk (the staging WIM mount + outputs add up).
- Local Administrator account.

## 2. PowerShell 7

```powershell
winget install --id Microsoft.PowerShell --source winget
```

Verify: `pwsh --version` shows 7.x.

## 3. Windows ADK + WinPE add-on

Download from <https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install>.
Install ADK first, then the WinPE add-on. Default location:
`C:\Program Files (x86)\Windows Kits\10\`.

## 4. OpenSSH server

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic

# Default shell = pwsh 7 (critical: without this, SSH lands in cmd.exe)
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Program Files\PowerShell\7\pwsh.exe' -PropertyType String -Force
```

### Authorize your dev-Mac key

Copy `~/.ssh/id_ed25519.pub` from the Mac. On the build host as Administrator:

```powershell
# Place in BOTH paths (Windows quirk: Administrators-group keys live under ProgramData)
$key = '<paste-public-key-here>'
Add-Content -Path "$env:USERPROFILE\.ssh\authorized_keys" -Value $key
Add-Content -Path 'C:\ProgramData\ssh\administrators_authorized_keys' -Value $key

# Lock down ACLs on the second file (sshd refuses lax permissions)
icacls 'C:\ProgramData\ssh\administrators_authorized_keys' /inheritance:r
icacls 'C:\ProgramData\ssh\administrators_authorized_keys' /grant 'Administrators:F' 'SYSTEM:F'
```

Disable password auth in `C:\ProgramData\ssh\sshd_config`:

```
PasswordAuthentication no
PubkeyAuthentication yes
```

Restart sshd: `Restart-Service sshd`.

## 5. Dev Drive (recommended)

Create a Dev Drive (ReFS) for the WIM mount/scratch area. Settings → System → Storage → Disks & volumes → Create dev drive (or `New-VHD` + ReFS-format manually). Mount as a drive letter (e.g. `D:`).

Verify trusted state:

```powershell
fsutil devdrv query D:
# Expect: "Trusted: Yes"

# If not trusted:
fsutil devdrv setfiltered /unfiltered D:
```

Then point `BuildRoot\work\` at it (via symlink or by setting `BuildRoot=D:\BuildRoot` in the build configs).

## 6. Directory layout

```powershell
$Root = 'C:\BuildRoot'   # or D:\BuildRoot if Dev Drive
New-Item -ItemType Directory -Path $Root\inputs\windows  -Force
New-Item -ItemType Directory -Path $Root\inputs\virtio   -Force
New-Item -ItemType Directory -Path $Root\inputs\runtime  -Force
New-Item -ItemType Directory -Path $Root\src             -Force
New-Item -ItemType Directory -Path $Root\outputs         -Force
New-Item -ItemType Directory -Path $Root\work            -Force
```

## 7. Stage source ISOs and runtime zips

- `inputs\windows\` — your Windows 11 install ISO (e.g. `Win11_24H2_ARM64.iso`).
- `inputs\virtio\` — `virtio-win-<version>.iso` from <https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/>.
- `inputs\runtime\` — `PowerShell-<ver>-win-<arch>.zip` from <https://github.com/PowerShell/PowerShell/releases> and `dotnet-runtime-<ver>-win-<arch>.zip` from <https://dotnet.microsoft.com/en-us/download/dotnet/8.0>.

## 8. Clone this repo into `src\`

```powershell
cd C:\BuildRoot\src
git clone https://github.com/<your-fork>/ProxmoxVEAutopilot.git .
```

## 9. Defender exclusions (skip if using Dev Drive)

If `BuildRoot\work\` is **not** on a Dev Drive, exclude it from real-time scanning — DISM mount/dismount of a 4 GB WIM with Defender enabled is 10x slower and prone to lock contention:

```powershell
Add-MpPreference -ExclusionPath 'C:\BuildRoot\work'
```

## 10. Verify

From an elevated pwsh:

```powershell
cd C:\BuildRoot\src
.\build\PrePEFlight-Check.ps1 -AdkRoot 'C:\Program Files (x86)\Windows Kits\10' -BuildRoot 'C:\BuildRoot' -WorkDriveLetter D
```

Expect `ALL CHECKS PASSED`. Address any failure before running the build scripts.
