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
    $msi = $null
    foreach ($drive in @('D','E','F','G','H','I')) {
        $candidate = "$($drive):\guest-agent\qemu-ga-x86_64.msi"
        if (Test-Path -LiteralPath $candidate) {
            $msi = $candidate
            break
        }
    }
    if (-not $msi) {
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

    & sc.exe config QEMU-GA start= auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QEMU Guest Agent service config failed with exit $LASTEXITCODE"
    }

    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        Start-Service -Name QEMU-GA
        $svc.WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
    }

    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        throw "QEMU Guest Agent service did not reach Running state; status=$($svc.Status)"
    }
    Write-OsdLog "QEMU Guest Agent verified running; status=$($svc.Status)"
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

    Invoke-OsdRequest -Config $Config -Path '/osd/client/hash' -Method POST `
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
        'capture_autopilot_hash' {
            Invoke-CaptureAutopilotHash -Config $Config -BearerToken $BearerToken
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
