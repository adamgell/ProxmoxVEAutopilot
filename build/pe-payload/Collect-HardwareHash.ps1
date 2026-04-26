<#
.SYNOPSIS
    Collect Autopilot hardware hash and POST to orchestrator.
    Runs as a scheduled task on first boot (specialize pass).
    Falls back to writing CSV locally if the POST fails.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$logPath = "$env:SystemDrive\Windows\Temp\autopilot-hwid.log"
Start-Transcript -Path $logPath -Force | Out-Null

try {
    $configPath = "$env:SystemDrive\autopilot\Bootstrap.json"
    $orchestratorUrl = $null
    $vmUuid = $null

    if (Test-Path $configPath) {
        $config = Get-Content $configPath -Raw | ConvertFrom-Json
        $orchestratorUrl = $config.orchestratorUrl
    }

    $cs = Get-CimInstance Win32_ComputerSystemProduct
    $vmUuid = $cs.UUID
    $manufacturer = $cs.Vendor
    $model = $cs.Name
    $serial = $cs.IdentifyingNumber

    Write-Host "UUID: $vmUuid"
    Write-Host "Manufacturer: $manufacturer"
    Write-Host "Model: $model"
    Write-Host "Serial: $serial"

    # Collect hardware hash via MDM bridge
    $hardwareHash = ""
    try {
        $devDetail = Get-CimInstance -Namespace root/cimv2/mdm/dmmap `
            -Class MDM_DevDetail_Ext01 `
            -Filter "InstanceID='Ext' AND ParentID='./DevDetail'" `
            -ErrorAction Stop
        $hardwareHash = $devDetail.DeviceHardwareData
        Write-Host "Hardware hash length: $($hardwareHash.Length)"
    } catch {
        Write-Host "MDM bridge unavailable (expected in VMs): $_"
    }

    $timestamp = (Get-Date).ToUniversalTime().ToString('o')

    # Try POST to orchestrator
    $posted = $false
    if ($orchestratorUrl) {
        $body = @{
            vmUuid       = $vmUuid
            serial       = $serial
            hardwareHash = $hardwareHash
            manufacturer = $manufacturer
            model        = $model
            timestamp    = $timestamp
        } | ConvertTo-Json

        try {
            $null = Invoke-RestMethod -Uri "$orchestratorUrl/winpe/hwid" `
                -Method POST -ContentType 'application/json' -Body $body `
                -TimeoutSec 15
            Write-Host "POSTed hardware hash to $orchestratorUrl/winpe/hwid"
            $posted = $true
        } catch {
            Write-Host "POST failed (will write local CSV): $_"
        }
    }

    # Fallback: write CSV locally
    $csvPath = "$env:SystemDrive\Windows\Temp\autopilot-hwid.csv"
    $row = [PSCustomObject]@{
        'Device Serial Number'   = $serial
        'Windows Product ID'     = ''
        'Hardware Hash'          = $hardwareHash
        'Manufacturer'           = $manufacturer
        'Model'                  = $model
        'UUID'                   = $vmUuid
        'Timestamp'              = $timestamp
        'PostedToOrchestrator'   = $posted
    }
    $row | Export-Csv -Path $csvPath -NoTypeInformation -Force
    Write-Host "Wrote fallback CSV: $csvPath"

} catch {
    Write-Host "Collect-HardwareHash FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
}
