#Requires -Version 7
<#
.SYNOPSIS
    Verify build host setup before running Build-*.ps1 for the first time.
#>

[CmdletBinding()]
param(
    [string] $AdkRoot = 'C:\Program Files (x86)\Windows Kits\10',
    [string] $BuildRoot = 'C:\BuildRoot',
    [string] $WorkDriveLetter
)

$ErrorActionPreference = 'Continue'
$failures = @()

function Check([string]$Name, [scriptblock]$Test, [string]$FixHint) {
    try {
        $result = & $Test
        if ($result) {
            Write-Host ("[ OK ]  {0}" -f $Name) -ForegroundColor Green
        } else {
            Write-Host ("[FAIL]  {0}" -f $Name) -ForegroundColor Red
            Write-Host ("        Hint: {0}" -f $FixHint) -ForegroundColor Yellow
            $script:failures += $Name
        }
    } catch {
        Write-Host ("[FAIL]  {0} (error: {1})" -f $Name, $_.Exception.Message) -ForegroundColor Red
        Write-Host ("        Hint: {0}" -f $FixHint) -ForegroundColor Yellow
        $script:failures += $Name
    }
}

Write-Host ('=' * 70)
Write-Host 'PE Flight Check'
Write-Host ('=' * 70)

Check 'Running PowerShell 7+' { $PSVersionTable.PSVersion.Major -ge 7 } 'Install pwsh 7 (winget install Microsoft.PowerShell)'
Check 'Running as Administrator' {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    ([System.Security.Principal.WindowsPrincipal]$id).IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
} 'Run pwsh as Administrator'

Check "ADK installed at $AdkRoot" { Test-Path $AdkRoot } "Install Windows ADK to $AdkRoot"
Check 'ADK has WinPE add-on (arm64 winpe.wim)' {
    Test-Path (Join-Path $AdkRoot 'Assessment and Deployment Kit\Windows Preinstallation Environment\arm64\en-us\winpe.wim')
} 'Install the WinPE add-on for the ADK'
Check 'ADK has oscdimg (arm64)' {
    Test-Path (Join-Path $AdkRoot 'Assessment and Deployment Kit\Deployment Tools\arm64\Oscdimg\oscdimg.exe')
} 'Install ADK Deployment Tools'
Check 'ADK has efisys_noprompt.bin (arm64)' {
    Test-Path (Join-Path $AdkRoot 'Assessment and Deployment Kit\Deployment Tools\arm64\Oscdimg\efisys_noprompt.bin')
} 'Reinstall ADK Deployment Tools — efisys_noprompt.bin should ship by default'

Check "BuildRoot exists ($BuildRoot)" { Test-Path $BuildRoot } "Create $BuildRoot and the inputs/outputs/work/src subdirs"
Check "BuildRoot\inputs\windows has at least one ISO" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\windows') -Filter '*.iso' -ErrorAction SilentlyContinue).Count -gt 0
} "Stage a Windows ISO at $BuildRoot\inputs\windows"
Check "BuildRoot\inputs\virtio has at least one ISO" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\virtio') -Filter '*.iso' -ErrorAction SilentlyContinue).Count -gt 0
} "Stage virtio-win.iso at $BuildRoot\inputs\virtio"
Check "BuildRoot\inputs\runtime has pwsh 7 zip" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\runtime') -Filter 'PowerShell-*-win-*.zip' -ErrorAction SilentlyContinue).Count -gt 0
} "Download pwsh 7 zip and place in $BuildRoot\inputs\runtime"
Check "BuildRoot\inputs\runtime has .NET 8 runtime zip" {
    @(Get-ChildItem -Path (Join-Path $BuildRoot 'inputs\runtime') -Filter 'dotnet-runtime-*-win-*.zip' -ErrorAction SilentlyContinue).Count -gt 0
} "Download .NET 8 runtime zip and place in $BuildRoot\inputs\runtime"

Check 'OpenSSH server present (built-in or standalone)' {
    # Either path is fine — the standalone Win32-OpenSSH installer lands in C:\Program Files\OpenSSH\;
    # the Optional-Feature install lands in C:\Windows\System32\OpenSSH\.
    (Test-Path 'C:\Windows\System32\OpenSSH\sshd.exe') -or (Test-Path 'C:\Program Files\OpenSSH\sshd.exe')
} 'Install via Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 OR via the standalone Win32-OpenSSH MSI from https://github.com/PowerShell/Win32-OpenSSH/releases'
Check 'OpenSSH server running' {
    (Get-Service -Name sshd -ErrorAction SilentlyContinue).Status -eq 'Running'
} 'Start-Service sshd; Set-Service sshd -StartupType Automatic'
Check 'OpenSSH default shell is pwsh 7' {
    (Get-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -ErrorAction SilentlyContinue).DefaultShell -eq 'C:\Program Files\PowerShell\7\pwsh.exe'
} "New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value 'C:\Program Files\PowerShell\7\pwsh.exe' -PropertyType String -Force"

if ($WorkDriveLetter) {
    # NTFS is the right file system for the work directory: DISM offline servicing
    # (Add-WindowsPackage on the mounted PE WIM) fails on ReFS Dev Drives with Win32
    # error 1812 (CDismCore::CacheImageSession). Inputs/outputs can live on a Dev
    # Drive for perf, but `lockPath` (and therefore `workDir`) must point at NTFS.
    Check "Work drive ${WorkDriveLetter}: is NTFS (DISM offline servicing requires NTFS, not ReFS)" {
        $vol = Get-Volume -DriveLetter $WorkDriveLetter -ErrorAction SilentlyContinue
        $vol -and $vol.FileSystem -eq 'NTFS'
    } "Either format ${WorkDriveLetter}: as NTFS, or set lockPath in build-pe-wim.config.json to a path on an NTFS volume (e.g. C:\\BuildRoot\\work\\.build.lock) and Add-MpPreference -ExclusionPath that path."
}

Write-Host ('=' * 70)
if ($failures.Count -eq 0) {
    Write-Host 'ALL CHECKS PASSED' -ForegroundColor Green
    exit 0
} else {
    Write-Host ("{0} CHECK(S) FAILED" -f $failures.Count) -ForegroundColor Red
    exit 1
}
