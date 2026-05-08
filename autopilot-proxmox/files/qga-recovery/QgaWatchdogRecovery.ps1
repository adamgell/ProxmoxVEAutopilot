param(
    [int] $TaskIntervalMinutes = 5,
    [int] $RestartIntervalMinutes = 30
)

$ErrorActionPreference = 'Stop'

if ($TaskIntervalMinutes -lt 1) {
    throw 'TaskIntervalMinutes must be at least 1.'
}
if ($RestartIntervalMinutes -lt 0) {
    throw 'RestartIntervalMinutes must be 0 or greater.'
}

$root = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\OSD'
$watchdogPath = Join-Path $root 'QgaWatchdog.ps1'
$statePath = Join-Path $root 'qga-watchdog-last-restart.txt'
$logPath = Join-Path $root 'qga-watchdog-recovery.log'

function New-DirectoryIfMissing {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Write-RecoveryLog {
    param([Parameter(Mandatory)] [string] $Message)
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-WatchdogScriptContent {
    param([Parameter(Mandatory)] [int] $IntervalMinutes)

    return @"
`$ErrorActionPreference = 'SilentlyContinue'
`$root = Join-Path `$env:ProgramData 'ProxmoxVEAutopilot\OSD'
`$logPath = Join-Path `$root 'qga-watchdog.log'
`$statePath = Join-Path `$root 'qga-watchdog-last-restart.txt'
`$restartIntervalMinutes = $IntervalMinutes

function Add-WatchdogLog {
    param([Parameter(Mandatory)] [string] `$Message)
    try {
        if (-not (Test-Path -LiteralPath `$root)) {
            New-Item -ItemType Directory -Path `$root -Force | Out-Null
        }
        Add-Content -LiteralPath `$logPath -Value "`$(Get-Date -Format o) `$Message" -Encoding UTF8
    } catch {}
}

try {
    `$svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    if (-not `$svc) {
        Add-WatchdogLog 'QEMU-GA service missing.'
        exit 0
    }

    `$svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
    if (`$svcInfo -and `$svcInfo.StartMode -ne 'Auto') {
        & sc.exe config QEMU-GA start= auto | Out-Null
        Add-WatchdogLog 'Set QEMU-GA service startup to auto.'
    }

    if (`$svc.Status -ne 'Running') {
        Start-Service -Name QEMU-GA -ErrorAction Stop
        (Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
        Set-Content -LiteralPath `$statePath -Value (Get-Date -Format o) -Encoding ASCII
        Add-WatchdogLog 'Started QEMU-GA service.'
        exit 0
    }

    if (`$restartIntervalMinutes -gt 0) {
        `$shouldRestart = `$false
        if (-not (Test-Path -LiteralPath `$statePath)) {
            `$shouldRestart = `$true
        } else {
            try {
                `$lastRestart = [datetime]::Parse((Get-Content -LiteralPath `$statePath -Raw).Trim())
                if ((New-TimeSpan -Start `$lastRestart -End (Get-Date)).TotalMinutes -ge `$restartIntervalMinutes) {
                    `$shouldRestart = `$true
                }
            } catch {
                `$shouldRestart = `$true
            }
        }

        if (`$shouldRestart) {
            Restart-Service -Name QEMU-GA -Force -ErrorAction Stop
            (Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
            Set-Content -LiteralPath `$statePath -Value (Get-Date -Format o) -Encoding ASCII
            Add-WatchdogLog "Recycled QEMU-GA service after `$restartIntervalMinutes minute interval."
        }
    }
} catch {
    Add-WatchdogLog "QGA watchdog error: `$(`$_.Exception.Message)"
}
"@
}

New-DirectoryIfMissing -Path $root
Write-RecoveryLog 'Starting QGA recovery/watchdog installation.'

$svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
if (-not $svc) {
    throw 'QEMU-GA service is not installed. Install QEMU Guest Agent first.'
}

& sc.exe config QEMU-GA start= auto | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "QEMU-GA auto-start configuration failed with exit $LASTEXITCODE."
}

& sc.exe failure QEMU-GA reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "QEMU-GA service recovery configuration failed with exit $LASTEXITCODE."
}

& sc.exe failureflag QEMU-GA 1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "QEMU-GA service failure flag configuration failed with exit $LASTEXITCODE."
}

Set-Content -LiteralPath $watchdogPath `
    -Value (Get-WatchdogScriptContent -IntervalMinutes $RestartIntervalMinutes) `
    -Encoding UTF8
Set-Content -LiteralPath $statePath -Value (Get-Date -Format o) -Encoding ASCII

$taskRun = "powershell.exe -ExecutionPolicy Bypass -NoProfile -File `"$watchdogPath`""
& schtasks.exe /Create `
    /TN '\ProxmoxVEAutopilot\QgaWatchdog' `
    /SC MINUTE `
    /MO $TaskIntervalMinutes `
    /RU SYSTEM `
    /RL HIGHEST `
    /F `
    /TR $taskRun | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "QGA watchdog scheduled task registration failed with exit $LASTEXITCODE."
}

$svc = Get-Service -Name QEMU-GA -ErrorAction Stop
if ($svc.Status -eq 'Running') {
    Restart-Service -Name QEMU-GA -Force -ErrorAction Stop
} else {
    Start-Service -Name QEMU-GA -ErrorAction Stop
}
(Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
Set-Content -LiteralPath $statePath -Value (Get-Date -Format o) -Encoding ASCII
Write-RecoveryLog "QGA recovery complete. Watchdog=$watchdogPath TaskIntervalMinutes=$TaskIntervalMinutes RestartIntervalMinutes=$RestartIntervalMinutes"
