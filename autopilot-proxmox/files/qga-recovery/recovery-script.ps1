param(
    [int] $TaskIntervalMinutes = 5,
    [int] $RestartIntervalMinutes = 10
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
$qgaStateDir = Join-Path $env:ProgramData 'qemu-ga'
$qgaLogPath = Join-Path $qgaStateDir 'qemu-ga.log'

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

function Get-QgaExecutablePath {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Qemu-ga\qemu-ga.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Qemu-ga\qemu-ga.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    $svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
    if ($svcInfo -and $svcInfo.PathName) {
        if ($svcInfo.PathName -match '"([^"]*qemu-ga\.exe)"') {
            return $Matches[1]
        }
        if ($svcInfo.PathName -match '([^\s]*qemu-ga\.exe)') {
            return $Matches[1]
        }
    }

    throw 'qemu-ga.exe was not found under Program Files and could not be parsed from the service command line.'
}

function Get-QgaServiceCommandLine {
    param(
        [Parameter(Mandatory)] [string] $ExePath,
        [Parameter(Mandatory)] [string] $StateDir,
        [Parameter(Mandatory)] [string] $LogFile
    )

    return ('"{0}" -d -m virtio-serial -p \\.\Global\org.qemu.guest_agent.0 --retry-path --block-rpcs=guest-network-get-interfaces -t "{1}" -l "{2}"' -f $ExePath, $StateDir, $LogFile)
}

function Set-QgaServiceCommandLine {
    param(
        [Parameter(Mandatory)] [string] $StateDir,
        [Parameter(Mandatory)] [string] $LogFile
    )

    New-DirectoryIfMissing -Path $StateDir
    $exePath = Get-QgaExecutablePath
    $binPath = Get-QgaServiceCommandLine -ExePath $exePath -StateDir $StateDir -LogFile $LogFile

    $svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction Stop
    $changeResult = Invoke-CimMethod -InputObject $svcInfo -MethodName Change -Arguments @{
        PathName = $binPath
    }
    if ($changeResult.ReturnValue -ne 0) {
        throw "QEMU-GA service command-line configuration failed with Win32_Service.Change return $($changeResult.ReturnValue)."
    }

    & sc.exe config QEMU-GA start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QEMU-GA delayed-auto configuration failed with exit $LASTEXITCODE."
    }

    Write-RecoveryLog "Configured QEMU-GA binPath=$binPath"
}

function Get-WatchdogScriptContent {
    param(
        [Parameter(Mandatory)] [int] $IntervalMinutes,
        [Parameter(Mandatory)] [string] $QgaStateDir,
        [Parameter(Mandatory)] [string] $QgaLogPath
    )

    return @"
`$ErrorActionPreference = 'SilentlyContinue'
`$root = Join-Path `$env:ProgramData 'ProxmoxVEAutopilot\OSD'
`$logPath = Join-Path `$root 'qga-watchdog.log'
`$statePath = Join-Path `$root 'qga-watchdog-last-restart.txt'
`$qgaStateDir = '$($QgaStateDir.Replace("'", "''"))'
`$qgaLogPath = '$($QgaLogPath.Replace("'", "''"))'
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

function Get-QgaExecutablePath {
    `$candidates = @(
        (Join-Path `$env:ProgramFiles 'Qemu-ga\qemu-ga.exe'),
        (Join-Path `${env:ProgramFiles(x86)} 'Qemu-ga\qemu-ga.exe')
    )
    foreach (`$candidate in `$candidates) {
        if (`$candidate -and (Test-Path -LiteralPath `$candidate)) {
            return `$candidate
        }
    }
    `$svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
    if (`$svcInfo -and `$svcInfo.PathName) {
        if (`$svcInfo.PathName -match '"([^"]*qemu-ga\.exe)"') {
            return `$Matches[1]
        }
        if (`$svcInfo.PathName -match '([^\s]*qemu-ga\.exe)') {
            return `$Matches[1]
        }
    }
    return `$null
}

function Set-QgaServiceCommandLine {
    try {
        if (-not (Test-Path -LiteralPath `$qgaStateDir)) {
            New-Item -ItemType Directory -Path `$qgaStateDir -Force | Out-Null
        }
        `$exePath = Get-QgaExecutablePath
        if (-not `$exePath) {
            Add-WatchdogLog 'qemu-ga.exe not found; cannot enforce service command line.'
            return `$false
        }
        `$desired = ('"{0}" -d -m virtio-serial -p \\.\Global\org.qemu.guest_agent.0 --retry-path --block-rpcs=guest-network-get-interfaces -t "{1}" -l "{2}"' -f `$exePath, `$qgaStateDir, `$qgaLogPath)
        `$svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
        if (`$svcInfo -and `$svcInfo.PathName -eq `$desired) {
            return `$false
        }
        `$changeResult = Invoke-CimMethod -InputObject `$svcInfo -MethodName Change -Arguments @{
            PathName = `$desired
        }
        if (`$changeResult.ReturnValue -eq 0) {
            & sc.exe config QEMU-GA start= delayed-auto | Out-Null
            Add-WatchdogLog "Corrected QEMU-GA service command line."
            return `$true
        }
        Add-WatchdogLog "Failed to correct QEMU-GA service command line: Win32_Service.Change return `$(`$changeResult.ReturnValue)"
    } catch {
        Add-WatchdogLog "Failed to correct QEMU-GA service command line: `$(`$_.Exception.Message)"
    }
    return `$false
}

try {
    `$svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    if (-not `$svc) {
        Add-WatchdogLog 'QEMU-GA service missing.'
        exit 0
    }

    `$serviceCommandLineChanged = Set-QgaServiceCommandLine

    `$svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
    if (`$svcInfo -and `$svcInfo.StartMode -ne 'Auto') {
        & sc.exe config QEMU-GA start= delayed-auto | Out-Null
        Add-WatchdogLog 'Set QEMU-GA service startup to delayed-auto.'
    }

    if (`$svc.Status -ne 'Running') {
        Start-Service -Name QEMU-GA -ErrorAction Stop
        (Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
        Set-Content -LiteralPath `$statePath -Value (Get-Date -Format o) -Encoding ASCII
        Add-WatchdogLog 'Started QEMU-GA service.'
        exit 0
    }

    if (`$serviceCommandLineChanged) {
        Restart-Service -Name QEMU-GA -Force -ErrorAction Stop
        (Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
        Set-Content -LiteralPath `$statePath -Value (Get-Date -Format o) -Encoding ASCII
        Add-WatchdogLog 'Restarted QEMU-GA after service command-line correction.'
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
New-DirectoryIfMissing -Path $qgaStateDir
Write-RecoveryLog 'Starting QGA recovery/watchdog installation.'

$svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
if (-not $svc) {
    throw 'QEMU-GA service is not installed. Install QEMU Guest Agent first.'
}

Set-QgaServiceCommandLine -StateDir $qgaStateDir -LogFile $qgaLogPath

& sc.exe failure QEMU-GA reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "QEMU-GA service recovery configuration failed with exit $LASTEXITCODE."
}

& sc.exe failureflag QEMU-GA 1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "QEMU-GA service failure flag configuration failed with exit $LASTEXITCODE."
}

Set-Content -LiteralPath $watchdogPath `
    -Value (Get-WatchdogScriptContent -IntervalMinutes $RestartIntervalMinutes -QgaStateDir $qgaStateDir -QgaLogPath $qgaLogPath) `
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
