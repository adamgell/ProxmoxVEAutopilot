function Wait-PeNetwork {
    <#
    .SYNOPSIS
        Block until PE has a non-APIPA IPv4 address. Calls wpeutil InitializeNetwork
        on each retry to work around NetKVM driver not binding in time for wpeinit.

    .OUTPUTS
        The first non-APIPA IPv4 address found.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [int] $TimeoutSeconds = 60,
        [int] $PollIntervalSeconds = 3
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        wpeutil InitializeNetwork 2>$null | Out-Null

        # Parse ipconfig — Get-NetIPAddress isn't available in WinPE
        $output = ipconfig 2>$null
        foreach ($line in $output) {
            if ($line -match 'IPv4 Address.*:\s*([\d\.]+)') {
                $ip = $Matches[1]
                if ($ip -notlike '169.254.*' -and $ip -ne '127.0.0.1') {
                    return $ip
                }
            }
        }
        if ($PollIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $PollIntervalSeconds
        }
    }
    throw "Wait-PeNetwork: timeout after $TimeoutSeconds seconds — no non-APIPA IPv4 address found"
}
