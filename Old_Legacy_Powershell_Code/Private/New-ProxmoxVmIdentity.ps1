function New-ProxmoxVmIdentity {
    <#
    .SYNOPSIS
        Generates stable identity values for a new Proxmox VM

    .DESCRIPTION
        Produces a per-VM SMBIOS UUID and primary disk serial that APHVTools can write
        into Proxmox config during creation or cloning. These values are generated once
        at provisioning time and then persist with the VM configuration.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $true)]
        [ValidateRange(1, 999999999)]
        [int]$Vmid
    )

    $vmUuid = [System.Guid]::NewGuid().ToString().ToUpperInvariant()
    $uuidToken = $vmUuid.Replace('-', '')
    $diskSerial = "APHV{0}{1}" -f ('{0:D6}' -f $Vmid), $uuidToken.Substring(0, 10)

    return [PSCustomObject]@{
        Uuid       = $vmUuid
        DiskSerial = $diskSerial
    }
}
