function Publish-ProxmoxHashCaptureScript {
    <#
    .SYNOPSIS
        Pushes the hardware hash capture script into a Proxmox VM via guest agent

    .DESCRIPTION
        Writes a PowerShell script into the guest VM that captures the Windows Autopilot
        hardware hash and saves it as a CSV file. The script uses Get-WindowsAutopilotInfo
        or WMI directly to capture the device hardware hash.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.

    .PARAMETER Node
        Proxmox node the VM is on.

    .PARAMETER Vmid
        VM ID to push the script into.

    .PARAMETER OutputPath
        Guest-side path where the hash CSV will be saved.
        Default: C:\ProgramData\APHVTools\HardwareHashes

    .PARAMETER GroupTag
        Optional Autopilot group tag to include in the generated CSV.
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
        [string]$OutputPath = 'C:\ProgramData\APHVTools\HardwareHashes',

        [parameter(Mandatory = $false)]
        [string]$GroupTag
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    $bundledScript = Join-Path $PSScriptRoot '..\..\Get-WindowsAutopilotInfo.ps1'
    if (-not (Test-Path $bundledScript)) {
        throw "Get-WindowsAutopilotInfo.ps1 not found at '$bundledScript'."
    }

    $autopilotInfoScript = Get-Content -Path $bundledScript -Raw -ErrorAction Stop

    # PowerShell script to capture hardware hash inside the guest
    $captureScript = @'
# APHVTools Hardware Hash Capture Script
$ErrorActionPreference = 'Stop'
$outputDir = '__OUTPUT_PATH__'

if (-not (Test-Path $outputDir)) {
    New-Item -Path $outputDir -ItemType Directory -Force | Out-Null
}

$serial = (Get-CimInstance -Class Win32_BIOS).SerialNumber
if (-not $serial -or $serial -eq 'None') {
    $serial = (Get-CimInstance -Class Win32_ComputerSystemProduct).UUID
}
$serial = $serial -replace '[^\w\-]', '_'

$outputFile = Join-Path $outputDir "${serial}_hwid.csv"
$autopilotInfoScript = 'C:\ProgramData\APHVTools\Get-WindowsAutopilotInfo.ps1'
if (Test-Path $outputFile) {
    Remove-Item -Path $outputFile -Force
}

if ('__GROUP_TAG__' -ne '') {
    & $autopilotInfoScript -OutputFile $outputFile -GroupTag '__GROUP_TAG__'
}
else {
    & $autopilotInfoScript -OutputFile $outputFile
}

if (-not (Test-Path $outputFile)) {
    throw "Expected hardware hash CSV was not created at $outputFile"
}

$outputFileInfo = Get-Item -Path $outputFile
if ($outputFileInfo.Length -le 0) {
    throw "Hardware hash CSV was created at $outputFile but is empty"
}

Write-Output "HASH_CAPTURED:$outputFile"
'@

    $captureScript = $captureScript.Replace('__OUTPUT_PATH__', $OutputPath)
    $captureScript = $captureScript.Replace('__GROUP_TAG__', ($GroupTag ?? '').Replace("'", "''"))
    $guestScriptPath = 'C:\ProgramData\APHVTools\CaptureHash.ps1'
    $guestAutopilotInfoScriptPath = 'C:\ProgramData\APHVTools\Get-WindowsAutopilotInfo.ps1'

    Write-Host "  Publishing hash capture script to VM $Vmid.. " -ForegroundColor Cyan -NoNewline

    try {
        # Ensure directory exists
        $null = Invoke-ProxmoxGuestCommand -PveTicket $PveTicket -Node $Node -Vmid $Vmid `
            -Command @('cmd.exe', '/c', 'mkdir', 'C:\ProgramData\APHVTools\HardwareHashes') `
            -TimeoutSeconds 30 -ErrorAction SilentlyContinue

        # Copy the bundled Get-WindowsAutopilotInfo script into the guest
        $autopilotInfoWriteResult = New-PveNodesQemuAgentFileWrite -PveTicket $PveTicket `
            -Node $Node -Vmid $Vmid `
            -File $guestAutopilotInfoScriptPath -Content $autopilotInfoScript

        if (-not $autopilotInfoWriteResult.IsSuccessStatusCode) {
            throw "Get-WindowsAutopilotInfo.ps1 write failed: $($autopilotInfoWriteResult.ReasonPhrase)"
        }

        # Write the capture wrapper script
        $writeResult = New-PveNodesQemuAgentFileWrite -PveTicket $PveTicket `
            -Node $Node -Vmid $Vmid `
            -File $guestScriptPath -Content $captureScript

        if (-not $writeResult.IsSuccessStatusCode) {
            throw "Capture wrapper write failed: $($writeResult.ReasonPhrase)"
        }

        Write-Host $script:tick -ForegroundColor Green
        return $guestScriptPath
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        Write-Warning "Failed to publish hash capture script to VM $Vmid`: $_"
        return $null
    }
}
