#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$PublicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFD1r+PkL8s2wE9zQUf535TkDFVbMKnf+ItnZljMTu6Z me@adamgell.com',
    [string]$InstallDir = 'C:\Program Files\OpenSSH'
)

$ErrorActionPreference = 'Stop'

$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'OpenSSH-Win64' }
    'ARM64' { 'OpenSSH-ARM64' }
    'x86'   { 'OpenSSH-Win32' }
    default { throw "Unsupported architecture: $env:PROCESSOR_ARCHITECTURE" }
}
$zipUrl  = "https://github.com/PowerShell/Win32-OpenSSH/releases/latest/download/$arch.zip"
$tempZip = Join-Path $env:TEMP "$arch.zip"

Write-Host "Downloading $zipUrl"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $zipUrl -OutFile $tempZip -UseBasicParsing

Write-Host "Extracting to $InstallDir"
if (Test-Path $InstallDir) {
    Get-Service sshd, ssh-agent -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Running' } | Stop-Service -Force
    Remove-Item $InstallDir -Recurse -Force
}
$parent = Split-Path $InstallDir -Parent
Expand-Archive -Path $tempZip -DestinationPath $parent -Force
Move-Item -Path (Join-Path $parent $arch) -Destination $InstallDir -Force
Remove-Item $tempZip

Write-Host "Running install-sshd.ps1"
& (Join-Path $InstallDir 'install-sshd.ps1')

Write-Host "Configuring services"
Set-Service -Name sshd -StartupType Automatic
Set-Service -Name ssh-agent -StartupType Automatic
Start-Service ssh-agent
Start-Service sshd

Write-Host "Opening firewall (TCP 22)"
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
        -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

Write-Host "Setting default shell to PowerShell"
if (-not (Test-Path 'HKLM:\SOFTWARE\OpenSSH')) {
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
}
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value (Get-Command powershell.exe).Source -PropertyType String -Force | Out-Null

Write-Host "Installing public key for $env:USERNAME"
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$currentUser).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if ($isAdmin) {
    $keysFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    $keysDir  = Split-Path $keysFile -Parent
    if (-not (Test-Path $keysDir))  { New-Item $keysDir  -ItemType Directory -Force | Out-Null }
    if (-not (Test-Path $keysFile)) { New-Item $keysFile -ItemType File -Force | Out-Null }
    if (-not (Select-String -Path $keysFile -SimpleMatch $PublicKey -Quiet)) {
        Add-Content -Path $keysFile -Value $PublicKey -Encoding ascii
    }
    icacls.exe $keysFile /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
} else {
    $sshDir = Join-Path $env:USERPROFILE '.ssh'
    if (-not (Test-Path $sshDir)) { New-Item $sshDir -ItemType Directory -Force | Out-Null }
    $keysFile = Join-Path $sshDir 'authorized_keys'
    if (-not (Test-Path $keysFile)) { New-Item $keysFile -ItemType File -Force | Out-Null }
    if (-not (Select-String -Path $keysFile -SimpleMatch $PublicKey -Quiet)) {
        Add-Content -Path $keysFile -Value $PublicKey -Encoding ascii
    }
    icacls.exe $keysFile /inheritance:r /grant "$($env:USERNAME):F" | Out-Null
}

Write-Host ""
Write-Host "Done. sshd: $((Get-Service sshd).Status), key file: $keysFile"
