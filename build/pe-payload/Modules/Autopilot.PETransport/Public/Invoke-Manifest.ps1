function Invoke-Manifest {
    <#
    .SYNOPSIS
        Fetch /winpe/manifest/<vm-uuid> and return the parsed manifest object.
        Retries on transient failure.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $VmUuid,
        [int] $RetryCount = 3,
        [int] $RetryBackoffSeconds = 5
    )

    $url = "$($OrchestratorUrl.TrimEnd('/'))/winpe/manifest/$VmUuid"
    $lastErr = $null
    for ($attempt = 1; $attempt -le $RetryCount; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $url -Method GET -ErrorAction Stop
        } catch {
            $lastErr = $_
            if ($attempt -lt $RetryCount) {
                Start-Sleep -Seconds $RetryBackoffSeconds
            }
        }
    }
    throw "Invoke-Manifest: $RetryCount attempts to GET $url all failed. Last error: $lastErr"
}
