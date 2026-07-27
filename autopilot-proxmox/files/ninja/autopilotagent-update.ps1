# Pushes the published AutopilotAgent MSI onto this endpoint. No arguments.
#
# Deploy through NinjaOne as a script action and run it. Everything it needs is
# either baked in below or discovered from the machine itself.
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
# Because of that, this script does NOT ask permission before installing. If it
# only installed when the server said "upgrade_available" it would do nothing
# on exactly the machines that need it. It installs, every time it runs.
# msiexec on an already-current MSI is a repair, which is harmless.
#
# It still queries update-check when it can, purely to record the answer. The
# endpoint prefers the installed_version the agent reports
# (Assembly.GetName().Version) over the heartbeat-recorded agent_version the
# fleet page reads, and those two can disagree. When they do, this logs
# MISMATCH, which is the evidence needed to fix update-check rather than keep
# pushing MSIs forever.
#
# The QGA path is not used here on purpose: guest-exec and guest-exec-status
# time out too often on a busy node to drive a fleet-wide update.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- baked config
# Only used when agent.json is missing or has no ServerUrl. A bootstrapped
# agent already knows its own controller, so that value wins.
$FallbackServerUrl      = "http://192.168.2.4:5000"
$DownloadTimeoutSeconds = 600
$AgentRoot              = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent"
$LogRoot                = Join-Path $AgentRoot "install"
$ConfigPath             = Join-Path $AgentRoot "agent.json"
$InstallDir             = Join-Path $env:ProgramFiles "ProxmoxVEAutopilot\AutopilotAgent"
# ------------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot "update.log"

function Write-InstallLog {
    param([string]$Message)
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Output $line
}

function Get-InstalledAgentVersion {
    # Two versions matter and they are not always the same number. The fleet
    # page shows what the agent reports over heartbeat, which is the assembly
    # version; the file version is what Explorer shows.
    $exe = Join-Path $InstallDir "AutopilotAgent.exe"
    $dll = Join-Path $InstallDir "AutopilotAgent.dll"
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
        Write-InstallLog "No agent.json at $ConfigPath; this machine may never have been bootstrapped."
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

function Resolve-ServerUrl {
    param($Identity)
    if ($Identity -and -not [string]::IsNullOrWhiteSpace($Identity.ServerUrl)) {
        return $Identity.ServerUrl.TrimEnd("/")
    }
    Write-InstallLog "Falling back to the baked controller URL."
    return $FallbackServerUrl.TrimEnd("/")
}

function Get-PublishedRelease {
    param($Identity, [string]$BaseUrl)
    # update-check needs the agent's bearer token, which only exists once the
    # agent has been bootstrapped. Without it we can still install, we just
    # cannot verify the download or record the disagreement.
    if (-not $Identity -or [string]::IsNullOrWhiteSpace($Identity.AgentToken)) {
        Write-InstallLog "No agent token available; skipping the authenticated version query."
        return $null
    }
    $rid = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "win-arm64" } else { "win-x64" }
    $body = @{
        agent_id           = $Identity.AgentId
        installed_version  = (Get-InstalledAgentVersion).AssemblyVersion
        runtime_identifier = $rid
    } | ConvertTo-Json -Compress
    try {
        return Invoke-RestMethod `
            -Uri ($BaseUrl + "/api/agent/v1/update-check") `
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

Write-InstallLog "Starting AutopilotAgent update."

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "AutopilotAgent update requires administrative context."
}

$before = Get-InstalledAgentVersion
Write-InstallLog "Installed: file=$($before.FileVersion) assembly=$($before.AssemblyVersion) path=$($before.Path)"

$agent = Get-AgentIdentity
$serverUrl = Resolve-ServerUrl -Identity $agent
Write-InstallLog "Controller: $serverUrl"
if ($agent) {
    Write-InstallLog "Agent identity: id=$($agent.AgentId)"
}

$release = Get-PublishedRelease -Identity $agent -BaseUrl $serverUrl
$expectedSha = $null
if ($release) {
    Write-InstallLog "update-check: status=$($release.status) installed=$($release.installed_version) published=$($release.published_version) reason=$($release.reason)"
    $expectedSha = $release.sha256
    # The disagreement worth capturing. If the server says "current" while the
    # assembly version is clearly older than what it publishes, the bug is in
    # which version each side compares, not in the download.
    if ($release.status -ne "upgrade_available" -and $release.published_version -and $before.AssemblyVersion) {
        if ($release.published_version -ne $before.AssemblyVersion) {
            Write-InstallLog "MISMATCH: server reports '$($release.status)' but published=$($release.published_version) differs from installed assembly=$($before.AssemblyVersion). This is why self-upgrade never fires on this machine."
        }
    }
}

$msiUrl = $serverUrl + "/api/cloudosd/assets/autopilotagent.msi"
$dst = Join-Path $env:TEMP "AutopilotAgent-push.msi"
Write-InstallLog "Downloading $msiUrl"
Invoke-WebRequest -Uri $msiUrl -OutFile $dst -UseBasicParsing -TimeoutSec $DownloadTimeoutSeconds
$size = (Get-Item $dst).Length
$actualSha = (Get-FileHash -Path $dst -Algorithm SHA256).Hash.ToLowerInvariant()
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

# Installs unconditionally. See the header: gating on the server's verdict
# would skip exactly the machines this script exists for. msiexec on an
# already-current MSI is a repair.
#
# Running msiexec from here rather than from inside the agent matters: the
# installer stops the AutopilotAgent service to replace its binaries, and an
# agent that launched its own installer would be killing the parent of the
# process it is waiting on.
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
