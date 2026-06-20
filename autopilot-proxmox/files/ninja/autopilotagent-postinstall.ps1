[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,
    [string]$BootstrapToken,
    [string]$AgentId,
    [int]$Vmid = 0,
    [string]$RunId,
    [string]$Phase = "retrofit",
    [int]$ApprovalTimeoutSeconds = 1800,
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
    Write-Output $line
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hash = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash) -replace "-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-TokenDiagnostic {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][string]$Value
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return "$Name length=0 placeholder=false sha256prefix=<empty>"
    }
    $trimmed = $Value.Trim()
    $hash = Get-Sha256Hex -Value $trimmed
    $isPlaceholder = ($trimmed -eq "BootstrapToken")
    return "$Name length=$($trimmed.Length) placeholder=$isPlaceholder sha256prefix=$($hash.Substring(0, 12))"
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
    }
    else {
        Write-InstallLog "QGA RPC block not present."
        Restart-Service -Name QEMU-GA -Force -ErrorAction SilentlyContinue
    }
}

Write-InstallLog "Starting AutopilotAgent postinstall."
New-Item -ItemType Directory -Force -Path $AgentRoot | Out-Null

Write-InstallLog "BootstrapToken parameter diagnostic: $(Get-TokenDiagnostic -Name "param" -Value $BootstrapToken)"
$bootstrapEnvCandidates = @(Get-ChildItem Env: -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -ieq "BootstrapToken" -or $_.Name -imatch "bootstrap"
    })
foreach ($candidate in $bootstrapEnvCandidates) {
    Write-InstallLog "BootstrapToken env diagnostic: $(Get-TokenDiagnostic -Name "env:$($candidate.Name)" -Value ([string]$candidate.Value))"
}

if ([string]::IsNullOrWhiteSpace($BootstrapToken) -or $BootstrapToken.Trim() -eq "BootstrapToken") {
    $envBootstrapToken = $bootstrapEnvCandidates |
    Where-Object { $_.Name -ieq "BootstrapToken" } |
    Select-Object -First 1
    if (-not $envBootstrapToken) {
        $envBootstrapToken = $bootstrapEnvCandidates |
        Where-Object {
            -not [string]::IsNullOrWhiteSpace([string]$_.Value) -and
            ([string]$_.Value).Trim() -ne "BootstrapToken"
        } |
        Select-Object -First 1
    }
    if ($envBootstrapToken) {
        Write-InstallLog "BootstrapToken selected from env:$($envBootstrapToken.Name)."
        $BootstrapToken = [string]$envBootstrapToken.Value
    }
    else {
        Write-InstallLog "BootstrapToken no matching environment candidate found."
        $BootstrapToken = [string]$env:BootstrapToken
    }
}
$BootstrapToken = $BootstrapToken.Trim()
if (-not $BootstrapToken -or $BootstrapToken -eq "BootstrapToken") {
    throw "BootstrapToken was not resolved. Set the Ninja script variable named BootstrapToken to the 64-character SHA-256 proof value and remove the literal -BootstrapToken `"BootstrapToken`" parameter."
}
$bootstrapTokenHash = Get-Sha256Hex -Value $BootstrapToken
$bootstrapProofPrefix = if ($BootstrapToken.Length -ge 12) { $BootstrapToken.Substring(0, 12) } else { $BootstrapToken }
Write-InstallLog "BootstrapToken received. Length=$($BootstrapToken.Length) ProofPrefix=$bootstrapProofPrefix Sha256OfProofPrefix=$($bootstrapTokenHash.Substring(0, 12))"

if (-not $AgentId) {
    $AgentId = "agent-$($env:COMPUTERNAME.ToLowerInvariant())"
}

$bootstrapBody = @{
    agent_id      = $AgentId
    phase         = $Phase
    computer_name = $env:COMPUTERNAME
    agent_version = "msi-retrofit"
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

if (-not $bootstrap.agent_token) {
    if (-not $bootstrap.approval_id -or -not $bootstrap.poll_url) {
        throw "Bootstrap did not return an agent token or approval polling metadata."
    }
    $pollUrl = [string]$bootstrap.poll_url
    Write-InstallLog "Bootstrap pending approval. ApprovalId=$($bootstrap.approval_id)"
    $approvalDeadline = (Get-Date).AddSeconds($ApprovalTimeoutSeconds)
    do {
        $retryAfter = 5
        if ($bootstrap.retry_after_seconds) {
            $retryAfter = [int]$bootstrap.retry_after_seconds
        }
        Start-Sleep -Seconds $retryAfter
        $claimUrl = $ServerUrl.TrimEnd("/") + $pollUrl
        try {
            $bootstrap = Invoke-RestMethod -Method Get -Uri $claimUrl `
                -Headers @{ Authorization = "Bearer $BootstrapToken" } `
                -TimeoutSec 15
            Write-InstallLog "Approval status=$($bootstrap.approval_status)."
        }
        catch {
            Write-InstallLog "Approval poll failed: $($_.Exception.Message)"
            continue
        }
        if ($bootstrap.agent_token) {
            break
        }
    } while ((Get-Date) -lt $approvalDeadline)
}

if (-not $bootstrap.agent_token) {
    throw "AutopilotAgent was not approved within $ApprovalTimeoutSeconds seconds."
}

$config = [ordered]@{
    serverUrl                = $ServerUrl.TrimEnd("/")
    agentId                  = $bootstrap.agent_id
    agentToken               = $bootstrap.agent_token
    runId                    = $(if ($RunId) { $RunId } else { $null })
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
    }
    catch {
        Write-InstallLog "Waiting for AutopilotAgent health: $($_.Exception.Message)"
    }
} while ((Get-Date) -lt $deadline)

if (-not $heartbeatVerified) {
    throw "AutopilotAgent did not report a heartbeat within $HeartbeatTimeoutSeconds seconds."
}

try {
    Remove-QgaNetworkRpcBlock
}
catch {
    Write-InstallLog "QGA RPC block cleanup failed after AutopilotAgent heartbeat; continuing: $($_.Exception.Message)"
}

$primaryIp = Get-PrimaryIpv4
Write-InstallLog "AutopilotAgent postinstall complete. AgentId=$($bootstrap.agent_id) PrimaryIPv4=$primaryIp"
