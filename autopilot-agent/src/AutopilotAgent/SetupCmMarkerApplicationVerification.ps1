[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$siteCode = 'LAB'
$deviceName = 'RING0IVY24-01'
$markerContent = 'LABZ1 MECM Marker Application deployment succeeded'
$markerPath = 'C:\ProgramData\SetupCm\MecmMarkerApplication\installed-by-mecm-application.txt'
$appEnforceLog = 'C:\Windows\CCM\Logs\AppEnforce.log'

if ($env:COMPUTERNAME -ine $deviceName) {
    throw "MECM marker application verification may only run on $deviceName."
}

$markerPresent = $false
$markerContentMatches = $false
$appEnforceMentionsMarker = $false
for ($attempt = 0; $attempt -lt 24; $attempt++) {
    $markerPresent = Test-Path -LiteralPath $markerPath -PathType Leaf
    $markerContentMatches = $markerPresent -and ((Get-Content -LiteralPath $markerPath -Raw -ErrorAction Stop).Trim() -eq $markerContent)
    $appEnforceMentionsMarker = (Test-Path -LiteralPath $appEnforceLog -PathType Leaf) -and
        ((Get-Content -LiteralPath $appEnforceLog -Tail 500 -ErrorAction SilentlyContinue | Out-String) -match 'LABZ1 MECM Marker Application')
    if ($markerPresent -and $markerContentMatches -and $appEnforceMentionsMarker) {
        break
    }
    Start-Sleep -Seconds 5
}

[ordered]@{
    site_code = $siteCode
    device_name = $deviceName
    marker_present = [bool]$markerPresent
    marker_content_matches = [bool]$markerContentMatches
    appenforce_mentions_marker = [bool]$appEnforceMentionsMarker
    marker_path = $markerPath
} | ConvertTo-Json -Compress
