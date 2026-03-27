function Get-ProxmoxClientVMHardwareHash {
    <#
    .SYNOPSIS
        Captures an Autopilot hardware hash from an existing Proxmox VM.

    .DESCRIPTION
        Connects to Proxmox, resolves the target VM by VMID when needed, waits
        for the QEMU guest agent, and captures the hardware hash into the
        tenant's local HardwareHashes folder.

    .PARAMETER TenantName
        Tenant workspace name used to resolve the local HardwareHashes path.

    .PARAMETER Vmid
        Existing Proxmox VM ID to target.

    .PARAMETER Node
        Optional Proxmox node. If omitted, the VM is resolved from the cluster.

    .PARAMETER VMName
        Optional VM name. If omitted, the VM name is resolved from the cluster.

    .PARAMETER GuestAgentTimeoutSeconds
        Maximum time to wait for the guest agent before giving up.

    .PARAMETER PollIntervalSeconds
        Seconds between guest-agent readiness checks.

    .PARAMETER GroupTag
        Optional Autopilot group tag to include in the captured CSV.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$TenantName,

        [parameter(Mandatory = $true)]
        [ValidateRange(1, 999999999)]
        [int]$Vmid,

        [parameter(Mandatory = $false)]
        [string]$Node,

        [parameter(Mandatory = $false)]
        [string]$VMName,

        [parameter(Mandatory = $false)]
        [ValidateRange(1, 86400)]
        [int]$GuestAgentTimeoutSeconds = 1800,

        [parameter(Mandatory = $false)]
        [ValidateRange(1, 300)]
        [int]$PollIntervalSeconds = 10,

        [parameter(Mandatory = $false)]
        [string]$GroupTag
    )

    if (-not $script:hvConfig) {
        throw "APHVTools is not initialized. Run Initialize-APHVTools first."
    }

    if (-not $script:hvConfig.vmPath) {
        throw "No vmPath found in APHVTools config. Run Initialize-APHVTools first."
    }

    $tenantConfig = @($script:hvConfig.tenantConfig) | Where-Object { $_.TenantName -eq $TenantName } | Select-Object -First 1
    if (-not $tenantConfig) {
        throw "Tenant '$TenantName' not found in APHVTools config. Run Add-TenantToConfig first."
    }

    $clientPath = Join-Path $script:hvConfig.vmPath $TenantName
    if (-not (Test-Path $clientPath)) {
        New-Item -Path $clientPath -ItemType Directory -Force | Out-Null
    }

    $ticket = Connect-ProxmoxHost

    if (-not $Node -or -not $VMName) {
        $vmLookup = Invoke-PveRestApi -PveTicket $ticket -Resource '/cluster/resources?type=vm' -Method Get
        if (-not $vmLookup.IsSuccessStatusCode) {
            throw "Failed to query Proxmox cluster resources: $($vmLookup.ReasonPhrase)"
        }

        $vmInfo = @($vmLookup.Response.data) |
            Where-Object { $_.type -eq 'qemu' -and [int]$_.vmid -eq $Vmid } |
            Select-Object -First 1

        if (-not $vmInfo) {
            throw "VMID $Vmid was not found in Proxmox cluster resources."
        }

        if (-not $Node) {
            $Node = $vmInfo.node
        }
        if (-not $VMName) {
            $VMName = if ($vmInfo.name) { [string]$vmInfo.name } else { "vm-$Vmid" }
        }
    }

    Write-Host "`n---- Existing VM Hardware Hash Capture ----" -ForegroundColor Green
    Write-Host "Targeting existing VM '$VMName' (VMID: $Vmid) on node '$Node'." -ForegroundColor Cyan
    Write-Host "Polling Proxmox guest-ping every $PollIntervalSeconds seconds until the guest agent responds." -ForegroundColor DarkGray

    $agentReady = Wait-ProxmoxGuestAgent -PveTicket $ticket -Node $Node -Vmid $Vmid -VMName $VMName `
        -TimeoutSeconds $GuestAgentTimeoutSeconds -PollIntervalSeconds $PollIntervalSeconds

    if (-not $agentReady) {
        return $null
    }

    return Get-ProxmoxVMHardwareHash -PveTicket $ticket -Node $Node -Vmid $Vmid -VMName $VMName -ClientPath $clientPath -GroupTag $GroupTag
}
