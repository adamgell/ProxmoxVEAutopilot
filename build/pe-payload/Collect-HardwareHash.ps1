<#
.SYNOPSIS
    Collect Autopilot hardware hash, POST to orchestrator, verify delivery.
    Runs in Audit Mode via auditUser RunSynchronous.
    Always writes CSV locally. Blocks until POST confirmed or retries exhausted.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$logPath = "$env:SystemDrive\Windows\Temp\autopilot-hwid.log"
Start-Transcript -Path $logPath -Force | Out-Null

# Wait for network stack to be fully up
Write-Host "Waiting for network..."
$deadline = (Get-Date).AddSeconds(60)
$hasNetwork = $false
while ((Get-Date) -lt $deadline) {
    try {
        $ip = (ipconfig | Select-String 'IPv4 Address.*?:\s*([\d\.]+)' | ForEach-Object { $_.Matches[0].Groups[1].Value }) |
            Where-Object { $_ -notlike '169.254.*' -and $_ -ne '127.0.0.1' } | Select-Object -First 1
        if ($ip) {
            Write-Host "Network ready: $ip"
            $hasNetwork = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 3
}
if (-not $hasNetwork) { Write-Host "WARNING: No network — POST will fail, CSV only" }

try {
    $configPath = "$env:SystemDrive\autopilot\Bootstrap.json"
    $orchestratorUrl = $null

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

    # Always write CSV first
    $csvPath = "$env:SystemDrive\Windows\Temp\autopilot-hwid.csv"
    $row = [PSCustomObject]@{
        'Device Serial Number'   = $serial
        'Windows Product ID'     = ''
        'Hardware Hash'          = $hardwareHash
        'Manufacturer'           = $manufacturer
        'Model'                  = $model
        'UUID'                   = $vmUuid
        'Timestamp'              = $timestamp
        'PostedToOrchestrator'   = $false
    }
    $row | Export-Csv -Path $csvPath -NoTypeInformation -Force
    Write-Host "Wrote CSV: $csvPath"

    # POST to orchestrator with retries
    $posted = $false
    if ($orchestratorUrl -and $hasNetwork) {
        $body = @{
            vmUuid       = $vmUuid
            serial       = $serial
            hardwareHash = $hardwareHash
            manufacturer = $manufacturer
            model        = $model
            timestamp    = $timestamp
        } | ConvertTo-Json

        for ($attempt = 1; $attempt -le 5; $attempt++) {
            try {
                $null = Invoke-RestMethod -Uri "$orchestratorUrl/winpe/hwid" `
                    -Method POST -ContentType 'application/json' -Body $body `
                    -TimeoutSec 30
                Write-Host "POSTed hardware hash to $orchestratorUrl/winpe/hwid"
                $posted = $true
                break
            } catch {
                Write-Host "POST attempt $attempt/5 failed: $_"
                if ($attempt -lt 5) { Start-Sleep -Seconds ($attempt * 5) }
            }
        }
    }

    # Update CSV with POST status
    $row.PostedToOrchestrator = $posted
    $row | Export-Csv -Path $csvPath -NoTypeInformation -Force

    if ($posted) {
        Write-Host "Hardware hash delivered to orchestrator."
    } else {
        Write-Host "WARNING: POST failed after all retries. CSV available at $csvPath"
        Write-Host "The orchestrator can pull this CSV via SSH or file share."
    }

} catch {
    Write-Host "Collect-HardwareHash FAILED: $_"
} finally {
    Stop-Transcript | Out-Null
}
