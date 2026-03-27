function Get-ProxmoxVMHardwareHash {
    <#
    .SYNOPSIS
        Captures and retrieves the hardware hash from a Proxmox VM via guest agent

    .DESCRIPTION
        Executes the hash capture script inside the guest VM, waits for completion,
        then reads back the resulting CSV file via the guest agent file-read API.
        Saves the CSV to the host-side tenant path for later Autopilot import.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.

    .PARAMETER Node
        Proxmox node the VM is on.

    .PARAMETER Vmid
        VM ID to capture the hash from.

    .PARAMETER VMName
        VM name (used for local file naming).

    .PARAMETER ClientPath
        Host-side directory to save the hash CSV.

    .PARAMETER TimeoutSeconds
        Maximum time to wait for hash capture. Default: 600 seconds.

    .PARAMETER GroupTag
        Optional Autopilot group tag to include in the captured CSV.
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
        [string]$VMName,

        [parameter(Mandatory = $true)]
        [string]$ClientPath,

        [parameter(Mandatory = $false)]
        [int]$TimeoutSeconds = 600,

        [parameter(Mandatory = $false)]
        [string]$GroupTag
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    Write-Host "  Capturing hardware hash from VM '$VMName' (VMID: $Vmid).. " -ForegroundColor Cyan -NoNewline

    try {
        # Ensure the capture script is published
        $scriptPath = Publish-ProxmoxHashCaptureScript -PveTicket $PveTicket -Node $Node -Vmid $Vmid -GroupTag $GroupTag
        if (-not $scriptPath) {
            throw "Failed to publish hash capture script"
        }

        # Proxmox guest-agent exec accepts a direct PowerShell -Command invocation
        # more reliably than -File for this path-based guest script execution.
        $scriptPath = $scriptPath.Trim()
        $commandText = "& '$scriptPath'"

        # Execute the capture script inside the guest
        $result = Invoke-ProxmoxGuestCommand -PveTicket $PveTicket -Node $Node -Vmid $Vmid `
            -Command @('powershell.exe', '-ExecutionPolicy', 'Bypass', '-Command', $commandText) `
            -TimeoutSeconds $TimeoutSeconds

        if ($result.ExitCode -ne 0) {
            throw "Hash capture script failed with exit code $($result.ExitCode). Stderr: $($result.Stderr)"
        }

        # Parse output to find the CSV path
        $capturedFile = $null
        if ($result.Stdout -match 'HASH_CAPTURED:(.+)') {
            $capturedFile = $Matches[1].Trim()
        }

        if (-not $capturedFile) {
            throw "Hash capture script did not report a captured file. Stdout: $($result.Stdout)"
        }

        # Read the CSV via guest exec so PowerShell in the guest normalizes the
        # file encoding before we persist it locally.
        $csvReadCommand = "Get-Content -Path '$($capturedFile.Replace("'", "''"))' -Raw"
        $csvReadResult = Invoke-ProxmoxGuestCommand -PveTicket $PveTicket -Node $Node -Vmid $Vmid `
            -Command @('powershell.exe', '-ExecutionPolicy', 'Bypass', '-Command', $csvReadCommand) `
            -TimeoutSeconds 60

        if ($csvReadResult.ExitCode -ne 0) {
            throw "Failed to read hash file from guest. Stderr: $($csvReadResult.Stderr)"
        }

        $csvContent = $csvReadResult.Stdout
        if ([string]::IsNullOrWhiteSpace($csvContent)) {
            throw "Hash CSV '$capturedFile' was read successfully but contained no data."
        }

        # Save locally
        $hashDir = Join-Path $ClientPath 'HardwareHashes'
        if (-not (Test-Path $hashDir)) {
            New-Item -Path $hashDir -ItemType Directory -Force | Out-Null
        }

        $localFile = Join-Path $hashDir "${VMName}_hwid.csv"
        Set-Content -Path $localFile -Value $csvContent -Encoding utf8 -Force

        Write-Host $script:tick -ForegroundColor Green
        Write-Host "  Hardware hash saved to: $localFile" -ForegroundColor Green

        return [PSCustomObject]@{
            VMName   = $VMName
            Vmid     = $Vmid
            HashFile = $localFile
            Content  = $csvContent
        }
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        Write-Warning "Failed to capture hardware hash from VM '$VMName' (VMID: $Vmid): $_"
        return $null
    }
}
