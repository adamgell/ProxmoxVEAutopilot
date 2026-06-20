# First-boot continuation for CloudOSD deployments.
#
# Runs as SYSTEM from a scheduled task created by SetupComplete. It installs
# the persistent AutopilotAgent MSI, runs the existing postinstall bootstrap
# script with the CloudOSD run token, confirms heartbeat, then leaves v2
# full-OS deployment work to the persistent AutopilotAgent service.

$ErrorActionPreference = 'Stop'

function Write-CloudOSDFirstBootLog {
    param([Parameter(Mandatory)] [string] $Message)
    $root = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD'
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -LiteralPath (Join-Path $root 'firstboot.log') -Value $line -Encoding UTF8
    Write-Output $line
}

function Get-CloudOSDLogTail {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [int] $Lines = 80
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return @()
    }
    try {
        return @(Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction Stop)
    } catch {
        return @("failed to read ${Path}: $($_.Exception.Message)")
    }
}

function Get-CloudOSDFirstBootDiagnosticData {
    param([string] $ErrorMessage = '')
    $cloudRoot = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD'
    $agentInstallRoot = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\AutopilotAgent\install'
    $firstbootLog = Join-Path $cloudRoot 'firstboot.log'
    $postinstallLog = Join-Path $agentInstallRoot 'postinstall.log'
    $msiLog = Join-Path $cloudRoot 'AutopilotAgent-msi.log'
    $setupCompleteLog = Join-Path $env:SystemRoot 'Setup\Scripts\SetupComplete.log'
    $agentService = $null
    $qgaService = $null
    if (Get-Command Get-Service -ErrorAction SilentlyContinue) {
        $agentService = Get-Service -Name AutopilotAgent -ErrorAction SilentlyContinue
        $qgaService = Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue
    }
    $agentServiceStatus = if ($agentService) { [string] $agentService.Status } else { 'missing' }
    $qgaServiceStatus = if ($qgaService) { [string] $qgaService.Status } else { 'missing' }
    return @{
        error = $ErrorMessage
        computer_name = $env:COMPUTERNAME
        firstboot_log_tail = Get-CloudOSDLogTail -Path $firstbootLog -Lines 80
        postinstall_log_tail = Get-CloudOSDLogTail -Path $postinstallLog -Lines 120
        agent_msi_log_tail = Get-CloudOSDLogTail -Path $msiLog -Lines 80
        setupcomplete_log_tail = Get-CloudOSDLogTail -Path $setupCompleteLog -Lines 80
        autopilot_agent_service_status = $agentServiceStatus
        qga_service_status = $qgaServiceStatus
    }
}

function Write-PVEAutopilotCloudOSDEvent {
    param(
        [Parameter(Mandatory)] [string] $ServerUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [string] $Phase,
        [Parameter(Mandatory)] [string] $EventType,
        [string] $Message,
        [string] $Severity = 'info',
        [hashtable] $Data = @{}
    )
    try {
        $uri = $ServerUrl.TrimEnd('/') + "/api/cloudosd/runs/$RunId/events"
        $body = @{
            phase = $Phase
            event_type = $EventType
            severity = $Severity
            message = $Message
            data = $Data
        } | ConvertTo-Json -Depth 20 -Compress
        Invoke-RestMethod -Uri $uri `
            -Method Post `
            -Headers @{ Authorization = "Bearer $BearerToken" } `
            -Body $body `
            -ContentType 'application/json' `
            -TimeoutSec 30 | Out-Null
    } catch {
        Write-CloudOSDFirstBootLog "failed to report CloudOSD event ${EventType}: $($_.Exception.Message)"
    }
}

function Get-CloudOSDNetworkSnapshot {
    try {
        $rows = @()
        $adapters = Get-NetAdapter -ErrorAction SilentlyContinue
        foreach ($adapter in $adapters) {
            $ips = Get-NetIPAddress `
                -InterfaceIndex $adapter.ifIndex `
                -AddressFamily IPv4 `
                -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress } |
                Select-Object -ExpandProperty IPAddress
            $ipText = $ips -join ','
            if (-not $ipText) { $ipText = '-' }
            $rows += ('{0} status={1} mac={2} ipv4={3}' -f `
                $adapter.Name,
                $adapter.Status,
                $adapter.MacAddress,
                $ipText)
        }
        if ($rows.Count -gt 0) { return ($rows -join '; ') }
        return 'no network adapters reported'
    } catch {
        return "network snapshot unavailable: $($_.Exception.Message)"
    }
}

function Read-PVEAutopilotCloudOSDRunConfig {
    param(
        [string] $Path = (Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\cloudosd-run.json')
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "CloudOSD run config not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Test-PVEAutopilotCloudOSDDomainJoinEnabled {
    param([AllowNull()] [object] $RunConfig)
    if ($null -eq $RunConfig) { return $false }
    if ($RunConfig.PSObject.Properties.Match('domain_join').Count -eq 0 -or
        -not $RunConfig.domain_join) {
        return $false
    }
    if ($RunConfig.domain_join.PSObject.Properties.Match('enabled').Count -eq 0) {
        return $false
    }
    return [bool] $RunConfig.domain_join.enabled
}

function Get-PVEAutopilotCloudOSDFirstBootStatePath {
    return Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\firstboot-state.json'
}

function Read-PVEAutopilotCloudOSDFirstBootState {
    param([Parameter(Mandatory)] [string] $RunId)
    $state = @{
        run_id = $RunId
        postinstall_failures = 0
        first_postinstall_failure_utc = ''
        last_postinstall_failure_utc = ''
        last_postinstall_error = ''
    }
    $path = Get-PVEAutopilotCloudOSDFirstBootStatePath
    if (-not (Test-Path -LiteralPath $path)) {
        return $state
    }
    try {
        $raw = Get-Content -LiteralPath $path -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $state
        }
        $loaded = $raw | ConvertFrom-Json
        if ([string] $loaded.run_id -ne $RunId) {
            return $state
        }
        foreach ($key in @(
            'postinstall_failures',
            'first_postinstall_failure_utc',
            'last_postinstall_failure_utc',
            'last_postinstall_error'
        )) {
            if ($loaded.PSObject.Properties.Match($key).Count -gt 0) {
                $state[$key] = $loaded.$key
            }
        }
    } catch {
        Write-CloudOSDFirstBootLog "Ignoring unreadable CloudOSD first boot retry state: $($_.Exception.Message)"
    }
    return $state
}

function Save-PVEAutopilotCloudOSDFirstBootState {
    param([Parameter(Mandatory)] [hashtable] $State)
    $path = Get-PVEAutopilotCloudOSDFirstBootStatePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $path -Encoding UTF8
}

function Clear-PVEAutopilotCloudOSDFirstBootState {
    param([Parameter(Mandatory)] [string] $RunId)
    $path = Get-PVEAutopilotCloudOSDFirstBootStatePath
    if (-not (Test-Path -LiteralPath $path)) {
        return
    }
    try {
        $state = Read-PVEAutopilotCloudOSDFirstBootState -RunId $RunId
        if ([string] $state['run_id'] -eq $RunId) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Write-CloudOSDFirstBootLog "Unable to clear CloudOSD first boot retry state: $($_.Exception.Message)"
    }
}

function Add-PVEAutopilotCloudOSDPostinstallFailure {
    param(
        [Parameter(Mandatory)] [string] $RunId,
        [string] $ErrorMessage = '',
        [scriptblock] $NowProvider = { Get-Date }
    )
    $state = Read-PVEAutopilotCloudOSDFirstBootState -RunId $RunId
    $count = 0
    try { $count = [int] $state['postinstall_failures'] } catch { $count = 0 }
    $now = & $NowProvider
    if ($now -isnot [datetime]) {
        $now = Get-Date
    }
    $nowUtc = ([datetime] $now).ToUniversalTime().ToString('o')
    $state['postinstall_failures'] = $count + 1
    if ([string]::IsNullOrWhiteSpace([string] $state['first_postinstall_failure_utc'])) {
        $state['first_postinstall_failure_utc'] = $nowUtc
    }
    $state['last_postinstall_failure_utc'] = $nowUtc
    $state['last_postinstall_error'] = $ErrorMessage
    Save-PVEAutopilotCloudOSDFirstBootState -State $state
    return $state
}

function Test-PVEAutopilotCloudOSDPostinstallRetryAllowed {
    param(
        [Parameter(Mandatory)] [hashtable] $State,
        [int] $MaxRetryableFailures = 12,
        [int] $WindowMinutes = 45,
        [scriptblock] $NowProvider = { Get-Date }
    )
    $failures = 0
    try { $failures = [int] $State['postinstall_failures'] } catch { $failures = 0 }
    if ($failures -le 0) {
        return $false
    }
    if ($failures -gt $MaxRetryableFailures) {
        return $false
    }
    $firstFailure = [string] $State['first_postinstall_failure_utc']
    if (-not [string]::IsNullOrWhiteSpace($firstFailure)) {
        try {
            $firstUtc = ([datetime]::Parse($firstFailure)).ToUniversalTime()
            $now = & $NowProvider
            if ($now -isnot [datetime]) {
                $now = Get-Date
            }
            $nowUtc = ([datetime] $now).ToUniversalTime()
            if ($nowUtc -gt $firstUtc.AddMinutes($WindowMinutes)) {
                return $false
            }
        } catch {
            Write-CloudOSDFirstBootLog "Unable to evaluate CloudOSD first boot retry window: $($_.Exception.Message)"
        }
    }
    return $true
}

function New-PVEAutopilotCloudOSDPostinstallRetryData {
    param(
        [Parameter(Mandatory)] [hashtable] $State,
        [string] $ErrorMessage = '',
        [string] $RecoveryErrorMessage = '',
        [int] $MaxRetryableFailures = 12,
        [int] $WindowMinutes = 45
    )
    return @{
        error = $ErrorMessage
        heartbeat_recovery_error = $RecoveryErrorMessage
        postinstall_failure_count = [int] $State['postinstall_failures']
        postinstall_retryable_failure_limit = $MaxRetryableFailures
        postinstall_retry_window_minutes = $WindowMinutes
        first_postinstall_failure_utc = [string] $State['first_postinstall_failure_utc']
        last_postinstall_failure_utc = [string] $State['last_postinstall_failure_utc']
    }
}

function Clear-PVEAutopilotDomainJoinSecrets {
    param(
        [string] $PantherRoot = (Join-Path $env:SystemRoot 'Panther')
    )
    if (-not (Test-Path -LiteralPath $PantherRoot)) {
        return 0
    }

    $redacted = 0
    $files = Get-ChildItem -LiteralPath $PantherRoot `
        -Recurse `
        -File `
        -Filter '*.xml' `
        -ErrorAction SilentlyContinue
    foreach ($file in @($files)) {
        try {
            if ($file.Name -notlike '*unattend*.xml') {
                continue
            }
            $xml = New-Object System.Xml.XmlDocument
            $xml.PreserveWhitespace = $true
            $xml.Load($file.FullName)
            $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
            $ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
            $passwordNodes = $xml.SelectNodes('//u:Password', $ns)
            if (-not $passwordNodes -or $passwordNodes.Count -eq 0) { continue }
            foreach ($node in @($passwordNodes)) {
                $valueNodes = $node.SelectNodes('u:Value', $ns)
                if ($valueNodes -and $valueNodes.Count -gt 0) {
                    foreach ($valueNode in @($valueNodes)) {
                        if ($valueNode.InnerText -and $valueNode.InnerText -ne 'REDACTED-BY-PVEAUTOPILOT') {
                            $valueNode.InnerText = 'REDACTED-BY-PVEAUTOPILOT'
                            $redacted++
                        }
                    }
                } elseif ($node.InnerText -and $node.InnerText -ne 'REDACTED-BY-PVEAUTOPILOT') {
                    $node.InnerText = 'REDACTED-BY-PVEAUTOPILOT'
                    $redacted++
                }
            }
            $writerSettings = New-Object System.Xml.XmlWriterSettings
            $writerSettings.Encoding = New-Object System.Text.UTF8Encoding($false)
            $writerSettings.Indent = $true
            $writer = [System.Xml.XmlWriter]::Create($file.FullName, $writerSettings)
            try {
                $xml.Save($writer)
            } finally {
                $writer.Close()
            }
        } catch {
            Write-CloudOSDFirstBootLog "skipping domain join secret cleanup for $($file.FullName): $($_.Exception.Message)"
            continue
        }
    }
    return $redacted
}

function Wait-CloudOSDNetwork {
    param(
        [int] $TimeoutSeconds = 600,
        [int] $DelaySeconds = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $nextLog = Get-Date
    while ((Get-Date) -lt $deadline) {
        if ((Get-Date) -ge $nextLog) {
            Write-CloudOSDFirstBootLog "Waiting for network: $(Get-CloudOSDNetworkSnapshot)"
            $nextLog = (Get-Date).AddSeconds(60)
        }
        $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike '169.254.*' -and
                $_.IPAddress -ne '127.0.0.1'
            } |
            Select-Object -First 1
        if ($ip) { return }
        Start-Sleep -Seconds $DelaySeconds
    }
    $snapshot = Get-CloudOSDNetworkSnapshot
    Write-CloudOSDFirstBootLog "Network wait timed out: $snapshot"
    throw "network was not ready before timeout; $snapshot"
}

function Wait-CloudOSDServer {
    param(
        [Parameter(Mandatory)] [string] $ServerUrl,
        [int] $TimeoutSeconds = 600,
        [int] $DelaySeconds = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthUrl = $ServerUrl.TrimEnd('/') + '/healthz'
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 15
            if ($response.StatusCode -lt 500) { return }
        } catch {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw "server health endpoint did not respond: $healthUrl"
}

function Get-PVEAutopilotQgaExecutablePath {
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

function Get-PVEAutopilotQgaServiceCommandLine {
    param(
        [Parameter(Mandatory)] [string] $ExePath,
        [Parameter(Mandatory)] [string] $StateDir,
        [Parameter(Mandatory)] [string] $LogFile
    )

    return ('"{0}" -d -m virtio-serial -p \\.\Global\org.qemu.guest_agent.0 --retry-path -t "{1}" -l "{2}"' -f $ExePath, $StateDir, $LogFile)
}

function Set-PVEAutopilotQgaServiceCommandLine {
    $stateDir = Join-Path $env:ProgramData 'qemu-ga'
    $logFile = Join-Path $stateDir 'qemu-ga.log'
    if (-not (Test-Path -LiteralPath $stateDir)) {
        New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    }

    $exePath = Get-PVEAutopilotQgaExecutablePath
    $binPath = Get-PVEAutopilotQgaServiceCommandLine `
        -ExePath $exePath `
        -StateDir $stateDir `
        -LogFile $logFile

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

    Write-CloudOSDFirstBootLog "QEMU Guest Agent service command line configured: $binPath"
    return $binPath
}

function Install-QemuGuestAgentIfPresent {
    $candidates = @(
        (Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\qemu-ga-x86_64.msi'),
        'D:\guest-agent\qemu-ga-x86_64.msi',
        'E:\guest-agent\qemu-ga-x86_64.msi',
        'F:\guest-agent\qemu-ga-x86_64.msi'
    )
    $msi = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $msi) {
        throw 'QEMU Guest Agent MSI not present; first-boot QGA setup cannot continue.'
    }
    Write-CloudOSDFirstBootLog "Installing QEMU Guest Agent from $msi"
    $log = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\qemu-ga-install.log'
    $p = Start-Process -FilePath msiexec.exe `
        -ArgumentList @('/i', $msi, '/qn', '/norestart', '/L*v', $log) `
        -Wait -PassThru
    if ($p.ExitCode -notin @(0, 3010, 1641)) {
        throw "QEMU Guest Agent MSI failed with exit code $($p.ExitCode)"
    }
    $binPath = Set-PVEAutopilotQgaServiceCommandLine
    Set-Service -Name QEMU-GA -StartupType Automatic -ErrorAction Stop
    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -eq 'Running') {
        Restart-Service -Name QEMU-GA -Force -ErrorAction Stop
    } else {
        Start-Service -Name QEMU-GA -ErrorAction Stop
    }
    (Get-Service -Name QEMU-GA -ErrorAction Stop).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
    $svc = Get-Service -Name QEMU-GA -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        throw "QEMU Guest Agent service did not reach Running state; status=$($svc.Status)"
    }
    Write-CloudOSDFirstBootLog 'QEMU Guest Agent service configured and started.'
    return [pscustomobject]@{
        installed = $true
        source = [string] $msi
        service = 'QEMU-GA'
        status = [string] $svc.Status
        command_line = [string] $binPath
    }
}

function Install-AutopilotAgentMsi {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "AutopilotAgent MSI not found: $Path"
    }
    $log = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\AutopilotAgent-msi.log'
    Write-CloudOSDFirstBootLog "Installing AutopilotAgent MSI from $Path"
    $p = Start-Process -FilePath msiexec.exe `
        -ArgumentList @('/i', $Path, '/qn', '/norestart', '/L*v', $log) `
        -Wait -PassThru
    if ($p.ExitCode -notin @(0, 3010, 1641)) {
        throw "AutopilotAgent MSI failed with exit code $($p.ExitCode)"
    }
    Write-CloudOSDFirstBootLog "AutopilotAgent MSI installed with exit code $($p.ExitCode)"
}

function Invoke-AutopilotAgentPostinstall {
    param(
        [Parameter(Mandatory)] [string] $ScriptPath,
        [Parameter(Mandatory)] [hashtable] $PostinstallArgs
    )
    if (-not (Test-Path -LiteralPath $ScriptPath)) {
        throw "AutopilotAgent postinstall script not found: $ScriptPath"
    }
    Write-CloudOSDFirstBootLog "Running AutopilotAgent postinstall from $ScriptPath"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath `
        -ServerUrl $PostinstallArgs.ServerUrl `
        -BootstrapToken $PostinstallArgs.BootstrapToken `
        -RunId $PostinstallArgs.RunId `
        -Vmid $PostinstallArgs.Vmid `
        -Phase $PostinstallArgs.Phase
    if ($LASTEXITCODE -ne 0) {
        throw "AutopilotAgent postinstall failed: $LASTEXITCODE"
    }
    Write-CloudOSDFirstBootLog 'AutopilotAgent postinstall completed.'
}

function Confirm-AutopilotAgentHeartbeat {
    param(
        [Parameter(Mandatory)] [string] $ConfigUrl,
        [Parameter(Mandatory)] [string] $Token,
        [int] $TimeoutSeconds = 120,
        [int] $DelaySeconds = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $result = Invoke-RestMethod -Uri $ConfigUrl `
                -Headers @{ Authorization = "Bearer $Token" } `
                -TimeoutSec 15
            if ($result.last_heartbeat_at) { return }
        } catch {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw 'AutopilotAgent heartbeat was not visible before timeout'
}

function Get-AutopilotAgentToken {
    param(
        [string] $ConfigPath = (Join-Path $env:ProgramData 'ProxmoxVEAutopilot\AutopilotAgent\agent.json')
    )
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "AutopilotAgent config not found: $ConfigPath"
    }
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    if (-not $config.agentToken) {
        throw "AutopilotAgent config does not contain agentToken: $ConfigPath"
    }
    return [string] $config.agentToken
}

function Remove-PVEAutopilotFirstBootTask {
    param([string] $Name = 'PVEAutopilot-CloudOSD-FirstBoot')
    schtasks.exe /Delete /F /TN $Name | Out-Null
}

function Get-CloudOSDLocalPayloadPath {
    param(
        [Parameter(Mandatory)] [object] $Payload,
        [Parameter(Mandatory)] [string] $Name
    )
    if ($Payload.PSObject.Properties.Match('local_path').Count -gt 0 -and $Payload.local_path) {
        return [string] $Payload.local_path
    }
    $candidate = Join-Path $env:ProgramData "ProxmoxVEAutopilot\CloudOSD\$Name"
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    throw "CloudOSD payload $Name is not staged"
}

function Invoke-CloudOSDOsdClient {
    $scriptPath = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\OSD\OsdClient.ps1'
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "OSD client is not staged: $scriptPath"
    }
    $stdoutLog = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\osd-client-firstboot.out.log'
    $stderrLog = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\osd-client-firstboot.err.log'
    Write-CloudOSDFirstBootLog "Starting CloudOSD OSD client from $scriptPath"
    $process = Start-Process -FilePath powershell.exe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath) `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        throw "CloudOSD OSD client failed with exit code $($process.ExitCode)"
    }
    Write-CloudOSDFirstBootLog 'CloudOSD OSD client completed.'
}

function Clear-PVEAutopilotOobeBootstrapAccount {
    param(
        [string] $UserName = 'PVEAutopilot',
        [scriptblock] $RegistryCleanup = {
            $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
            foreach ($name in @('AutoAdminLogon', 'DefaultUserName', 'DefaultPassword', 'DefaultDomainName')) {
                Remove-ItemProperty -Path $winlogon -Name $name -ErrorAction SilentlyContinue
            }
            $userList = Join-Path $winlogon 'SpecialAccounts\UserList'
            New-Item -Path $userList -Force -ErrorAction SilentlyContinue | Out-Null
            New-ItemProperty -Path $userList `
                -Name 'PVEAutopilot' `
                -Value 0 `
                -PropertyType DWord `
                -Force `
                -ErrorAction SilentlyContinue | Out-Null
        },
        [scriptblock] $DisableUser = {
            param([string] $Name)
            if (Get-Command Disable-LocalUser -ErrorAction SilentlyContinue) {
                Disable-LocalUser -Name $Name -ErrorAction SilentlyContinue
            } else {
                net.exe user $Name /active:no | Out-Null
            }
        }
    )
    & $RegistryCleanup
    & $DisableUser $UserName
}

function Invoke-PVEAutopilotBootstrapSessionLogoff {
    param(
        [string] $UserName = 'PVEAutopilot',
        [scriptblock] $QueryUser = { query.exe user 2>$null },
        [scriptblock] $LogoffSession = { param([int] $SessionId) logoff.exe $SessionId }
    )

    $loggedOff = 0
    $lines = @(& $QueryUser)
    foreach ($line in $lines) {
        $text = ([string] $line).Trim()
        if (-not $text -or $text -like 'USERNAME*') { continue }
        if ($text.StartsWith('>')) {
            $text = $text.Substring(1).Trim()
        }
        $parts = @($text -split '\s+' | Where-Object { $_ })
        if ($parts.Count -lt 2 -or $parts[0] -ine $UserName) { continue }

        $sessionId = $null
        foreach ($token in @($parts | Select-Object -Skip 1)) {
            $parsed = 0
            if ([int]::TryParse($token, [ref] $parsed)) {
                $sessionId = $parsed
                break
            }
        }
        if ($null -eq $sessionId) {
            Write-CloudOSDFirstBootLog "Unable to identify session ID for OOBE bootstrap user $UserName from query.exe output: $line" | Out-Null
            continue
        }

        Write-CloudOSDFirstBootLog "Logging off OOBE bootstrap user $UserName session $sessionId" | Out-Null
        & $LogoffSession $sessionId
        $loggedOff++
    }
    return $loggedOff
}

function Invoke-PVEAutopilotFirstBoot {
    param(
        [object] $RunConfig,
        [scriptblock] $WaitForNetwork = { Wait-CloudOSDNetwork },
        [scriptblock] $WaitForServer = { param($Url) Wait-CloudOSDServer -ServerUrl $Url },
        [scriptblock] $InstallQga = { Install-QemuGuestAgentIfPresent },
        [scriptblock] $InstallMsi = { param($Path) Install-AutopilotAgentMsi -Path $Path },
        [scriptblock] $RunPostinstall = { param($ScriptPath,$PostinstallArgs) Invoke-AutopilotAgentPostinstall -ScriptPath $ScriptPath -PostinstallArgs $PostinstallArgs },
        [scriptblock] $ConfirmHeartbeat = { param($ConfigUrl,$Token) Confirm-AutopilotAgentHeartbeat -ConfigUrl $ConfigUrl -Token $Token },
        [scriptblock] $RunOsdClient = { Invoke-CloudOSDOsdClient },
        [scriptblock] $RemoveScheduledTask = { param($Name) Remove-PVEAutopilotFirstBootTask -Name $Name },
        [scriptblock] $EndBootstrapSession = { param($Name) Invoke-PVEAutopilotBootstrapSessionLogoff -UserName $Name },
        [int] $PostinstallRetryableFailures = 12,
        [int] $PostinstallRetryWindowMinutes = 45,
        [scriptblock] $ReportEvent = {
            param(
                [string] $ServerUrl,
                [string] $RunId,
                [string] $BearerToken,
                [string] $Phase,
                [string] $EventType,
                [string] $Message,
                [string] $Severity,
                [hashtable] $Data
            )
            Write-PVEAutopilotCloudOSDEvent -ServerUrl $ServerUrl `
                -RunId $RunId `
                -BearerToken $BearerToken `
                -Phase $Phase `
                -EventType $EventType `
                -Message $Message `
                -Severity $Severity `
                -Data $Data
        }
    )
    if (-not $RunConfig) { $RunConfig = Read-PVEAutopilotCloudOSDRunConfig }

    $serverUrl = [string] $RunConfig.server_base_url
    $runId = [string] $RunConfig.run_id
    $vmid = 0
    if ($RunConfig.agent.PSObject.Properties.Match('vmid').Count -gt 0 -and $RunConfig.agent.vmid) {
        $vmid = [int] $RunConfig.agent.vmid
    } elseif ($RunConfig.PSObject.Properties.Match('vmid').Count -gt 0 -and $RunConfig.vmid) {
        $vmid = [int] $RunConfig.vmid
    }
    $bootstrapToken = [string] $RunConfig.agent.bootstrap_token
    $msiPath = Get-CloudOSDLocalPayloadPath `
        -Payload $RunConfig.payloads.autopilotagent_msi `
        -Name 'AutopilotAgent.msi'
    $postinstallPath = Get-CloudOSDLocalPayloadPath `
        -Payload $RunConfig.payloads.autopilotagent_postinstall `
        -Name 'autopilotagent-postinstall.ps1'

    function Send-PVEAutopilotFirstBootEvent {
        param(
            [Parameter(Mandatory)] [string] $Phase,
            [Parameter(Mandatory)] [string] $EventType,
            [string] $Message,
            [string] $Severity = 'info',
            [hashtable] $Data = @{}
        )
        try {
            & $ReportEvent -ServerUrl $serverUrl `
                -RunId $runId `
                -BearerToken $bootstrapToken `
                -Phase $Phase `
                -EventType $EventType `
                -Message $Message `
                -Severity $Severity `
                -Data $Data
        } catch {
            Write-CloudOSDFirstBootLog "failed to report CloudOSD event ${EventType}: $($_.Exception.Message)"
        }
    }

    Write-CloudOSDFirstBootLog "Starting CloudOSD first boot for run $runId"
    Send-PVEAutopilotFirstBootEvent -Phase 'setupcomplete' `
        -EventType 'setupcomplete_task_started' `
        -Message 'SetupComplete scheduled task started CloudOSD first boot'
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_start' `
        -Message 'CloudOSD first boot started'
    if (Test-PVEAutopilotCloudOSDDomainJoinEnabled -RunConfig $RunConfig) {
        try {
            $redactedCount = Clear-PVEAutopilotDomainJoinSecrets
            Send-PVEAutopilotFirstBootEvent -Phase 'domain_join' `
                -EventType 'domain_join_secret_cleanup_ok' `
                -Message 'Domain join unattend secrets redacted from Panther' `
                -Data @{ redacted_password_nodes = $redactedCount }
        } catch {
            Send-PVEAutopilotFirstBootEvent -Phase 'domain_join' `
                -EventType 'domain_join_secret_cleanup_failed' `
                -Severity 'error' `
                -Message $_.Exception.Message
            throw
        }
    }
    & $WaitForNetwork
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_network_ready' `
        -Message 'CloudOSD first boot network is ready'
    & $WaitForServer $serverUrl
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_server_ready' `
        -Message 'ProxmoxVEAutopilot server is reachable from installed OS'
    $qgaResult = & $InstallQga
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_qga_ready' `
        -Message 'QEMU Guest Agent service configured and running' `
        -Data @{
            source = [string] $qgaResult.source
            service = [string] $qgaResult.service
            status = [string] $qgaResult.status
        }
    & $InstallMsi $msiPath
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_agent_msi_installed' `
        -Message 'AutopilotAgent MSI installed'
    $postinstallArgs = @{
        ServerUrl = $serverUrl
        BootstrapToken = $bootstrapToken
        RunId = $runId
        Vmid = $vmid
        Phase = 'cloudosd'
    }
    $agentToken = $null
    $postinstallRecovered = $false
    try {
        & $RunPostinstall $postinstallPath $postinstallArgs
    }
    catch {
        $postinstallError = $_
        Write-CloudOSDFirstBootLog "AutopilotAgent postinstall reported failure: $($postinstallError.Exception.Message)"
        try {
            $agentToken = Get-AutopilotAgentToken
            & $ConfirmHeartbeat ($serverUrl.TrimEnd('/') + '/api/agent/v1/config') $agentToken
            $postinstallRecovered = $true
            Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
                -EventType 'firstboot_postinstall_recovered' `
                -Severity 'warning' `
                -Message 'AutopilotAgent heartbeat was visible after postinstall failure; continuing' `
                -Data @{ error = $postinstallError.Exception.Message }
        }
        catch {
            $heartbeatRecoveryError = $_
            Write-CloudOSDFirstBootLog "AutopilotAgent heartbeat recovery after postinstall failure failed: $($_.Exception.Message)"
            $retryState = Add-PVEAutopilotCloudOSDPostinstallFailure `
                -RunId $runId `
                -ErrorMessage $postinstallError.Exception.Message
            $retryData = New-PVEAutopilotCloudOSDPostinstallRetryData `
                -State $retryState `
                -ErrorMessage $postinstallError.Exception.Message `
                -RecoveryErrorMessage $heartbeatRecoveryError.Exception.Message `
                -MaxRetryableFailures $PostinstallRetryableFailures `
                -WindowMinutes $PostinstallRetryWindowMinutes
            try {
                $diagnostics = Get-CloudOSDFirstBootDiagnosticData -ErrorMessage $postinstallError.Exception.Message
            } catch {
                $diagnostics = @{
                    error = $postinstallError.Exception.Message
                    diagnostics_error = $_.Exception.Message
                }
            }
            foreach ($key in $retryData.Keys) {
                $diagnostics[$key] = $retryData[$key]
            }
            if (Test-PVEAutopilotCloudOSDPostinstallRetryAllowed `
                    -State $retryState `
                    -MaxRetryableFailures $PostinstallRetryableFailures `
                    -WindowMinutes $PostinstallRetryWindowMinutes) {
                Write-CloudOSDFirstBootLog "AutopilotAgent postinstall failure is retryable; leaving scheduled task in place for the next run."
                Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
                    -EventType 'firstboot_postinstall_retry_scheduled' `
                    -Severity 'warning' `
                    -Message 'AutopilotAgent postinstall failed before heartbeat; scheduled task will retry' `
                    -Data $retryData
                return
            }
            try {
                Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
                    -EventType 'firstboot_postinstall_failed' `
                    -Severity 'error' `
                    -Message 'AutopilotAgent postinstall failed and heartbeat recovery did not succeed' `
                    -Data $diagnostics
            } catch {
                Write-CloudOSDFirstBootLog "failed to report postinstall diagnostics: $($_.Exception.Message)"
            }
            throw $postinstallError
        }
    }
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_postinstall_complete' `
        -Message 'AutopilotAgent postinstall completed' `
        -Data @{ recovered = $postinstallRecovered }
    if (-not $agentToken) {
        $agentToken = Get-AutopilotAgentToken
    }
    if (-not $postinstallRecovered) {
        & $ConfirmHeartbeat ($serverUrl.TrimEnd('/') + '/api/agent/v1/config') $agentToken
    }
    Send-PVEAutopilotFirstBootEvent -Phase 'AutopilotAgent' `
        -EventType 'autopilotagent_heartbeat_visible' `
        -Message 'AutopilotAgent heartbeat visible from installed OS'
    Clear-PVEAutopilotCloudOSDFirstBootState -RunId $runId
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_v2_agent_ownership_ready' `
        -Message 'Persistent AutopilotAgent will claim CloudOSD v2 full-OS steps'
    try {
        Clear-PVEAutopilotOobeBootstrapAccount
        Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
            -EventType 'firstboot_oobe_bootstrap_cleanup_ok' `
            -Message 'Temporary CloudOSD OOBE bootstrap account cleanup completed'
    } catch {
        Write-CloudOSDFirstBootLog "CloudOSD OOBE bootstrap account cleanup failed: $($_.Exception.Message)"
        Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
            -EventType 'firstboot_oobe_bootstrap_cleanup_failed' `
            -Severity 'warning' `
            -Message $_.Exception.Message
    }
    & $RemoveScheduledTask 'PVEAutopilot-CloudOSD-FirstBoot'
    Write-CloudOSDFirstBootLog "CloudOSD first boot complete for run $runId"
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_complete' `
        -Message 'CloudOSD first boot completed'
    try {
        $loggedOff = & $EndBootstrapSession 'PVEAutopilot'
        Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
            -EventType 'firstboot_oobe_bootstrap_session_logoff_requested' `
            -Message 'Temporary CloudOSD OOBE bootstrap desktop session logoff requested' `
            -Data @{ sessions_logged_off = $loggedOff }
    } catch {
        Write-CloudOSDFirstBootLog "CloudOSD OOBE bootstrap session logoff failed: $($_.Exception.Message)"
        Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
            -EventType 'firstboot_oobe_bootstrap_session_logoff_failed' `
            -Severity 'warning' `
            -Message $_.Exception.Message
    }
}

function Invoke-PVEAutopilotFirstBootWithMutex {
    $mutexName = 'Global\PVEAutopilotCloudOSDFirstBoot'
    $mutex = New-Object System.Threading.Mutex($false, $mutexName)
    $lockTaken = $false
    try {
        $lockTaken = $mutex.WaitOne([TimeSpan]::Zero)
        if (-not $lockTaken) {
            Write-CloudOSDFirstBootLog 'Another CloudOSD first boot instance is already running; skipping this scheduled-task overlap.'
            try {
                $runConfig = Read-PVEAutopilotCloudOSDRunConfig
                Write-PVEAutopilotCloudOSDEvent -ServerUrl ([string] $runConfig.server_base_url) `
                    -RunId ([string] $runConfig.run_id) `
                    -BearerToken ([string] $runConfig.agent.bootstrap_token) `
                    -Phase 'first_boot' `
                    -EventType 'firstboot_overlap_skipped' `
                    -Severity 'warning' `
                    -Message 'Skipped overlapping CloudOSD first boot scheduled-task invocation' `
                    -Data (Get-CloudOSDFirstBootDiagnosticData -ErrorMessage 'overlapping first boot instance skipped')
            } catch {
                Write-CloudOSDFirstBootLog "failed to report first boot overlap skip: $($_.Exception.Message)"
            }
            return
        }
        Invoke-PVEAutopilotFirstBoot
    } finally {
        if ($lockTaken) {
            try { $mutex.ReleaseMutex() | Out-Null } catch {}
        }
        $mutex.Dispose()
    }
}

if ($env:CLOUDOSD_FIRSTBOOT_LIBRARY_ONLY -ne '1') {
    try {
        Invoke-PVEAutopilotFirstBootWithMutex
    }
    catch {
        $firstBootError = $_
        $firstBootErrorMessage = $firstBootError.Exception.Message
        Write-CloudOSDFirstBootLog "CloudOSD first boot failed: $firstBootErrorMessage"
        try {
            $runConfig = Read-PVEAutopilotCloudOSDRunConfig
            $retryData = $null
            if ($firstBootErrorMessage -like 'AutopilotAgent postinstall failed:*') {
                $retryState = Add-PVEAutopilotCloudOSDPostinstallFailure `
                    -RunId ([string] $runConfig.run_id) `
                    -ErrorMessage $firstBootErrorMessage
                $retryData = New-PVEAutopilotCloudOSDPostinstallRetryData `
                    -State $retryState `
                    -ErrorMessage $firstBootErrorMessage `
                    -RecoveryErrorMessage 'outer first boot catch' `
                    -MaxRetryableFailures 12 `
                    -WindowMinutes 45
                if (Test-PVEAutopilotCloudOSDPostinstallRetryAllowed -State $retryState) {
                    Write-CloudOSDFirstBootLog "CloudOSD first boot postinstall failure is retryable from outer catch; leaving scheduled task in place."
                    Write-PVEAutopilotCloudOSDEvent -ServerUrl ([string] $runConfig.server_base_url) `
                        -RunId ([string] $runConfig.run_id) `
                        -BearerToken ([string] $runConfig.agent.bootstrap_token) `
                        -Phase 'first_boot' `
                        -EventType 'firstboot_postinstall_retry_scheduled' `
                        -Severity 'warning' `
                        -Message 'AutopilotAgent postinstall failed before heartbeat; scheduled task will retry' `
                        -Data $retryData
                    return
                }
            }
            try {
                $failureData = Get-CloudOSDFirstBootDiagnosticData -ErrorMessage $firstBootErrorMessage
            } catch {
                $failureData = @{
                    error = $firstBootErrorMessage
                    diagnostics_error = $_.Exception.Message
                }
            }
            if ($retryData) {
                foreach ($key in $retryData.Keys) {
                    $failureData[$key] = $retryData[$key]
                }
            }
            Write-PVEAutopilotCloudOSDEvent -ServerUrl ([string] $runConfig.server_base_url) `
                -RunId ([string] $runConfig.run_id) `
                -BearerToken ([string] $runConfig.agent.bootstrap_token) `
                -Phase 'first_boot' `
                -EventType 'firstboot_failed' `
                -Severity 'error' `
                -Message $firstBootErrorMessage `
                -Data $failureData
        } catch {
            Write-CloudOSDFirstBootLog "failed to report CloudOSD first boot failure: $($_.Exception.Message)"
        }
        throw
    }
}
