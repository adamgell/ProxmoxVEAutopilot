function New-ProxmoxSerialNumber {
    <#
    .SYNOPSIS
        Generates a manufacturer-specific serial number for Proxmox VMs

    .DESCRIPTION
        Creates serial numbers with manufacturer-appropriate prefixes to match
        real hardware patterns. If a custom serial is provided, returns it directly.

    .PARAMETER Manufacturer
        OEM manufacturer name. Used to select the serial prefix.

    .PARAMETER CustomSerial
        Optional custom serial number. If provided, returned as-is.

    .EXAMPLE
        New-ProxmoxSerialNumber -Manufacturer "Lenovo"
        # Returns: PF-A3B7C1D9

    .EXAMPLE
        New-ProxmoxSerialNumber -Manufacturer "Dell Inc."
        # Returns: SVC-E4F2A8B1

    .EXAMPLE
        New-ProxmoxSerialNumber -CustomSerial "MY-SERIAL-123"
        # Returns: MY-SERIAL-123
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        [string]$Manufacturer,

        [parameter(Mandatory = $false)]
        [string]$CustomSerial
    )

    if ($CustomSerial) {
        return $CustomSerial
    }

    # Generate 4 random bytes as hex
    $bytes = [byte[]]::new(4)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $hex = ($bytes | ForEach-Object { $_.ToString('X2') }) -join ''

    # Select prefix based on manufacturer
    $prefix = switch -Wildcard ($Manufacturer) {
        'Lenovo'                  { 'PF' }
        'Dell*'                   { 'SVC' }
        'HP'                      { 'CZC' }
        'Microsoft*'              { 'MSF' }
        default                   { 'LAB' }
    }

    return "$prefix-$hex"
}
