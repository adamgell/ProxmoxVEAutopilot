#!/usr/bin/env pwsh
# Upload hardware hash CSVs to Windows Autopilot via Microsoft Graph.
# Called by playbooks/upload_hashes.yml with env vars for credentials.

$ErrorActionPreference = 'Stop'

$appId     = $env:ENTRA_APP_ID
$tenantId  = $env:ENTRA_TENANT_ID
$appSecret = $env:ENTRA_APP_SECRET
$hashDir   = $env:HASH_DIR

if (-not $appId -or -not $tenantId -or -not $appSecret) {
    throw "Missing Entra credentials. Set ENTRA_APP_ID, ENTRA_TENANT_ID, ENTRA_APP_SECRET."
}

if (-not $hashDir -or -not (Test-Path $hashDir)) {
    throw "Hash directory '$hashDir' not found."
}

Import-Module Microsoft.Graph.Authentication -ErrorAction Stop
Import-Module WindowsAutopilotIntune -ErrorAction Stop

Write-Host "Connecting to Microsoft Graph..." -ForegroundColor Cyan
$secret = ConvertTo-SecureString $appSecret -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential($appId, $secret)
Connect-MgGraph -TenantId $tenantId -ClientSecretCredential $credential -NoWelcome
Write-Host "Connected as app $appId in tenant $tenantId"

$csvFiles = Get-ChildItem -Path $hashDir -Filter "*_hwid.csv"
if ($csvFiles.Count -eq 0) {
    Write-Host "No CSV files found in $hashDir"
    exit 0
}

Write-Host "Found $($csvFiles.Count) hash file(s) to upload"
$successCount = 0
$failCount = 0

foreach ($csv in $csvFiles) {
    Write-Host "`nUploading: $($csv.Name)" -ForegroundColor Yellow
    try {
        $result = Import-AutopilotCSV -csvFile $csv.FullName
        Write-Host "  Imported successfully" -ForegroundColor Green
        if ($result) { $result | Format-List }
        $successCount++
    }
    catch {
        Write-Host "  FAILED: $_" -ForegroundColor Red
        $failCount++
    }
}

Write-Host "`n--- Upload Summary ---" -ForegroundColor Cyan
Write-Host "Total: $($csvFiles.Count) | Success: $successCount | Failed: $failCount"

Disconnect-MgGraph | Out-Null
