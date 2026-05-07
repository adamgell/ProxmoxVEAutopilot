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
    & sc.exe config QEMU-GA start= auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "QEMU Guest Agent service config failed with exit $LASTEXITCODE"
    }
    $svc = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    if (-not $svc) {
        throw "QEMU Guest Agent service was not registered after install"
    }
    if ($svc.Status -ne 'Running') {
        Start-Service -Name QEMU-GA
        $svc.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    }
    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        throw "QEMU Guest Agent service did not reach Running state; status=$($svc.Status)"
    }
    Write-OsdLog 'QEMU Guest Agent install/start command completed.'
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
    param([Parameter(Mandatory)] [object] $Action)

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

    $fileName = [System.IO.Path]::GetFileName(([Uri] $sourceUri).AbsolutePath)
    if ([string]::IsNullOrWhiteSpace($fileName)) {
        $fileName = 'package.bin'
    }
    $packagePath = Join-Path $stageDir $fileName
    Write-OsdLog "Downloading package content source=$sourceUri target=$packagePath"
    Invoke-WebRequest -Uri $sourceUri -OutFile $packagePath -UseBasicParsing -TimeoutSec 300

    $actualSha = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha -ne $expectedSha) {
        throw "install_package SHA256 mismatch expected=$expectedSha actual=$actualSha path=$packagePath"
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

try {
    $cfg = Read-OsdConfig
    Write-OsdLog "OSD client starting run_id=$($cfg.run_id)"
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
            switch ($kind) {
                'install_qga' { Invoke-InstallQga }
                'fix_recovery_partition' { Invoke-RecoveryFix }
                'install_package' { Invoke-InstallPackage -Action $action }
                default { throw "unknown OSD step kind: $kind" }
            }
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
