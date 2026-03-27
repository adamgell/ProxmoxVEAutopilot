function Get-NextProxmoxVmid {
    <#
    .SYNOPSIS
        Gets the next available VMID from the Proxmox cluster

    .DESCRIPTION
        Queries the Proxmox API for the next available VMID using the /cluster/nextid endpoint.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        $PveTicket
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    $response = Invoke-PveRestApi -PveTicket $PveTicket -Resource '/cluster/nextid' -Method Get
    if (-not $response.IsSuccessStatusCode) {
        throw "Failed to get next VMID: $($response.ReasonPhrase)"
    }

    return [int]$response.Response.data
}
