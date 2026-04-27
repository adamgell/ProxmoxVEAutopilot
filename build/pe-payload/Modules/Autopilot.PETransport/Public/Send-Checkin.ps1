function Send-Checkin {
    <#
    .SYNOPSIS
        POST /winpe/checkin. Fire-and-forget: server unreachable does NOT
        bubble up to the caller — the bootstrap shouldn't die over a missed
        checkin.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $VmUuid,
        [Parameter(Mandatory)] [string] $StepId,
        [Parameter(Mandatory)] [ValidateSet('starting','ok','error')] [string] $Status,
        [Parameter(Mandatory)] [string] $Timestamp,
        [double] $DurationSec = 0.0,
        [string] $LogTail = '',
        [string] $ErrorMessage = $null,
        [hashtable] $Extra = @{}
    )

    $url = "$($OrchestratorUrl.TrimEnd('/'))/winpe/checkin"
    $payload = @{
        vmUuid       = $VmUuid
        stepId       = $StepId
        status       = $Status
        timestamp    = $Timestamp
        durationSec  = $DurationSec
        logTail      = $LogTail
        errorMessage = $ErrorMessage
        extra        = $Extra
    } | ConvertTo-Json -Depth 8 -Compress

    try {
        Invoke-RestMethod -Uri $url -Method POST -Body $payload -ContentType 'application/json' -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "Send-Checkin: POST $url failed (continuing): $_"
    }
}
