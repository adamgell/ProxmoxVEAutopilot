<#
.SYNOPSIS
    PE-side bootstrap interpreter: identify, fetch manifest, dispatch steps, checkin.

.DESCRIPTION
    Replaces the placeholder Bootstrap from Plan 1. Runs from unattend.xml's
    windowsPE-pass RunSynchronousCommand. Must NOT exit on its own — the
    caller (winpeshl.ini's chained cmd.exe) keeps PE alive.

    Lifecycle:
      1. Start-Transcript
      2. Read Bootstrap.json
      3. Wait-PeNetwork (KNOWN-ISSUES #1)
      4. Initialize-SshHostKeys (KNOWN-ISSUES #2; no-op if SSH wasn't baked)
      5. Identify via Get-PeIdentity (SMBIOS UUID)
      6. Invoke-Manifest
      7. For each step: Send-Checkin starting → Invoke-<Type>Step → Send-Checkin ok|error
      8. If a terminal step (reboot|shutdown) fired, return; otherwise return so
         winpeshl's chained cmd.exe keeps the operator on the console.
#>

[CmdletBinding()]
param(
    [switch] $DryRunForTesting
)

# Add the Modules path so Import-Module finds them.
$env:PSModulePath = "X:\autopilot\Modules;$env:PSModulePath"

if ($DryRunForTesting) {
    # When sourced from Pester (Bootstrap.Tests.ps1), Import-Module the local
    # versions so dispatch logic can be exercised without a real PE.
    # Use $MyInvocation.MyCommand.Path so we get Bootstrap.ps1's own directory
    # even when the script is dot-sourced (in which case $PSScriptRoot is the
    # caller's directory, not ours).
    $bootstrapDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $localModules  = Join-Path $bootstrapDir 'Modules'
    # Import PESteps first; its .psm1 imports PETransport as a nested module.
    # Then re-import PETransport (without -Force so we get a session-level copy)
    # so that Send-Checkin is visible in the session scope for Invoke-BootstrapManifest.
    Import-Module (Join-Path $localModules 'Autopilot.PESteps' 'Autopilot.PESteps.psd1') -Force
    Import-Module (Join-Path $localModules 'Autopilot.PETransport' 'Autopilot.PETransport.psd1') -Force
} else {
    Import-Module Autopilot.PETransport -Force -ErrorAction Stop -Verbose
    Import-Module Autopilot.PESteps -Force -ErrorAction Stop -Verbose
}


function Invoke-BootstrapManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Manifest,
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $VmUuid
    )

    $onError = if ($Manifest.PSObject.Properties.Name -contains 'onError') { $Manifest.onError } else { 'halt' }

    foreach ($step in $Manifest.steps) {
        $stepId = $step.id
        $type   = $step.type
        $startTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Send-Checkin -OrchestratorUrl $OrchestratorUrl -VmUuid $VmUuid -StepId $stepId `
            -Status 'starting' -Timestamp $startTs

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $result = switch ($type) {
                'log'            { Invoke-LogStep -Message $step.message }
                'partition'      { Invoke-PartitionStep -Layout $step.layout }
                'apply-wim'      {
                    $tgt = if ($step.PSObject.Properties.Name -contains 'target') { $step.target } else { 'W:' }
                    $idx = if ($step.PSObject.Properties.Name -contains 'index') { $step.index } else { 1 }
                    Invoke-ApplyWimStep -OrchestratorUrl $OrchestratorUrl `
                        -Sha256 $step.content.sha256 -Size $step.content.size -Target $tgt -Index $idx
                }
                'stage-files'    { Invoke-StageFilesStep -OrchestratorUrl $OrchestratorUrl `
                                    -Sha256 $step.content.sha256 -Size $step.content.size -Target $step.target }
                'write-unattend' { Invoke-WriteUnattendStep -OrchestratorUrl $OrchestratorUrl `
                                    -Sha256 $step.content.sha256 -Size $step.content.size -Target $step.target }
                'set-registry'   { Invoke-SetRegistryStep -Hive $step.hive -Target $step.target -Keys $step.keys }
                'schedule-task'  { Invoke-ScheduleTaskStep -Target $step.target -Name $step.name -TaskXml $step.taskXml }
                'bcdboot'        { Invoke-BcdbootStep -Windows $step.windows -Esp $step.esp }
                'inject-driver'  { Invoke-InjectDriverStep -OrchestratorUrl $OrchestratorUrl `
                                    -Sha256 $step.content.sha256 -Size $step.content.size -Target $step.target }
                'reboot'         { Invoke-RebootStep }
                'shutdown'       { Invoke-ShutdownStep }
                default          { throw "unknown step type '$type' (step id=$stepId)" }
            }
            $sw.Stop()
            $okTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            Send-Checkin -OrchestratorUrl $OrchestratorUrl -VmUuid $VmUuid -StepId $stepId `
                -Status 'ok' -Timestamp $okTs -DurationSec ($sw.Elapsed.TotalSeconds) `
                -LogTail $result.LogTail -Extra ($result.Extra ?? @{})
        } catch {
            $sw.Stop()
            $errTs = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            $errMsg = $_.Exception.Message
            Send-Checkin -OrchestratorUrl $OrchestratorUrl -VmUuid $VmUuid -StepId $stepId `
                -Status 'error' -Timestamp $errTs -DurationSec ($sw.Elapsed.TotalSeconds) `
                -LogTail "step $stepId failed: $errMsg" -ErrorMessage $errMsg
            if ($onError -eq 'continue') {
                Write-Host "Bootstrap: step $stepId failed but onError=continue; proceeding"
                continue
            }
            throw
        }
    }
}


# When sourced for testing, return without running the boot logic.
if ($DryRunForTesting) { return }


# ---- Real PE boot path ----

# Guard: if a previous run already laid down Windows, don't re-partition.
# Drive letters don't persist across PE reboots. Use WMI (no StorageWMI pkg needed).
$vols = Get-CimInstance Win32_Volume -ErrorAction SilentlyContinue |
    Where-Object { $_.Label -eq 'Windows' -and $_.DriveType -eq 3 }
foreach ($vol in $vols) {
    $letter = if ($vol.DriveLetter) { $vol.DriveLetter } else {
        # No letter assigned — mount it temporarily
        $vol | Set-CimInstance -Property @{ DriveLetter = 'W:' } -ErrorAction SilentlyContinue
        'W:'
    }
    if (Test-Path "$letter\Windows\System32\ntoskrnl.exe") {
        Write-Host "Windows already installed on $letter — shutting down PE."
        wpeutil shutdown
        return
    }
}

$transcript = 'X:\Windows\Temp\autopilot-pe.log'
Start-Transcript -Path $transcript -Append -Force | Out-Null

try {
    Write-Host '========================================'
    Write-Host 'Autopilot PE bootstrap'
    Write-Host '========================================'

    $configPath = 'X:\autopilot\Bootstrap.json'
    if (-not (Test-Path $configPath)) {
        throw "Bootstrap.json not found at $configPath"
    }
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    Write-Host "Orchestrator URL: $($config.orchestratorUrl)"

    Write-Host 'Wait-PeNetwork...'
    $ip = Wait-PeNetwork -TimeoutSeconds $config.networkTimeoutSec
    Write-Host "Got IP: $ip"

    Write-Host 'Initialize-SshHostKeys...'
    Initialize-SshHostKeys

    $identity = Get-PeIdentity
    Write-Host "Identity: $($identity.Uuid) (vendor=$($identity.Vendor) name=$($identity.Name))"

    Write-Host 'Fetching manifest...'
    $manifest = Invoke-Manifest -OrchestratorUrl $config.orchestratorUrl -VmUuid $identity.Uuid `
        -RetryCount $config.manifestRetries -RetryBackoffSeconds $config.manifestRetryBackoffSec
    Write-Host "Manifest: $($manifest.steps.Count) steps, onError=$($manifest.onError)"

    Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl $config.orchestratorUrl -VmUuid $identity.Uuid

    # Stage payload files to target so first-boot scripts can reach the orchestrator
    $targetPayload = 'W:\autopilot'
    if (-not (Test-Path $targetPayload)) { New-Item -ItemType Directory -Path $targetPayload -Force | Out-Null }
    Copy-Item 'X:\autopilot\Bootstrap.json' "$targetPayload\" -Force
    Copy-Item 'X:\autopilot\Collect-HardwareHash.ps1' "$targetPayload\" -Force -ErrorAction SilentlyContinue
    Write-Host "Staged first-boot payload to $targetPayload"

    Write-Host 'Bootstrap complete.'
} catch {
    Write-Host "Bootstrap FAILED: $_" -ForegroundColor Red
    & 'X:\autopilot\Debug.ps1'
} finally {
    Stop-Transcript | Out-Null
}
