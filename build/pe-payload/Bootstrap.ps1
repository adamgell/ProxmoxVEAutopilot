# Placeholder PE bootstrap. Plan 3 replaces this with the manifest interpreter.
# Runs from unattend.xml's RunSynchronousCommand (windowsPE pass).

$transcript = 'X:\Windows\Temp\autopilot-pe.log'
Start-Transcript -Path $transcript -Append -Force | Out-Null

Write-Host '========================================'
Write-Host 'Autopilot PE bootstrap (placeholder)'
Write-Host '========================================'
Write-Host "PowerShell version : $($PSVersionTable.PSVersion)"
Write-Host "Edition            : $($PSVersionTable.PSEdition)"
Write-Host "OS                 : $([System.Environment]::OSVersion.VersionString)"
Write-Host "Architecture       : $env:PROCESSOR_ARCHITECTURE"
Write-Host "Hostname           : $env:COMPUTERNAME"

try {
    $cs = Get-CimInstance Win32_ComputerSystemProduct -ErrorAction Stop
    Write-Host "SMBIOS UUID        : $($cs.UUID)"
    Write-Host "Vendor             : $($cs.Vendor)"
    Write-Host "Name               : $($cs.Name)"
} catch {
    Write-Host "SMBIOS UUID        : <Get-CimInstance failed: $_>"
}

if (Test-Path 'X:\autopilot\Bootstrap.json') {
    $config = Get-Content 'X:\autopilot\Bootstrap.json' -Raw | ConvertFrom-Json
    Write-Host "Orchestrator URL   : $($config.orchestratorUrl)"
} else {
    Write-Host "Orchestrator URL   : <Bootstrap.json not found>"
}

Write-Host ''
Write-Host 'Placeholder bootstrap complete. Plan 3 will replace this.'
Write-Host 'Dropping to interactive shell for inspection.'

Stop-Transcript | Out-Null

# Leave an interactive prompt so the operator can verify PE comes up correctly.
# Plan 3 replaces this with the manifest dispatch loop.
