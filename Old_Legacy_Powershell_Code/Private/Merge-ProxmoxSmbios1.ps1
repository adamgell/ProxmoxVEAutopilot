function Merge-ProxmoxSmbios1 {
    <#
    .SYNOPSIS
        Builds or augments a Proxmox SMBIOS type 1 string

    .DESCRIPTION
        Creates a SMBIOS type 1 config string from discrete OEM fields or augments an
        existing raw string. This is used to ensure APHVTools can consistently set a
        non-zero per-VM UUID without duplicating SMBIOS assembly logic.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        [string]$Smbios1,

        [parameter(Mandatory = $false)]
        [string]$Manufacturer,

        [parameter(Mandatory = $false)]
        [string]$Product,

        [parameter(Mandatory = $false)]
        [string]$Family,

        [parameter(Mandatory = $false)]
        [string]$SerialNumber,

        [parameter(Mandatory = $false)]
        [string]$SKU,

        [parameter(Mandatory = $false)]
        [ValidatePattern('^[0-9a-fA-F-]{36}$')]
        [string]$Uuid
    )

    if ($Smbios1) {
        $smbiosParts = @(
            $Smbios1 -split ',' |
                Where-Object { $_ -and $_ -notmatch '^uuid=' }
        )

        if ($Uuid) {
            $smbiosParts += "uuid=$Uuid"
        }

        return ($smbiosParts -join ',')
    }

    $hasOemFields = $Manufacturer -or $Product -or $Family -or $SerialNumber -or $SKU
    if (-not $hasOemFields -and -not $Uuid) {
        return $null
    }

    $smbiosParts = @()
    if ($hasOemFields) {
        $smbiosParts += 'base64=1'
        if ($Manufacturer) { $smbiosParts += "manufacturer=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Manufacturer)))" }
        if ($Product) { $smbiosParts += "product=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Product)))" }
        if ($Family) { $smbiosParts += "family=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Family)))" }
        if ($SerialNumber) { $smbiosParts += "serial=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($SerialNumber)))" }
        if ($SKU) { $smbiosParts += "sku=$([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($SKU)))" }
    }

    if ($Uuid) {
        $smbiosParts += "uuid=$Uuid"
    }

    return ($smbiosParts -join ',')
}
