# First-boot continuation for CloudOSD deployments.
#
# Runs as SYSTEM from a scheduled task created by SetupComplete. It installs
# the persistent AutopilotAgent MSI, runs the existing postinstall bootstrap
# script with the CloudOSD run token, confirms heartbeat, then removes its own
# scheduled task.

$ErrorActionPreference = 'Stop'

function Write-CloudOSDFirstBootLog {
    param([Parameter(Mandatory)] [string] $Message)
    $root = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\CloudOSD'
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -LiteralPath (Join-Path $root 'firstboot.log') -Value $line -Encoding UTF8
    Write-Output $line
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

function Invoke-PVEAutopilotFirstBoot {
    param(
        [object] $RunConfig,
        [scriptblock] $WaitForNetwork = { Wait-CloudOSDNetwork },
        [scriptblock] $WaitForServer = { param($Url) Wait-CloudOSDServer -ServerUrl $Url },
        [scriptblock] $InstallMsi = { param($Path) Install-AutopilotAgentMsi -Path $Path },
        [scriptblock] $RunPostinstall = { param($ScriptPath,$PostinstallArgs) Invoke-AutopilotAgentPostinstall -ScriptPath $ScriptPath -PostinstallArgs $PostinstallArgs },
        [scriptblock] $ConfirmHeartbeat = { param($ConfigUrl,$Token) Confirm-AutopilotAgentHeartbeat -ConfigUrl $ConfigUrl -Token $Token },
        [scriptblock] $RemoveScheduledTask = { param($Name) Remove-PVEAutopilotFirstBootTask -Name $Name }
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

    Write-CloudOSDFirstBootLog "Starting CloudOSD first boot for run $runId"
    & $WaitForNetwork
    & $WaitForServer $serverUrl
    Install-QemuGuestAgentIfPresent
    & $InstallMsi $msiPath
    $postinstallArgs = @{
        ServerUrl = $serverUrl
        BootstrapToken = $bootstrapToken
        RunId = $runId
        Vmid = $vmid
        Phase = 'cloudosd'
    }
    & $RunPostinstall $postinstallPath $postinstallArgs
    $agentToken = Get-AutopilotAgentToken
    & $ConfirmHeartbeat ($serverUrl.TrimEnd('/') + '/api/agent/v1/config') $agentToken
    & $RemoveScheduledTask 'PVEAutopilot-CloudOSD-FirstBoot'
    Write-CloudOSDFirstBootLog "CloudOSD first boot complete for run $runId"
}

if ($env:CLOUDOSD_FIRSTBOOT_LIBRARY_ONLY -ne '1') {
    try {
        Invoke-PVEAutopilotFirstBoot
    }
    catch {
        Write-CloudOSDFirstBootLog "CloudOSD first boot failed: $($_.Exception.Message)"
        throw
    }
}
