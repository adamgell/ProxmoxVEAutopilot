<#
.SYNOPSIS
    Diagnostic dump on bootstrap failure (onError=halt path).

.DESCRIPTION
    Prints what's known so the operator (looking at the cmd.exe console or
    SSHing in) has context. Doesn't drop to interactive — winpeshl.ini already
    chains cmd.exe after wpeinit so the operator already has a shell.
#>
Write-Host ''
Write-Host '========================================'
Write-Host 'Autopilot PE bootstrap: DEBUG MODE'
Write-Host '========================================'

try {
    $cs = Get-CimInstance Win32_ComputerSystemProduct
    Write-Host "SMBIOS UUID  : $($cs.UUID)"
    Write-Host "Vendor       : $($cs.Vendor)"
    Write-Host "Name         : $($cs.Name)"
} catch {
    Write-Host "SMBIOS       : <error: $_>"
}

Write-Host "PowerShell   : $($PSVersionTable.PSVersion)"
Write-Host "Architecture : $env:PROCESSOR_ARCHITECTURE"
Write-Host "Hostname     : $env:COMPUTERNAME"

if (Test-Path 'X:\autopilot\Bootstrap.json') {
    $cfg = Get-Content 'X:\autopilot\Bootstrap.json' -Raw | ConvertFrom-Json
    Write-Host "Orchestrator : $($cfg.orchestratorUrl)"
}

Write-Host ''
Write-Host 'Network state:'
try {
    ipconfig | Out-Host
} catch {
    Write-Host "  <error: $_>"
}

Write-Host ''
Write-Host 'sshd status:'
try {
    Get-Service sshd -ErrorAction SilentlyContinue | Format-List Status, StartType | Out-Host
} catch {
    Write-Host "  <not installed or error: $_>"
}

Write-Host ''
Write-Host "See full transcript at: X:\Windows\Temp\autopilot-pe.log"
Write-Host "Use 'wpeutil reboot' or 'wpeutil shutdown' to exit PE."
Write-Host ''
