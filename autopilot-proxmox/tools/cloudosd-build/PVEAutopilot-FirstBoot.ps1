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

function Install-QemuGuestAgentIfPresent {
    $candidates = @(
        (Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\qemu-ga-x86_64.msi'),
        'D:\guest-agent\qemu-ga-x86_64.msi',
        'E:\guest-agent\qemu-ga-x86_64.msi',
        'F:\guest-agent\qemu-ga-x86_64.msi'
    )
    $msi = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $msi) {
        Write-CloudOSDFirstBootLog 'QEMU Guest Agent MSI not present; skipping.'
        return
    }
    Write-CloudOSDFirstBootLog "Installing QEMU Guest Agent from $msi"
    $log = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD\qemu-ga-install.log'
    $p = Start-Process -FilePath msiexec.exe `
        -ArgumentList @('/i', $msi, '/qn', '/norestart', '/L*v', $log) `
        -Wait -PassThru
    if ($p.ExitCode -notin @(0, 3010, 1641)) {
        throw "QEMU Guest Agent MSI failed with exit code $($p.ExitCode)"
    }
    try {
        Set-Service -Name QEMU-GA -StartupType Automatic -ErrorAction Stop
        Start-Service -Name QEMU-GA -ErrorAction Stop
        Write-CloudOSDFirstBootLog 'QEMU Guest Agent service started.'
    } catch {
        Write-CloudOSDFirstBootLog "QEMU Guest Agent MSI installed but service did not start yet: $($_.Exception.Message)"
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
        [scriptblock] $InstallMsi = { param($Path) Install-AutopilotAgentMsi -Path $Path },
        [scriptblock] $RunPostinstall = { param($ScriptPath,$PostinstallArgs) Invoke-AutopilotAgentPostinstall -ScriptPath $ScriptPath -PostinstallArgs $PostinstallArgs },
        [scriptblock] $ConfirmHeartbeat = { param($ConfigUrl,$Token) Confirm-AutopilotAgentHeartbeat -ConfigUrl $ConfigUrl -Token $Token },
        [scriptblock] $RunOsdClient = { Invoke-CloudOSDOsdClient },
        [scriptblock] $RemoveScheduledTask = { param($Name) Remove-PVEAutopilotFirstBootTask -Name $Name },
        [scriptblock] $EndBootstrapSession = { param($Name) Invoke-PVEAutopilotBootstrapSessionLogoff -UserName $Name },
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
    Install-QemuGuestAgentIfPresent
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
    & $RunPostinstall $postinstallPath $postinstallArgs
    Send-PVEAutopilotFirstBootEvent -Phase 'first_boot' `
        -EventType 'firstboot_postinstall_complete' `
        -Message 'AutopilotAgent postinstall completed'
    $agentToken = Get-AutopilotAgentToken
    & $ConfirmHeartbeat ($serverUrl.TrimEnd('/') + '/api/agent/v1/config') $agentToken
    Send-PVEAutopilotFirstBootEvent -Phase 'AutopilotAgent' `
        -EventType 'autopilotagent_heartbeat_visible' `
        -Message 'AutopilotAgent heartbeat visible from installed OS'
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

if ($env:CLOUDOSD_FIRSTBOOT_LIBRARY_ONLY -ne '1') {
    try {
        Invoke-PVEAutopilotFirstBoot
    }
    catch {
        Write-CloudOSDFirstBootLog "CloudOSD first boot failed: $($_.Exception.Message)"
        try {
            $runConfig = Read-PVEAutopilotCloudOSDRunConfig
            Write-PVEAutopilotCloudOSDEvent -ServerUrl ([string] $runConfig.server_base_url) `
                -RunId ([string] $runConfig.run_id) `
                -BearerToken ([string] $runConfig.agent.bootstrap_token) `
                -Phase 'first_boot' `
                -EventType 'firstboot_failed' `
                -Severity 'error' `
                -Message $_.Exception.Message
        } catch {
            Write-CloudOSDFirstBootLog "failed to report CloudOSD first boot failure: $($_.Exception.Message)"
        }
        throw
    }
}
