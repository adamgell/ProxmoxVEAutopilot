[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [Parameter(Mandatory = $true)]
    [string]$BootstrapToken,

    [string]$AgentId,
    [int]$Vmid = 0,
    [string]$RunId,
    [string]$Phase = "retrofit",
    [int]$HeartbeatTimeoutSeconds = 90,
    [string]$LogRoot = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent\install"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot "postinstall.log"
$AgentRoot = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent"
$ConfigPath = Join-Path $AgentRoot "agent.json"

function Write-InstallLog {
    param([string]$Message)
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line
}

function Get-PrimaryIpv4 {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "169.254.*" -and
            $_.IPAddress -ne "127.0.0.1" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Sort-Object InterfaceMetric, PrefixLength |
        Select-Object -First 1 -ExpandProperty IPAddress
}

function Remove-QgaNetworkRpcBlock {
    $svc = Get-CimInstance Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-InstallLog "QEMU-GA service not found; skipping QGA policy reset."
        return
    }
    $oldPath = [string]$svc.PathName
    $newPath = ($oldPath -replace "\s+--block-rpcs=guest-network-get-interfaces", "")
    $newPath = ($newPath -replace "--block-rpcs=guest-network-get-interfaces\s+", "")
    if ($newPath -ne $oldPath) {
        Write-InstallLog "Removing QGA guest-network-get-interfaces RPC block."
        $result = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{
            PathName = $newPath
        }
        if ($result.ReturnValue -ne 0) {
            throw "QEMU-GA service command-line configuration failed with exit $($result.ReturnValue)."
        }
        Restart-Service -Name QEMU-GA -Force -ErrorAction Stop
    } else {
        Write-InstallLog "QGA RPC block not present."
        Restart-Service -Name QEMU-GA -Force -ErrorAction SilentlyContinue
    }
}

Write-InstallLog "Starting AutopilotAgent postinstall."
New-Item -ItemType Directory -Force -Path $AgentRoot | Out-Null

if (-not $AgentId) {
    $AgentId = "agent-$($env:COMPUTERNAME.ToLowerInvariant())"
}

$bootstrapBody = @{
    agent_id        = $AgentId
    phase           = $Phase
    computer_name   = $env:COMPUTERNAME
    agent_version   = "msi-retrofit"
}
if ($Vmid -gt 0) {
    $bootstrapBody.vmid = $Vmid
}
if ($RunId) {
    $bootstrapBody.run_id = $RunId
}

$bootstrapUrl = $ServerUrl.TrimEnd("/") + "/api/agent/v1/bootstrap"
Write-InstallLog "Bootstrapping AutopilotAgent through $bootstrapUrl."
$bootstrap = Invoke-RestMethod -Method Post -Uri $bootstrapUrl `
    -Headers @{ Authorization = "Bearer $BootstrapToken" } `
    -ContentType "application/json" `
    -Body ($bootstrapBody | ConvertTo-Json -Depth 5)

$config = [ordered]@{
    serverUrl                = $ServerUrl.TrimEnd("/")
    agentId                  = $bootstrap.agent_id
    agentToken               = $bootstrap.agent_token
    runId                    = $RunId
    phase                    = $Phase
    vmid                     = $(if ($Vmid -gt 0) { $Vmid } else { $null })
    heartbeatIntervalSeconds = $bootstrap.heartbeat_interval_seconds
}
$config | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigPath -Encoding UTF8
Write-InstallLog "Wrote AutopilotAgent config to $ConfigPath."

Set-Service -Name AutopilotAgent -StartupType Automatic -ErrorAction Stop
Start-Service -Name AutopilotAgent -ErrorAction Stop

$deadline = (Get-Date).AddSeconds($HeartbeatTimeoutSeconds)
$heartbeatVerified = $false
do {
    Start-Sleep -Seconds 5
    try {
        $configCheck = Invoke-RestMethod -Method Get `
            -Uri ($ServerUrl.TrimEnd("/") + "/api/agent/v1/config") `
            -Headers @{ Authorization = "Bearer $($bootstrap.agent_token)" } `
            -TimeoutSec 10
        if ($configCheck.agent_id -eq $bootstrap.agent_id -and $configCheck.last_heartbeat_at) {
            Write-InstallLog "AutopilotAgent heartbeat verified at $($configCheck.last_heartbeat_at)."
            $heartbeatVerified = $true
            break
        }
        Write-InstallLog "AutopilotAgent is authenticated; waiting for first heartbeat."
    } catch {
        Write-InstallLog "Waiting for AutopilotAgent health: $($_.Exception.Message)"
    }
} while ((Get-Date) -lt $deadline)

if (-not $heartbeatVerified) {
    throw "AutopilotAgent did not report a heartbeat within $HeartbeatTimeoutSeconds seconds."
}

Remove-QgaNetworkRpcBlock

$primaryIp = Get-PrimaryIpv4
Write-InstallLog "AutopilotAgent postinstall complete. AgentId=$($bootstrap.agent_id) PrimaryIPv4=$primaryIp"
