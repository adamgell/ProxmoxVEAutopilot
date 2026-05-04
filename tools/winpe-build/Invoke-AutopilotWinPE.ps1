# Invoke-AutopilotWinPE.ps1
#
# In-WinPE phase-0 agent. Boots a Proxmox VM into Windows by:
#   register -> capture_hash (M2) -> partition_disk -> apply_wim ->
#   inject_drivers -> validate_boot_drivers -> stage_autopilot_config ->
#   bake_boot_entry -> stage_unattend -> done -> reboot.
#
# Designed for PowerShell 5.1 (WinPE-bundled). Sourced by Pester tests
# during development; running it from startnet.cmd at WinPE boot drives
# the live flow.

Set-StrictMode -Version Latest

function Read-AgentConfig {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "config not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-AgentLog {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('DEBUG','INFO','WARN','ERROR')] [string] $Level,
        [Parameter(Mandatory)] [string] $Message
    )
    $ts = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fffK')
    $line = "$ts [$Level] $Message"
    Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-VMIdentity {
    param(
        [scriptblock] $UuidResolver = { (Get-CimInstance Win32_ComputerSystemProduct).UUID },
        [scriptblock] $MacResolver  = {
            (Get-NetAdapter -Physical |
                Where-Object Status -eq 'Up' |
                Sort-Object ifIndex |
                Select-Object -First 1).MacAddress
        }
    )
    $uuid = & $UuidResolver
    $mac  = & $MacResolver
    if ([string]::IsNullOrWhiteSpace($uuid)) { throw "could not read SMBIOS UUID" }
    if ([string]::IsNullOrWhiteSpace($mac))  { throw "could not read MAC address"  }
    return [pscustomobject]@{
        vm_uuid = $uuid.ToString().ToLowerInvariant()
        mac     = $mac.ToString()
    }
}

function Invoke-OrchestratorRequest {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string] $Method,
        [hashtable] $Body,
        [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [int] $MaxAttempts = 5,
        [int] $RetryDelayMs = 2000,
        [int] $TimeoutSec = 30,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    $payload = $null
    if ($Body) { $payload = $Body | ConvertTo-Json -Depth 10 -Compress }

    $bases = @($BaseUrl)
    if ($FallbackBaseUrl) { $bases += $FallbackBaseUrl }

    $lastErr = $null
    foreach ($base in $bases) {
        $uri = ($base.TrimEnd('/')) + '/' + $Path.TrimStart('/')
        for ($i = 1; $i -le $MaxAttempts; $i++) {
            try {
                return & $RestInvoker $uri $Method $headers $payload 'application/json' $TimeoutSec
            } catch {
                $lastErr = $_
                if ($i -lt $MaxAttempts) { Start-Sleep -Milliseconds $RetryDelayMs }
            }
        }
    }
    throw $lastErr
}

function _ReportStepState {
    param(
        [string] $BaseUrl, [string] $BearerToken, [int] $StepId,
        [string] $State, [string] $ErrorMessage,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker
    )
    $body = @{ state = $State }
    if ($ErrorMessage) { $body.error = $ErrorMessage }
    $reqArgs = @{
        BaseUrl = $BaseUrl
        Path = "/winpe/step/$StepId/result"
        Method = 'POST'
        Body = $body
        BearerToken = $BearerToken
        RestInvoker = $RestInvoker
    }
    if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
    $r = Invoke-OrchestratorRequest @reqArgs
    if ($r.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $r.bearer_token) {
        return $r.bearer_token
    }
    return $BearerToken
}

function Invoke-ActionLoop {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [object[]] $Actions,
        [Parameter(Mandatory)] [hashtable] $Handlers,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $token = $BearerToken
    foreach ($action in $Actions) {
        $kind = $action.kind
        $stepId = [int] $action.step_id
        if (-not $Handlers.ContainsKey($kind)) {
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' `
                -ErrorMessage "no handler for kind: $kind" `
                -FallbackBaseUrl $FallbackBaseUrl `
                -RestInvoker $RestInvoker
            throw "no handler registered for kind: $kind"
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'running' `
            -FallbackBaseUrl $FallbackBaseUrl `
            -RestInvoker $RestInvoker
        try {
            # Handlers receive the CURRENT token (post-running-refresh)
            # rather than capturing the original via closure, so a long
            # apply_wim followed by stage_unattend GETs use a fresh
            # token, not one that may have expired during the apply.
            & $Handlers[$kind] $action.params $token
        } catch {
            $msg = $_.Exception.Message
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' -ErrorMessage $msg `
                -FallbackBaseUrl $FallbackBaseUrl `
                -RestInvoker $RestInvoker
            throw
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'ok' `
            -FallbackBaseUrl $FallbackBaseUrl `
            -RestInvoker $RestInvoker
    }
    return $token
}
