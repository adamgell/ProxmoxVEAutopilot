<#
.SYNOPSIS
    Collect Autopilot hardware hash, POST to orchestrator, verify delivery.
    Runs in Audit Mode via auditUser RunSynchronous.
    Sends checkins at each phase for operator UI feedback.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$logPath = "$env:SystemDrive\Windows\Temp\autopilot-hwid.log"
Start-Transcript -Path $logPath -Force | Out-Null

$configPath = "$env:SystemDrive\autopilot\Bootstrap.json"
$orchestratorUrl = $null
$vmUuid = $null

if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    $orchestratorUrl = $config.orchestratorUrl
}

function Send-Status {
    param([string]$StepId, [string]$Status, [string]$LogTail = '', [string]$ErrorMessage = $null)
    if (-not $orchestratorUrl -or -not $vmUuid) { return }
    $body = @{
        vmUuid       = $vmUuid
        stepId       = $StepId
        status       = $Status
        timestamp    = (Get-Date).ToUniversalTime().ToString('o')
        durationSec  = 0.0
        logTail      = $LogTail
        errorMessage = $ErrorMessage
        extra        = @{}
    } | ConvertTo-Json
    try {
        $null = Invoke-RestMethod -Uri "$orchestratorUrl/winpe/checkin" `
            -Method POST -ContentType 'application/json' -Body $body -TimeoutSec 5
    } catch {}
}

# Get identity early for checkins
try {
    $cs = Get-CimInstance Win32_ComputerSystemProduct
    $vmUuid = $cs.UUID
} catch {}

Send-Status -StepId 'audit-network' -Status 'starting' -LogTail 'Waiting for network...'
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
if ($hasNetwork) {
    Send-Status -StepId 'audit-network' -Status 'ok' -LogTail "IP: $ip"
} else {
    Send-Status -StepId 'audit-network' -Status 'error' -LogTail 'No network' -ErrorMessage 'No network after 60s'
    Write-Host "WARNING: No network — POST will fail, CSV only"
}

try {
    $manufacturer = $cs.Vendor
    $model = $cs.Name
    $serial = $cs.IdentifyingNumber

    Write-Host "UUID: $vmUuid"
    Write-Host "Manufacturer: $manufacturer"
    Write-Host "Model: $model"
    Write-Host "Serial: $serial"

    Send-Status -StepId 'audit-hash' -Status 'starting' -LogTail 'Collecting hardware hash...'
    $hardwareHash = ""
    try {
        $devDetail = Get-CimInstance -Namespace root/cimv2/mdm/dmmap `
            -Class MDM_DevDetail_Ext01 `
            -Filter "InstanceID='Ext' AND ParentID='./DevDetail'" `
            -ErrorAction Stop
        $hardwareHash = $devDetail.DeviceHardwareData
        Write-Host "Hardware hash length: $($hardwareHash.Length)"
        Send-Status -StepId 'audit-hash' -Status 'ok' -LogTail "Hash length: $($hardwareHash.Length)"
    } catch {
        Write-Host "MDM bridge unavailable (expected in VMs): $_"
        Send-Status -StepId 'audit-hash' -Status 'ok' -LogTail "MDM unavailable (VM) — hash empty"
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

    # POST to orchestrator
    Send-Status -StepId 'audit-post' -Status 'starting' -LogTail 'POSTing hash to orchestrator...'
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
                Send-Status -StepId 'audit-post' -Status 'starting' -LogTail "POST attempt $attempt/5 failed"
                if ($attempt -lt 5) { Start-Sleep -Seconds ($attempt * 5) }
            }
        }
    }

    # Update CSV
    $row.PostedToOrchestrator = $posted
    $row | Export-Csv -Path $csvPath -NoTypeInformation -Force

    if ($posted) {
        Send-Status -StepId 'audit-post' -Status 'ok' -LogTail 'Hash delivered to orchestrator'
        Write-Host "Hardware hash delivered to orchestrator."
    } else {
        Send-Status -StepId 'audit-post' -Status 'error' -LogTail "POST failed — CSV at $csvPath" -ErrorMessage 'POST failed after retries'
        Write-Host "WARNING: POST failed. CSV available at $csvPath"
    }

    Send-Status -StepId 'audit-sysprep' -Status 'starting' -LogTail 'Preparing sysprep /oobe /shutdown...'
    Write-Host "Sysprep will run next (from unattend RunSynchronous)."

} catch {
    Write-Host "Collect-HardwareHash FAILED: $_"
    Send-Status -StepId 'audit-hash' -Status 'error' -LogTail "FAILED: $_" -ErrorMessage "$_"
} finally {
    Stop-Transcript | Out-Null
}
