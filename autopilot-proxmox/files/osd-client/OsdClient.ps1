$ErrorActionPreference = 'Stop'

function New-DirectoryIfMissing {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

$OsdRoot = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\OSD'
$SetupLog = Join-Path $env:WINDIR 'Setup\Scripts\SetupComplete.log'
$ClientLog = Join-Path $OsdRoot 'osd-client.log'
New-DirectoryIfMissing -Path $OsdRoot
New-DirectoryIfMissing -Path (Split-Path -Parent $SetupLog)

function Add-OsdLogLine {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Line,
        [switch] $BestEffort
    )
    try {
        Add-Content -LiteralPath $Path -Value $Line -Encoding UTF8 -ErrorAction Stop
    } catch {
        if (-not $BestEffort) {
            throw
        }
    }
}

function Write-OsdLog {
    param([Parameter(Mandatory)] [string] $Message)
    $line = "$(Get-Date -Format o) $Message"
    Add-OsdLogLine -Path $ClientLog -Line $line
    Add-OsdLogLine -Path $SetupLog -Line $line -BestEffort
    Write-Host $line
}

function Read-OsdConfig {
    $path = Join-Path $OsdRoot 'osd-config.json'
    if (-not (Test-Path -LiteralPath $path)) {
        throw "OSD config not found: $path"
    }
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Get-LogTail {
    param([string] $Path, [int] $Lines = 80)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return (Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue) -join "`n"
}

function Invoke-OsdRequest {
    param(
        [Parameter(Mandatory)] [object] $Config,
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string] $Method,
        [hashtable] $Body,
        [string] $BearerToken,
        [int] $MaxAttempts = 8
    )
    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    $payload = $null
    if ($Body) { $payload = $Body | ConvertTo-Json -Depth 8 -Compress }
    $bases = @($Config.flask_base_url)
    if ($Config.PSObject.Properties.Match('flask_base_url_fallback').Count -gt 0 -and $Config.flask_base_url_fallback) {
        $bases += $Config.flask_base_url_fallback
    }
    $lastErr = $null
    foreach ($base in $bases) {
        $uri = ($base.TrimEnd('/')) + '/' + $Path.TrimStart('/')
        for ($i = 1; $i -le $MaxAttempts; $i++) {
            try {
                return Invoke-RestMethod -Uri $uri -Method $Method -Headers $headers `
                    -Body $payload -ContentType 'application/json' -TimeoutSec 30
            } catch {
                $lastErr = $_
                Write-OsdLog "request failed attempt=$i uri=$uri error=$($_.Exception.Message)"
                Start-Sleep -Seconds ([Math]::Min(20, 2 * $i))
            }
        }
    }
    throw $lastErr
}

function Send-StepState {
    param(
        [Parameter(Mandatory)] [object] $Config,
        [Parameter(Mandatory)] [int] $StepId,
        [Parameter(Mandatory)] [ValidateSet('running','ok','error')] [string] $State,
        [string] $ErrorMessage,
        [string] $BearerToken
    )
    $body = @{ state = $State }
    if ($ErrorMessage) { $body.error = $ErrorMessage }
    $r = Invoke-OsdRequest -Config $Config -Path "/osd/client/step/$StepId/result" `
        -Method POST -Body $body -BearerToken $BearerToken
    if ($r.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $r.bearer_token) {
        return [string] $r.bearer_token
    }
    return $BearerToken
}

function Send-ContentStageState {
    param(
        [object] $Config,
        [Parameter(Mandatory)] [object] $Item,
        [Parameter(Mandatory)] [ValidateSet('staging','staged','failed')] [string] $State,
        [string] $Phase = 'full_os',
        [string] $StagingPath,
        [string] $ErrorMessage,
        [string] $BearerToken
    )
    if (-not $Config) { return }
    $manifestId = ''
    if ($Item.PSObject.Properties.Match('id').Count -gt 0) {
        $manifestId = [string] $Item.id
    }
    if ([string]::IsNullOrWhiteSpace($manifestId)) { return }

    $agentId = 'osd-client'
    if ($Config.PSObject.Properties.Match('agent_id').Count -gt 0 -and $Config.agent_id) {
        $agentId = [string] $Config.agent_id
    } elseif (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) {
        $agentId = [string] $env:COMPUTERNAME
    }

    $body = @{
        run_id = [string] $Config.run_id
        agent_id = $agentId
        phase = $Phase
        status = $State
    }
    if (-not [string]::IsNullOrWhiteSpace($StagingPath)) {
        $body.staging_path = $StagingPath
    }
    if (-not [string]::IsNullOrWhiteSpace($ErrorMessage)) {
        $body.error = $ErrorMessage
    }

    Invoke-OsdRequest -Config $Config `
        -Path "/osd/v2/agent/content/$manifestId/stage" `
        -Method POST -Body $body -BearerToken $BearerToken | Out-Null
}

function Invoke-InstallQga {
    param([switch] $Required)

    if (Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue) {
        Invoke-VerifyQga
        Write-OsdLog 'QEMU Guest Agent was already installed and verified.'
        return
    }

    $msi = $null
    foreach ($drive in @('D','E','F','G','H','I')) {
        $candidate = "$($drive):\guest-agent\qemu-ga-x86_64.msi"
        if (Test-Path -LiteralPath $candidate) {
            $msi = $candidate
            break
        }
    }
    if (-not $msi) {
        if ($Required) {
            throw 'QEMU Guest Agent MSI not found on attached media.'
        }
        Write-OsdLog 'QEMU Guest Agent MSI not found on attached media; skipping install.'
        return
    }
    $log = Join-Path $OsdRoot 'qemu-ga-install.log'
    Write-OsdLog "Installing QEMU Guest Agent from $msi"
    $proc = Start-Process -FilePath msiexec.exe `
        -ArgumentList @('/i', $msi, '/qn', '/norestart', '/L*v', $log) `
        -Wait -PassThru
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
        throw "QEMU Guest Agent installer failed with exit $($proc.ExitCode)"
    }
    Invoke-VerifyQga
    Write-OsdLog 'QEMU Guest Agent install/start command completed.'
}

function Invoke-VerifyQga {
    Write-OsdLog 'Verifying QEMU Guest Agent service before OOBE handoff.'

    $svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    if (-not $svc) {
        throw 'QEMU Guest Agent service is not registered'
    }

    $serviceInfo = Get-CimInstance -ClassName Win32_Service `
        -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
    if ($serviceInfo) {
        Write-OsdLog (
            "QEMU Guest Agent service state=$($serviceInfo.State) " +
            "start_mode=$($serviceInfo.StartMode) path=$($serviceInfo.PathName)"
        )
    }

    Set-QgaServiceCommandLine

    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -eq 'Running') {
        Restart-Service -Name QEMU-GA -Force -ErrorAction Stop
    } else {
        Start-Service -Name QEMU-GA
    }
    (Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))

    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        throw "QEMU Guest Agent service did not reach Running state; status=$($svc.Status)"
    }
    Write-OsdLog "QEMU Guest Agent verified running; status=$($svc.Status)"
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
    $stateDir = Join-Path $env:ProgramData 'qemu-ga'
    $logFile = Join-Path $stateDir 'qemu-ga.log'
    if (-not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }

    $exePath = Get-QgaExecutablePath
    $binPath = Get-QgaServiceCommandLine -ExePath $exePath -StateDir $stateDir -LogFile $logFile

    $svcInfo = Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction Stop
    $changeResult = Invoke-CimMethod -InputObject $svcInfo -MethodName Change -Arguments @{
        PathName = $binPath
    }
    if ($changeResult.ReturnValue -ne 0) {
        throw "QEMU Guest Agent service command-line configuration failed with Win32_Service.Change return $($changeResult.ReturnValue)"
    }

    & sc.exe config QEMU-GA start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QEMU Guest Agent delayed-auto configuration failed with exit $LASTEXITCODE"
    }

    Write-OsdLog "QEMU Guest Agent service command line configured: $binPath"
}

function Get-OsdActionIntParam {
    param(
        [object] $Action,
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [int] $Default
    )

    if (-not $Action -or $Action.PSObject.Properties.Match('params').Count -eq 0 -or -not $Action.params) {
        return $Default
    }
    if ($Action.params.PSObject.Properties.Match($Name).Count -eq 0 -or $null -eq $Action.params.$Name) {
        return $Default
    }
    try {
        return [int] $Action.params.$Name
    } catch {
        throw "OSD action parameter $Name must be an integer"
    }
}

function Get-QgaWatchdogScriptContent {
    param([Parameter(Mandatory)] [int] $RestartIntervalMinutes)

    return @"
`$ErrorActionPreference = 'SilentlyContinue'
`$root = Join-Path `$env:ProgramData 'ProxmoxVEAutopilot\OSD'
`$logPath = Join-Path `$root 'qga-watchdog.log'
`$statePath = Join-Path `$root 'qga-watchdog-last-restart.txt'
`$qgaStateDir = Join-Path `$env:ProgramData 'qemu-ga'
`$qgaLogPath = Join-Path `$qgaStateDir 'qemu-ga.log'
`$restartIntervalMinutes = $RestartIntervalMinutes

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
            Add-WatchdogLog 'Corrected QEMU-GA service command line.'
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

function Set-QgaServiceRecovery {
    & sc.exe failure QEMU-GA reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QEMU Guest Agent service recovery configuration failed with exit $LASTEXITCODE"
    }
    & sc.exe failureflag QEMU-GA 1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QEMU Guest Agent service failure flag configuration failed with exit $LASTEXITCODE"
    }
}

function Register-QgaWatchdogTask {
    param(
        [Parameter(Mandatory)] [string] $ScriptPath,
        [Parameter(Mandatory)] [int] $TaskIntervalMinutes
    )

    $taskRun = "powershell.exe -ExecutionPolicy Bypass -NoProfile -File `"$ScriptPath`""
    $args = @(
        '/Create',
        '/TN', '\ProxmoxVEAutopilot\QgaWatchdog',
        '/SC', 'MINUTE',
        '/MO', [string] $TaskIntervalMinutes,
        '/RU', 'SYSTEM',
        '/RL', 'HIGHEST',
        '/F',
        '/TR', $taskRun
    )
    & schtasks.exe @args | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QGA watchdog scheduled task registration failed with exit $LASTEXITCODE"
    }
}

function Invoke-InstallQgaWatchdog {
    param([object] $Action)

    $taskIntervalMinutes = Get-OsdActionIntParam -Action $Action `
        -Name 'task_interval_minutes' -Default 5
    $restartIntervalMinutes = Get-OsdActionIntParam -Action $Action `
        -Name 'restart_interval_minutes' -Default 10

    if ($taskIntervalMinutes -lt 1) {
        throw 'task_interval_minutes must be at least 1'
    }
    if ($restartIntervalMinutes -lt 0) {
        throw 'restart_interval_minutes must be 0 or greater'
    }

    Invoke-VerifyQga

    $watchdogPath = Join-Path $OsdRoot 'QgaWatchdog.ps1'
    $statePath = Join-Path $OsdRoot 'qga-watchdog-last-restart.txt'
    $watchdog = Get-QgaWatchdogScriptContent -RestartIntervalMinutes $restartIntervalMinutes
    Set-Content -LiteralPath $watchdogPath -Value $watchdog -Encoding UTF8
    Set-Content -LiteralPath $statePath -Value (Get-Date -Format o) -Encoding ASCII

    Set-QgaServiceRecovery
    Register-QgaWatchdogTask -ScriptPath $watchdogPath `
        -TaskIntervalMinutes $taskIntervalMinutes

    Write-OsdLog (
        "QGA watchdog installed task_interval_minutes=$taskIntervalMinutes " +
        "restart_interval_minutes=$restartIntervalMinutes path=$watchdogPath"
    )
}

function Invoke-RecoveryFix {
    $script = Join-Path $OsdRoot 'FixRecoveryPartition.ps1'
    if (-not (Test-Path -LiteralPath $script)) {
        Write-OsdLog "Recovery fix script missing: $script"
        return
    }
    Write-OsdLog "Running recovery partition fix: $script"
    & powershell.exe -ExecutionPolicy Bypass -NoProfile -File $script
    if ($LASTEXITCODE -ne 0) {
        Write-OsdLog "Recovery fix exited with $LASTEXITCODE; preserving SetupComplete non-blocking behavior."
    }
}

function Invoke-InstallPackage {
    param(
        [Parameter(Mandatory)] [object] $Action,
        [object] $Config,
        [string] $BearerToken
    )

    $content = @($Action.content)
    if ($content.Count -ne 1) {
        throw "install_package requires exactly one content item; count=$($content.Count)"
    }
    $item = $content[0]
    $sourceUri = [string] $item.source_uri
    $expectedSha = ([string] $item.sha256).ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($sourceUri)) {
        throw 'install_package content item missing source_uri'
    }
    if ([string]::IsNullOrWhiteSpace($expectedSha)) {
        throw 'install_package content item missing sha256'
    }

    $stageDir = [string] $item.staging_path
    if ([string]::IsNullOrWhiteSpace($stageDir)) {
        $safeName = ([string] $item.logical_name) -replace '[^\w.-]', '_'
        $stageDir = Join-Path $OsdRoot "Content\$safeName"
    }
    New-DirectoryIfMissing -Path $stageDir
    $phase = 'full_os'
    if ($Action.PSObject.Properties.Match('phase').Count -gt 0 -and $Action.phase) {
        $phase = [string] $Action.phase
    }

    $fileName = [System.IO.Path]::GetFileName(([Uri] $sourceUri).AbsolutePath)
    if ([string]::IsNullOrWhiteSpace($fileName)) {
        $fileName = 'package.bin'
    }
    $packagePath = Join-Path $stageDir $fileName
    try {
        Send-ContentStageState -Config $Config -Item $item -State staging `
            -Phase $phase -StagingPath $stageDir -BearerToken $BearerToken
        Write-OsdLog "Downloading package content source=$sourceUri target=$packagePath"
        Invoke-WebRequest -Uri $sourceUri -OutFile $packagePath -UseBasicParsing -TimeoutSec 300

        $actualSha = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualSha -ne $expectedSha) {
            throw "install_package SHA256 mismatch expected=$expectedSha actual=$actualSha path=$packagePath"
        }
        Send-ContentStageState -Config $Config -Item $item -State staged `
            -Phase $phase -StagingPath $stageDir -BearerToken $BearerToken
    } catch {
        Send-ContentStageState -Config $Config -Item $item -State failed `
            -Phase $phase -StagingPath $stageDir `
            -ErrorMessage $_.Exception.Message -BearerToken $BearerToken
        throw
    }

    $installCommand = ''
    if ($Action.PSObject.Properties.Match('params').Count -gt 0 -and
        $Action.params.PSObject.Properties.Match('install_command').Count -gt 0) {
        $installCommand = [string] $Action.params.install_command
    }
    if ([string]::IsNullOrWhiteSpace($installCommand)) {
        if ($packagePath.EndsWith('.msi', [System.StringComparison]::OrdinalIgnoreCase)) {
            $installLog = Join-Path $stageDir 'install.log'
            $installCommand = "msiexec.exe /i `"$packagePath`" /qn /norestart /L*v `"$installLog`""
        } else {
            throw 'install_package requires params.install_command for non-MSI content'
        }
    } else {
        $installCommand = $installCommand.Replace('{path}', $packagePath)
    }

    Write-OsdLog "Running package install command: $installCommand"
    $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', $installCommand) `
        -Wait -PassThru
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
        throw "install_package command failed with exit $($proc.ExitCode)"
    }
    Write-OsdLog "Package install completed exit=$($proc.ExitCode)"
}

function Invoke-CaptureAutopilotHash {
    param(
        [Parameter(Mandatory)] [object] $Config,
        [Parameter(Mandatory)] [string] $BearerToken
    )

    $autopilotInfoScript = Join-Path $OsdRoot 'Get-WindowsAutopilotInfo.ps1'
    if (-not (Test-Path -LiteralPath $autopilotInfoScript)) {
        throw "Get-WindowsAutopilotInfo.ps1 not found: $autopilotInfoScript"
    }

    $hashDir = Join-Path $OsdRoot 'HardwareHashes'
    New-DirectoryIfMissing -Path $hashDir

    $serial = ''
    try {
        $bios = Get-CimInstance -ClassName Win32_BIOS -ErrorAction Stop
        $serial = [string] $bios.SerialNumber
    } catch {
        Write-OsdLog "Unable to read BIOS serial before hash capture: $($_.Exception.Message)"
    }
    if ([string]::IsNullOrWhiteSpace($serial) -or $serial -eq 'None') {
        try {
            $csprod = Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop
            $serial = [string] $csprod.UUID
        } catch {
            Write-OsdLog "Unable to read system UUID before hash capture: $($_.Exception.Message)"
        }
    }
    if ([string]::IsNullOrWhiteSpace($serial)) {
        $serial = $env:COMPUTERNAME
    }

    $safeSerial = $serial -replace '[^\w.-]', '_'
    $csvPath = Join-Path $hashDir "${safeSerial}_hwid.csv"
    if (Test-Path -LiteralPath $csvPath) {
        Remove-Item -LiteralPath $csvPath -Force
    }

    Write-OsdLog "Capturing Autopilot hardware hash to $csvPath"
    & powershell.exe -ExecutionPolicy Bypass -NoProfile `
        -File $autopilotInfoScript -OutputFile $csvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Get-WindowsAutopilotInfo.ps1 failed with exit $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $csvPath)) {
        throw "Autopilot hardware hash CSV was not created at $csvPath"
    }

    $rows = @(Import-Csv -LiteralPath $csvPath)
    if ($rows.Count -lt 1) {
        throw "Autopilot hardware hash CSV is empty: $csvPath"
    }
    $row = $rows[0]
    $capturedSerial = [string] $row.'Device Serial Number'
    $productId = [string] $row.'Windows Product ID'
    $hardwareHash = [string] $row.'Hardware Hash'
    if ([string]::IsNullOrWhiteSpace($capturedSerial)) {
        $capturedSerial = $serial
    }
    if ([string]::IsNullOrWhiteSpace($hardwareHash)) {
        throw "Autopilot hardware hash CSV missing Hardware Hash column: $csvPath"
    }

    $hashUploadPath = '/osd/client/hash'
    if (
        ($Config.PSObject.Properties.Match('engine').Count -gt 0 -and [string] $Config.engine -eq 'v2') -or
        ($Config.PSObject.Properties.Match('api_version').Count -gt 0 -and [string] $Config.api_version -eq '2')
    ) {
        $hashUploadPath = '/osd/v2/agent/hash'
    }

    Invoke-OsdRequest -Config $Config -Path $hashUploadPath -Method POST `
        -BearerToken $BearerToken `
        -Body @{
            serial_number = $capturedSerial
            product_id = $productId
            hardware_hash = $hardwareHash
        } | Out-Null
    Write-OsdLog "Autopilot hardware hash uploaded for serial=$capturedSerial path=$csvPath"
}

function Invoke-HandoffToOobe {
    Invoke-VerifyQga
    Write-OsdLog 'Pre-OOBE gate passed; handing off to OOBE.'
}

function Get-OsdAgentId {
    param([Parameter(Mandatory)] [object] $Config)

    if ($Config.PSObject.Properties.Match('agent_id').Count -gt 0 -and $Config.agent_id) {
        return [string] $Config.agent_id
    }
    if (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) {
        return [string] $env:COMPUTERNAME
    }
    return 'osd-client'
}

function Get-OsdPhase {
    param([Parameter(Mandatory)] [object] $Config)

    if ($Config.PSObject.Properties.Match('phase').Count -gt 0 -and $Config.phase) {
        return [string] $Config.phase
    }
    return 'full_os'
}

function Get-OsdeployAgentEndpoint {
    param(
        [Parameter(Mandatory)] [object] $Config,
        [Parameter(Mandatory)] [object] $AgentConfig,
        [Parameter(Mandatory)] [string] $PropertyName,
        [Parameter(Mandatory)] [string] $DefaultPath
    )

    if ($AgentConfig.PSObject.Properties.Match($PropertyName).Count -gt 0 -and $AgentConfig.$PropertyName) {
        return [string] $AgentConfig.$PropertyName
    }
    if ($Config.PSObject.Properties.Match($PropertyName).Count -gt 0 -and $Config.$PropertyName) {
        return [string] $Config.$PropertyName
    }
    return $Config.flask_base_url.TrimEnd('/') + $DefaultPath
}

function Invoke-OsdeployAgentRequest {
    param(
        [Parameter(Mandatory)] [string] $Uri,
        [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string] $Method,
        [hashtable] $Body,
        [string] $BearerToken,
        [int] $MaxAttempts = 8
    )

    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    $payload = $null
    if ($Body) { $payload = $Body | ConvertTo-Json -Depth 8 -Compress }
    $lastErr = $null
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $headers `
                -Body $payload -ContentType 'application/json' -TimeoutSec 30
        } catch {
            $lastErr = $_
            Write-OsdLog "agent request failed attempt=$i uri=$Uri error=$($_.Exception.Message)"
            Start-Sleep -Seconds ([Math]::Min(20, 2 * $i))
        }
    }
    throw $lastErr
}

function Get-OsdeployQgaState {
    $svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    if (-not $svc) { return 'missing' }
    return ([string] $svc.Status).ToLowerInvariant()
}

function Get-OsdeployOsName {
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        if ($os.Caption) { return [string] $os.Caption }
    } catch {
        Write-OsdLog "Unable to read OS name for OSDeploy heartbeat: $($_.Exception.Message)"
    }
    return ''
}

function Invoke-OsdeployAgentBootstrapHeartbeat {
    param([Parameter(Mandatory)] [object] $Config)

    if ($Config.PSObject.Properties.Match('osdeploy_agent').Count -eq 0 -or -not $Config.osdeploy_agent) {
        return
    }

    $agentConfig = $Config.osdeploy_agent
    $bootstrapToken = [string] $agentConfig.bootstrap_token
    if ([string]::IsNullOrWhiteSpace($bootstrapToken)) {
        throw 'OSDeploy agent bootstrap config is missing bootstrap_token'
    }

    $agentId = Get-OsdAgentId -Config $Config
    $bootstrapUrl = Get-OsdeployAgentEndpoint `
        -Config $Config `
        -AgentConfig $agentConfig `
        -PropertyName 'bootstrap_url' `
        -DefaultPath '/api/agent/v1/bootstrap'
    $heartbeatUrl = Get-OsdeployAgentEndpoint `
        -Config $Config `
        -AgentConfig $agentConfig `
        -PropertyName 'agent_heartbeat_url' `
        -DefaultPath '/api/agent/v1/heartbeat'

    Write-OsdLog "Bootstrapping OSDeploy full-OS heartbeat agent_id=$agentId"
    $bootstrapBody = @{
        agent_id = $agentId
        run_id = [string] $Config.run_id
        phase = 'osdeploy'
        computer_name = $env:COMPUTERNAME
        agent_version = 'osd-client-0.1.0'
    }
    if ($agentConfig.PSObject.Properties.Match('vmid').Count -gt 0 -and $agentConfig.vmid) {
        $bootstrapBody.vmid = [int] $agentConfig.vmid
    }

    $bootstrap = Invoke-OsdeployAgentRequest `
        -Uri $bootstrapUrl `
        -Method POST `
        -BearerToken $bootstrapToken `
        -Body $bootstrapBody
    if (-not $bootstrap.agent_token) {
        throw 'OSDeploy agent bootstrap did not return agent_token'
    }
    if ($bootstrap.agent_id) {
        $agentId = [string] $bootstrap.agent_id
    }

    $heartbeatBody = @{
        agent_id = $agentId
        computer_name = $env:COMPUTERNAME
        os_name = Get-OsdeployOsName
        qga_service_name = 'QEMU-GA'
        qga_state = Get-OsdeployQgaState
        current_run_id = [string] $Config.run_id
        current_phase = 'full_os'
        current_step_id = 'osdeploy_full_os_heartbeat'
        agent_version = 'osd-client-0.1.0'
    }
    if ($agentConfig.PSObject.Properties.Match('vmid').Count -gt 0 -and $agentConfig.vmid) {
        $heartbeatBody.vmid = [int] $agentConfig.vmid
    }
    Invoke-OsdeployAgentRequest `
        -Uri $heartbeatUrl `
        -Method POST `
        -BearerToken ([string] $bootstrap.agent_token) `
        -Body $heartbeatBody | Out-Null
    Write-OsdLog "osdeploy_full_os_heartbeat posted agent_id=$agentId"
}

function Invoke-OsdeployFirstBootReadiness {
    param([Parameter(Mandatory)] [object] $Config)

    if ($Config.PSObject.Properties.Match('osdeploy_agent').Count -eq 0 -or -not $Config.osdeploy_agent) {
        return
    }

    Invoke-InstallQga -Required
}

function Invoke-OsdAction {
    param(
        [Parameter(Mandatory)] [object] $Action,
        [object] $Config,
        [string] $BearerToken
    )

    $kind = [string] $Action.kind
    switch ($kind) {
        'install_qga' { Invoke-InstallQga }
        'fix_recovery_partition' { Invoke-RecoveryFix }
        'verify_qga' { Invoke-VerifyQga }
        'install_qga_watchdog' { Invoke-InstallQgaWatchdog -Action $Action }
        'capture_autopilot_hash' {
            Invoke-CaptureAutopilotHash -Config $Config -BearerToken $BearerToken
        }
        'wait_agent_heartbeat' {
            Write-OsdLog 'wait_agent_heartbeat is satisfied by the CloudOSD first-boot heartbeat gate.'
        }
        'handoff_to_oobe' { Invoke-HandoffToOobe }
        'install_package' {
            Invoke-InstallPackage -Action $Action -Config $Config -BearerToken $BearerToken
        }
        default { throw "unknown OSD step kind: $kind" }
    }
}

function Send-V2StepResult {
    param(
        [Parameter(Mandatory)] [object] $Config,
        [Parameter(Mandatory)] [object] $Action,
        [Parameter(Mandatory)] [ValidateSet('success','failed','skipped','reboot_required')] [string] $Status,
        [string] $Message,
        [string] $BearerToken
    )

    $phase = [string] $Action.phase
    if ([string]::IsNullOrWhiteSpace($phase)) {
        $phase = Get-OsdPhase -Config $Config
    }
    $body = @{
        run_id = [string] $Config.run_id
        agent_id = Get-OsdAgentId -Config $Config
        phase = $phase
        status = $Status
    }
    if (-not [string]::IsNullOrWhiteSpace($Message)) {
        $body.message = $Message
    }

    $r = Invoke-OsdRequest -Config $Config `
        -Path "/osd/v2/agent/step/$($Action.step_id)/result" `
        -Method POST -Body $body -BearerToken $BearerToken
    if ($r.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $r.bearer_token) {
        return [string] $r.bearer_token
    }
    return $BearerToken
}

function Invoke-OsdV2Client {
    param([Parameter(Mandatory)] [object] $Config)

    $agentId = Get-OsdAgentId -Config $Config
    $phase = Get-OsdPhase -Config $Config
    $token = [string] $Config.bearer_token

    $reg = Invoke-OsdRequest -Config $Config -Path '/osd/v2/agent/register' `
        -Method POST -BearerToken $token `
        -Body @{
            run_id = [string] $Config.run_id
            agent_id = $agentId
            phase = $phase
            computer_name = $env:COMPUTERNAME
            capabilities = @('content', 'packages', 'hash_capture')
        }
    if ($reg.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $reg.bearer_token) {
        $token = [string] $reg.bearer_token
    }

    while ($true) {
        $next = Invoke-OsdRequest -Config $Config -Path '/osd/v2/agent/next' `
            -Method POST -BearerToken $token `
            -Body @{
                run_id = [string] $Config.run_id
                agent_id = $agentId
                phase = $phase
                batch_size = 1
            }
        if ($next.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $next.bearer_token) {
            $token = [string] $next.bearer_token
        }

        $actions = @($next.actions)
        if ($actions.Count -eq 0) { break }

        foreach ($action in $actions) {
            $kind = [string] $action.kind
            Write-OsdLog "OSD v2 step starting id=$($action.step_id) kind=$kind"
            try {
                Invoke-OsdAction -Action $action -Config $Config -BearerToken $token
                $token = Send-V2StepResult -Config $Config -Action $action `
                    -Status success -Message 'ok' -BearerToken $token
                Write-OsdLog "OSD v2 step completed id=$($action.step_id) kind=$kind"
            } catch {
                $token = Send-V2StepResult -Config $Config -Action $action `
                    -Status failed -Message $_.Exception.Message -BearerToken $token
                throw
            }
        }
    }

    Invoke-OsdRequest -Config $Config -Path '/osd/v2/agent/phase-complete' `
        -Method POST -BearerToken $token `
        -Body @{
            run_id = [string] $Config.run_id
            agent_id = $agentId
            phase = $phase
        } | Out-Null
}

if ($env:AUTOPILOT_OSD_CLIENT_LIBRARY_ONLY -eq '1') {
    return
}

try {
    $cfg = Read-OsdConfig
    Write-OsdLog "OSD client starting run_id=$($cfg.run_id)"
    $engine = ''
    if ($cfg.PSObject.Properties.Match('engine').Count -gt 0 -and $cfg.engine) {
        $engine = [string] $cfg.engine
    }
    if ($cfg.PSObject.Properties.Match('api_version').Count -gt 0 -and [string] $cfg.api_version -eq '2') {
        $engine = 'v2'
    }
    if ($engine -eq 'v2') {
        Invoke-OsdV2Client -Config $cfg
        Invoke-OsdeployFirstBootReadiness -Config $cfg
        Invoke-OsdeployAgentBootstrapHeartbeat -Config $cfg
        Write-OsdLog 'OSD v2 client completed.'
        exit 0
    }

    $token = [string] $cfg.bearer_token
    $reg = Invoke-OsdRequest -Config $cfg -Path '/osd/client/register' -Method POST `
        -BearerToken $token `
        -Body @{
            computer_name = $env:COMPUTERNAME
            setupcomplete_log_tail = (Get-LogTail -Path $SetupLog)
        }
    if ($reg.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $reg.bearer_token) {
        $token = [string] $reg.bearer_token
    }

    foreach ($action in @($reg.actions)) {
        $stepId = [int] $action.step_id
        $kind = [string] $action.kind
        Write-OsdLog "OSD step starting id=$stepId kind=$kind"
        $token = Send-StepState -Config $cfg -StepId $stepId -State running -BearerToken $token
        try {
            Invoke-OsdAction -Action $action -Config $cfg -BearerToken $token
            $token = Send-StepState -Config $cfg -StepId $stepId -State ok -BearerToken $token
            Write-OsdLog "OSD step completed id=$stepId kind=$kind"
        } catch {
            $token = Send-StepState -Config $cfg -StepId $stepId -State error `
                -ErrorMessage $_.Exception.Message -BearerToken $token
            throw
        }
    }

    Invoke-OsdRequest -Config $cfg -Path '/osd/client/complete' -Method POST `
        -Body @{} -BearerToken $token | Out-Null
    Write-OsdLog 'OSD client completed.'
    exit 0
} catch {
    Write-OsdLog "OSD client failed: $($_.Exception.Message)"
    Write-OsdLog $_.ScriptStackTrace
    exit 1
}
