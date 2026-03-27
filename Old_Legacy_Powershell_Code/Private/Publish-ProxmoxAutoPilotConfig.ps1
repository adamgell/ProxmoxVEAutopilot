function Publish-ProxmoxAutoPilotConfig {
    <#
    .SYNOPSIS
        Pushes AutopilotConfigurationFile.json into a Proxmox VM via guest agent

    .DESCRIPTION
        Reads the Autopilot configuration JSON from the host filesystem and writes it
        to the Windows Autopilot provisioning directory inside the guest VM using the
        QEMU guest agent file-write API.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.

    .PARAMETER Node
        Proxmox node the VM is on.

    .PARAMETER Vmid
        VM ID to write the config into.

    .PARAMETER ClientPath
        Host-side directory containing AutopilotConfigurationFile.json.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        $PveTicket,

        [parameter(Mandatory = $true)]
        [string]$Node,

        [parameter(Mandatory = $true)]
        [int]$Vmid,

        [parameter(Mandatory = $true)]
        [string]$ClientPath
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    $autopilotFile = Join-Path $ClientPath 'AutopilotConfigurationFile.json'
    if (-not (Test-Path $autopilotFile)) {
        Write-Warning "AutopilotConfigurationFile.json not found at '$autopilotFile'. Skipping Autopilot injection."
        return $false
    }

    $content = Get-Content -Path $autopilotFile -Raw -ErrorAction Stop
    $guestPath = 'C:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json'

    Write-Host "  Injecting Autopilot config into VM $Vmid.. " -ForegroundColor Cyan -NoNewline

    try {
        # Ensure the directory exists in the guest
        Invoke-ProxmoxGuestCommand -PveTicket $PveTicket -Node $Node -Vmid $Vmid `
            -Command @('cmd.exe', '/c', 'mkdir', 'C:\Windows\Provisioning\Autopilot') `
            -TimeoutSeconds 30 -ErrorAction SilentlyContinue

        # Write the file
        $writeResult = New-PveNodesQemuAgentFileWrite -PveTicket $PveTicket `
            -Node $Node -Vmid $Vmid `
            -File $guestPath -Content $content

        if (-not $writeResult.IsSuccessStatusCode) {
            throw "File write failed: $($writeResult.ReasonPhrase)"
        }

        Write-Host $script:tick -ForegroundColor Green
        return $true
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        Write-Warning "Failed to inject Autopilot config into VM $Vmid`: $_"
        return $false
    }
}
