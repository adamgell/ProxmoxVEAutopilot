function Set-ProxmoxDiskSerial {
    <#
    .SYNOPSIS
        Adds or replaces the serial property on a Proxmox disk config string
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$DiskConfig,

        [parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9._-]+$')]
        [string]$Serial
    )

    $diskParts = @(
        $DiskConfig -split ',' |
            Where-Object { $_ -and $_ -notmatch '^serial=' }
    )

    if (-not $diskParts) {
        throw "Disk config '$DiskConfig' could not be parsed for serial injection."
    }

    $diskParts += "serial=$Serial"
    return ($diskParts -join ',')
}
