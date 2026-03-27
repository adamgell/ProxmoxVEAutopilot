function Wait-ProxmoxGuestAgent {
    <#
    .SYNOPSIS
        Waits for the Proxmox QEMU guest agent to respond for a VM.

    .DESCRIPTION
        Polls the Proxmox guest-ping endpoint until the guest agent responds or
        the timeout expires. Writes visible progress so long Windows setup phases
        do not look like the script is hung, while keeping transcript output easy
        to read outside an interactive terminal.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.

    .PARAMETER Node
        Proxmox node the VM is on.

    .PARAMETER Vmid
        VM ID to check.

    .PARAMETER VMName
        Optional VM name for friendlier status output.

    .PARAMETER TimeoutSeconds
        Maximum time to wait for the guest agent. Default: 1800 seconds.

    .PARAMETER PollIntervalSeconds
        Seconds between guest-ping checks. Default: 10 seconds.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        $PveTicket,

        [parameter(Mandatory = $true)]
        [string]$Node,

        [parameter(Mandatory = $true)]
        [int]$Vmid,

        [parameter(Mandatory = $false)]
        [string]$VMName,

        [parameter(Mandatory = $false)]
        [ValidateRange(1, 86400)]
        [int]$TimeoutSeconds = 1800,

        [parameter(Mandatory = $false)]
        [ValidateRange(1, 300)]
        [int]$PollIntervalSeconds = 10
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    $agentReady = $false
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $checkNumber = 0
    $vmLabel = if ($VMName) { "'$VMName' (VMID: $Vmid)" } else { "VMID $Vmid" }

    while ((Get-Date) -lt $deadline) {
        $checkNumber++
        $remainingSeconds = [Math]::Max([int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds), 0)
        Write-Host "  Guest agent check #$checkNumber for $vmLabel (timeout in ${remainingSeconds}s)..." -ForegroundColor DarkGray

        try {
            $pingResult = New-PveNodesQemuAgentPing -PveTicket $PveTicket -Node $Node -Vmid $Vmid
            if ($pingResult.IsSuccessStatusCode) {
                $agentReady = $true
                break
            }
        }
        catch {
            Write-Verbose ("Guest agent ping failed for {0}: {1}" -f $vmLabel, $_.Exception.Message)
        }

        $sleepSeconds = [Math]::Min($PollIntervalSeconds, [Math]::Max([int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds), 0))
        if ($sleepSeconds -gt 0) {
            $timeoutSecondsLeft = [Math]::Max([int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds), 0)
            Write-Host ("  Guest agent not ready yet. Waiting {0}s before the next check (timeout in {1}s)..." -f $sleepSeconds, $timeoutSecondsLeft) -ForegroundColor DarkGray
            Start-Sleep -Seconds $sleepSeconds
        }
    }

    if ($agentReady) {
        Write-Host "  Guest agent is ready for VM $vmLabel" -ForegroundColor Green
        return $true
    }

    Write-Warning "Guest agent did not respond within $TimeoutSeconds seconds for VM $vmLabel."
    return $false
}
