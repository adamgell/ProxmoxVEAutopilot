function Wait-PeNetwork {
    <#
    .SYNOPSIS
        Block until PE has a non-APIPA IPv4 address. Calls wpeutil InitializeNetwork
        on each retry to work around Plan 1 KNOWN-ISSUES #1 (NetKVM driver doesn't
        bind in time for wpeinit's first network init).

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
        wpeutil InitializeNetwork | Out-Null

        $ipv4 = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object IPAddress -notlike '169.254.*' |
            Where-Object IPAddress -ne '127.0.0.1')
        if ($ipv4.Count -gt 0) {
            return $ipv4[0].IPAddress
        }
        if ($PollIntervalSeconds -gt 0) {
            Start-Sleep -Seconds $PollIntervalSeconds
        }
    }
    throw "Wait-PeNetwork: timeout after $TimeoutSeconds seconds — no non-APIPA IPv4 address found"
}
