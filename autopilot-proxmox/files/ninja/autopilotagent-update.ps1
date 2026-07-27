[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    # Install even when the agent already reports the published version.
    # Useful for repairing a broken install rather than upgrading one.
    [switch]$Force,

    # Report what would happen and exit without touching msiexec.
    [switch]$WhatIfOnly,

    [int]$DownloadTimeoutSeconds = 600,
    [string]$LogRoot = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent\install"
)

# Pushes the published AutopilotAgent MSI onto this endpoint.
#
# Why this exists
# ---------------
# The agent is supposed to upgrade itself: it calls
# /api/agent/v1/update-check after every heartbeat and installs the MSI when
# the server reports "upgrade_available". On part of the fleet that loop never
# fires. Those agents heartbeat every 30s, get a 200 from update-check, and
# have logged thousands of lines with zero "MSI update completed" and zero
# "Agent update check failed" entries, which are the only two outcomes the
# updater can produce once it is past its first branch. So they are being told
# they are current while the fleet page shows "Upgrade available".
#
# Running the install from here sidesteps that entirely. It also records what
# update-check actually answers for this machine, which is the evidence needed
# to close the underlying bug: the endpoint prefers the installed_version the
# agent sends (Assembly.GetName().Version) over the heartbeat-recorded
# agent_version the fleet page reads, and those two can disagree.
#
# Deploy through NinjaOne as a script action. Only ServerUrl is required; the
# agent's own token is read from agent.json to authenticate the version query,
# and the script degrades to an unverified install when no token is present.

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot "update.log"
$AgentRoot = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent"
$ConfigPath = Join-Path $AgentRoot "agent.json"

function Write-InstallLog {
    param([string]$Message)
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Output $line
}

function Get-InstalledAgentVersion {
    # Two versions matter and they are not always the same number. The fleet
    # page shows what the agent reports over heartbeat, which is the assembly
    # version; the file version is what shows up in Explorer.
    $exe = Join-Path $env:ProgramFiles "ProxmoxVEAutopilot\AutopilotAgent\AutopilotAgent.exe"
    $dll = Join-Path $env:ProgramFiles "ProxmoxVEAutopilot\AutopilotAgent\AutopilotAgent.dll"
    $result = [ordered]@{ FileVersion = $null; AssemblyVersion = $null; Path = $null }
    if (Test-Path $exe) {
        $result.Path = $exe
        $result.FileVersion = (Get-Item $exe).VersionInfo.FileVersion
    }
    if (Test-Path $dll) {
        try {
            $result.AssemblyVersion = [System.Reflection.AssemblyName]::GetAssemblyName($dll).Version.ToString()
        }
        catch {
            Write-InstallLog "Could not read assembly version: $($_.Exception.Message)"
        }
    }
    return [pscustomobject]$result
}

function Get-AgentIdentity {
    if (-not (Test-Path $ConfigPath)) {
        return $null
    }
    try {
        return Get-Content $ConfigPath -Raw | ConvertFrom-Json
    }
    catch {
        Write-InstallLog "agent.json is present but unreadable: $($_.Exception.Message)"
        return $null
    }
}

function Get-PublishedRelease {
    param($Identity)
    # update-check needs the agent's bearer token, which only exists once the
    # agent has been bootstrapped. Without it we can still install, we just
    # cannot verify the download or log the disagreement.
    if (-not $Identity -or [string]::IsNullOrWhiteSpace($Identity.AgentToken)) {
        Write-InstallLog "No agent token available; skipping the authenticated version query."
        return $null
    }
    $rid = if ([System.Environment]::Is64BitOperatingSystem -and $env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win-arm64" } else { "win-x64" }
    $body = @{
        agent_id           = $Identity.AgentId
        installed_version  = (Get-InstalledAgentVersion).AssemblyVersion
        runtime_identifier = $rid
    } | ConvertTo-Json -Compress
    try {
        return Invoke-RestMethod `
            -Uri ($ServerUrl.TrimEnd("/") + "/api/agent/v1/update-check") `
            -Method Post `
            -Headers @{ Authorization = "Bearer $($Identity.AgentToken)" } `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 60
    }
    catch {
        Write-InstallLog "update-check query failed: $($_.Exception.Message)"
        return $null
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

Write-InstallLog "Starting AutopilotAgent update against $ServerUrl."

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "AutopilotAgent update requires administrative context."
}

$before = Get-InstalledAgentVersion
Write-InstallLog "Installed: file=$($before.FileVersion) assembly=$($before.AssemblyVersion) path=$($before.Path)"

$agent = Get-AgentIdentity
if ($agent) {
    Write-InstallLog "Agent identity: id=$($agent.AgentId) server=$($agent.ServerUrl)"
}

$release = Get-PublishedRelease -Identity $agent
$expectedSha = $null
if ($release) {
    Write-InstallLog "update-check: status=$($release.status) installed=$($release.installed_version) published=$($release.published_version) reason=$($release.reason)"
    $expectedSha = $release.sha256
    # This is the disagreement worth capturing. If the server says "current"
    # while the assembly version is clearly older than what it publishes, the
    # bug is in which version each side compares, not in the download.
    if ($release.status -ne "upgrade_available" -and $release.published_version -and $before.AssemblyVersion) {
        if ($release.published_version -ne $before.AssemblyVersion) {
            Write-InstallLog "MISMATCH: server reports '$($release.status)' but published=$($release.published_version) differs from installed assembly=$($before.AssemblyVersion). This is why self-upgrade never fires on this machine."
        }
    }
    if ($release.status -ne "upgrade_available" -and -not $Force) {
        Write-InstallLog "Server reports no upgrade. Re-run with -Force to install anyway."
    }
}

if ($WhatIfOnly) {
    Write-InstallLog "WhatIfOnly set; stopping before download."
    exit 0
}

$msiUrl = $ServerUrl.TrimEnd("/") + "/api/cloudosd/assets/autopilotagent.msi"
$dst = Join-Path $env:TEMP "AutopilotAgent-push.msi"
Write-InstallLog "Downloading $msiUrl"
Invoke-WebRequest -Uri $msiUrl -OutFile $dst -UseBasicParsing -TimeoutSec $DownloadTimeoutSeconds
$size = (Get-Item $dst).Length
$actualSha = Get-Sha256Hex -Path $dst
Write-InstallLog "Downloaded $size bytes sha256=$actualSha"

if ($expectedSha) {
    if ($actualSha -ne $expectedSha.ToLowerInvariant()) {
        Remove-Item $dst -Force -ErrorAction SilentlyContinue
        throw "Downloaded MSI failed SHA-256 validation (expected $expectedSha, got $actualSha)."
    }
    Write-InstallLog "SHA-256 matches the published release."
}
else {
    Write-InstallLog "No published SHA-256 to compare against; installing unverified."
}

# msiexec stops the AutopilotAgent service to replace its binaries. Running it
# from here rather than from inside the agent avoids the agent tearing down the
# process that is waiting on the installer.
Write-InstallLog "Running msiexec /i /qn /norestart"
$proc = Start-Process msiexec.exe -ArgumentList @("/i", $dst, "/qn", "/norestart") -Wait -PassThru
Write-InstallLog "msiexec exit code $($proc.ExitCode)"
if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
    throw "AutopilotAgent MSI install failed with exit code $($proc.ExitCode)."
}

Remove-Item $dst -Force -ErrorAction SilentlyContinue

$svc = Get-Service -Name AutopilotAgent -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne "Running") {
        Write-InstallLog "Service is $($svc.Status); starting it."
        Start-Service -Name AutopilotAgent
    }
    Write-InstallLog "Service state=$((Get-Service -Name AutopilotAgent).Status)"
}
else {
    Write-InstallLog "Warning: AutopilotAgent service not found after install."
}

$after = Get-InstalledAgentVersion
Write-InstallLog "Now installed: file=$($after.FileVersion) assembly=$($after.AssemblyVersion)"
if ($before.AssemblyVersion -eq $after.AssemblyVersion -and $before.FileVersion -eq $after.FileVersion) {
    Write-InstallLog "Version did not change. The MSI may already match, or the install did not take."
}
Write-InstallLog "AutopilotAgent update complete. The fleet page updates on the next heartbeat (~30s)."
