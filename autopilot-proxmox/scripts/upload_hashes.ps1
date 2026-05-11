#!/usr/bin/env pwsh
# Upload hardware hash CSVs to Windows Autopilot via Microsoft Graph.
# Uploads all CSVs in parallel using PowerShell jobs.

$ErrorActionPreference = 'Stop'

$appId     = $env:ENTRA_APP_ID
$tenantId  = $env:ENTRA_TENANT_ID
$appSecret = $env:ENTRA_APP_SECRET
$hashDir   = $env:HASH_DIR
$hashFile  = $env:HASH_FILE
$groupTag  = $env:GROUP_TAG

if (-not $appId -or -not $tenantId -or -not $appSecret) {
    throw "Missing Entra credentials. Set ENTRA_APP_ID, ENTRA_TENANT_ID, ENTRA_APP_SECRET."
}

if (-not $hashDir -or -not (Test-Path $hashDir)) {
    throw "Hash directory '$hashDir' not found."
}

if ($hashFile) {
    if (-not (Test-Path -LiteralPath $hashFile)) {
        throw "Selected hash file '$hashFile' not found."
    }
    $csvFiles = @(Get-Item -LiteralPath $hashFile)
} else {
    $csvFiles = @(Get-ChildItem -Path $hashDir -Filter "*_hwid.csv")
}
if ($csvFiles.Count -eq 0) {
    Write-Host "No CSV files found in $hashDir"
    exit 0
}

Write-Host "Found $($csvFiles.Count) hash file(s) to upload in parallel"
if ($hashFile) {
    Write-Host "Selected hash file: $hashFile"
}
if ($groupTag) {
    Write-Host "Applying group tag override: $groupTag"
}

# Launch a background job per CSV
$jobs = @()
foreach ($csv in $csvFiles) {
    Write-Host "Starting upload: $($csv.Name)"
    $job = Start-Job -ScriptBlock {
        param($csvPath, $appId, $tenantId, $appSecret, $groupTag)
        $ErrorActionPreference = 'Stop'
        Import-Module Microsoft.Graph.Authentication
        Import-Module WindowsAutopilotIntune
        function New-TaggedCsvCopy {
            param(
                [Parameter(Mandatory)] [string] $CsvPath,
                [string] $Tag
            )
            if ([string]::IsNullOrWhiteSpace($Tag)) {
                return $CsvPath
            }
            $rows = @(Import-Csv -LiteralPath $CsvPath)
            if ($rows.Count -eq 0) {
                throw "Hash CSV '$CsvPath' contains no data rows."
            }
            foreach ($row in $rows) {
                if ($row.PSObject.Properties.Match('Group Tag').Count -eq 0) {
                    $row | Add-Member -NotePropertyName 'Group Tag' -NotePropertyValue $Tag
                } else {
                    $row.'Group Tag' = $Tag
                }
            }
            $tempName = [IO.Path]::GetFileNameWithoutExtension($CsvPath) + "-tagged-" + [guid]::NewGuid().ToString("N") + ".csv"
            $tempPath = Join-Path ([IO.Path]::GetTempPath()) $tempName
            $rows | Export-Csv -LiteralPath $tempPath -NoTypeInformation -Encoding UTF8
            return $tempPath
        }
        $uploadPath = $null
        $connected = $false
        try {
            $uploadPath = New-TaggedCsvCopy -CsvPath $csvPath -Tag $groupTag
            $secret = ConvertTo-SecureString $appSecret -AsPlainText -Force
            $credential = New-Object System.Management.Automation.PSCredential($appId, $secret)
            Connect-MgGraph -TenantId $tenantId -ClientSecretCredential $credential -NoWelcome
            $connected = $true
            $result = Import-AutopilotCSV -csvFile $uploadPath
            return @{
                File = (Split-Path $csvPath -Leaf)
                Success = $true
                Result = ($result | Out-String)
            }
        } finally {
            if ($connected) {
                Disconnect-MgGraph | Out-Null
            }
            if ($uploadPath -and $uploadPath -ne $csvPath -and (Test-Path -LiteralPath $uploadPath)) {
                Remove-Item -LiteralPath $uploadPath -Force
            }
        }
    } -ArgumentList $csv.FullName, $appId, $tenantId, $appSecret, $groupTag
    $jobs += $job
}

Write-Host "`nWaiting for $($jobs.Count) upload(s) to complete..."

# Wait and collect results
$successCount = 0
$failCount = 0

foreach ($job in $jobs) {
    $result = Receive-Job -Job $job -Wait -ErrorAction SilentlyContinue
    $err = $job.ChildJobs[0].Error

    if ($job.State -eq 'Completed' -and $result.Success) {
        Write-Host "OK: $($result.File)" -ForegroundColor Green
        if ($result.Result.Trim()) { Write-Host "  $($result.Result.Trim())" }
        $successCount++
    } else {
        $fileName = if ($result.File) { $result.File } else { "unknown" }
        $errMsg = if ($err) { $err[0].ToString() } else { $job.State }
        Write-Host "FAILED: $fileName - $errMsg" -ForegroundColor Red
        $failCount++
    }
    Remove-Job -Job $job
}

Write-Host "`n--- Upload Summary ---" -ForegroundColor Cyan
Write-Host "Total: $($csvFiles.Count) | Success: $successCount | Failed: $failCount"

if ($failCount -gt 0) { exit 1 }
