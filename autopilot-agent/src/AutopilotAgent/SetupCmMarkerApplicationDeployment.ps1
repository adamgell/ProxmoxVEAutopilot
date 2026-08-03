[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$siteCode = 'LAB'
$siteServer = 'LABZ1-CM01.test.gell.one'
$targetDeviceName = 'RING0IVY24-01'
$collectionName = 'LABZ1 MECM Marker Application - RING0IVY24-01'
$applicationName = 'LABZ1 MECM Marker Application'
$deploymentTypeName = 'Write MECM Application Marker'
$markerContent = 'LABZ1 MECM Marker Application deployment succeeded'
$sourcePath = 'C:\ProgramData\SetupCm\MecmMarkerApplication'
$sourceScriptPath = Join-Path $sourcePath 'Install-MecmMarkerApplication.ps1'
$changed = $false

if ($env:COMPUTERNAME -ine 'LABZ1-CM01') {
    throw 'MECM marker application deployment may only run on LABZ1-CM01.'
}

$sourceScript = @"
`$ErrorActionPreference = 'Stop'
`$markerPath = 'C:\ProgramData\SetupCm\MecmMarkerApplication\installed-by-mecm-application.txt'
New-Item -ItemType Directory -Path (Split-Path -Parent `$markerPath) -Force | Out-Null
Set-Content -LiteralPath `$markerPath -Value '$markerContent' -Encoding utf8 -NoNewline
"@
New-Item -ItemType Directory -Path $sourcePath -Force | Out-Null
Set-Content -LiteralPath $sourceScriptPath -Value $sourceScript -Encoding utf8 -NoNewline

$adminUiPath = [string]$env:SMS_ADMIN_UI_PATH
if ([string]::IsNullOrWhiteSpace($adminUiPath)) {
    throw 'SMS_ADMIN_UI_PATH is required to load the Configuration Manager module.'
}
$modulePath = Join-Path (Split-Path -Parent $adminUiPath) 'ConfigurationManager.psd1'
if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
    throw "Configuration Manager module was not found at $modulePath."
}
Import-Module -Name $modulePath -Force -ErrorAction Stop
$Global:CMPSSuppressFastNotUsedCheck = $true
$siteDrive = Get-PSDrive -PSProvider CMSite -ErrorAction Stop |
    Where-Object { $_.Name -eq $siteCode } |
    Select-Object -First 1
if (-not $siteDrive) {
    New-PSDrive -Name $siteCode -PSProvider CMSite -Root $siteServer -ErrorAction Stop | Out-Null
}

Push-Location -LiteralPath "$siteCode`:"
try {
    $device = @(Get-CMDevice -Name $targetDeviceName -ErrorAction Stop)
    if ($device.Count -ne 1) {
        throw "Configuration Manager did not return exactly one device named $targetDeviceName."
    }

    $collection = @(Get-CMDeviceCollection -Name $collectionName -ErrorAction Stop)
    if ($collection.Count -gt 1) {
        throw "Configuration Manager returned multiple device collections named $collectionName."
    }
    if ($collection.Count -eq 0) {
        New-CMDeviceCollection -Name $collectionName -LimitingCollectionName 'All Systems' -RefreshType Continuous -Comment 'Single-device MECM Application deployment proof.' -ErrorAction Stop | Out-Null
        $changed = $true
        $collection = @(Get-CMDeviceCollection -Name $collectionName -ErrorAction Stop)
    }
    if ($collection.Count -ne 1) {
        throw "Configuration Manager did not read back exactly one device collection named $collectionName."
    }
    $directRules = @($collection[0] | Get-CMDeviceCollectionDirectMembershipRule -ErrorAction Stop)
    if (-not ($directRules | Where-Object { [string]$_.ResourceID -eq [string]$device[0].ResourceID })) {
        Add-CMDeviceCollectionDirectMembershipRule -CollectionId $collection[0].CollectionID -ResourceId $device[0].ResourceID -ErrorAction Stop | Out-Null
        $changed = $true
    }
    Invoke-CMCollectionUpdate -CollectionId $collection[0].CollectionID -ErrorAction Stop

    $application = @(Get-CMApplication -Name $applicationName -ErrorAction Stop)
    if ($application.Count -gt 1) {
        throw "Configuration Manager returned multiple applications named $applicationName."
    }
    $applicationCreated = $false
    if ($application.Count -eq 0) {
        New-CMApplication -Name $applicationName -Publisher 'LABZ1' -SoftwareVersion '1.0.0' -Description 'Harmless single-device MECM Application deployment proof.' -ErrorAction Stop | Out-Null
        $applicationCreated = $true
        $changed = $true
        $application = @(Get-CMApplication -Name $applicationName -ErrorAction Stop)
    }
    if ($application.Count -ne 1) {
        throw "Configuration Manager did not read back exactly one application named $applicationName."
    }

    $deploymentTypes = @(Get-CMDeploymentType -ApplicationName $applicationName -ErrorAction Stop)
    if ($deploymentTypes.Count -gt 1) {
        throw "Configuration Manager returned multiple deployment types for $applicationName."
    }
    if ($deploymentTypes.Count -eq 0) {
        $detectionClause = New-CMDetectionClauseFile -Path 'C:\ProgramData\SetupCm\MecmMarkerApplication' -FileName 'installed-by-mecm-application.txt' -Existence -ErrorAction Stop
        Add-CMScriptDeploymentType -ApplicationName $applicationName -DeploymentTypeName $deploymentTypeName -InstallCommand 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File Install-MecmMarkerApplication.ps1' -ContentLocation $sourcePath -InstallationBehaviorType InstallForSystem -LogonRequirementType WhetherOrNotUserLoggedOn -UserInteractionMode Hidden -AddDetectionClause $detectionClause -ErrorAction Stop | Out-Null
        $changed = $true
    }

    $deployments = @(Get-CMApplicationDeployment -Name $applicationName -ErrorAction Stop | Where-Object { $_.CollectionID -eq $collection[0].CollectionID })
    if ($deployments.Count -gt 1) {
        throw "Configuration Manager returned multiple marker application deployments for $targetDeviceName."
    }
    $deploymentCreated = $false
    if ($deployments.Count -eq 0) {
        $deploymentTime = (Get-Date).AddMinutes(-1)
        New-CMApplicationDeployment -Name $applicationName -CollectionId $collection[0].CollectionID -DeployAction Install -DeployPurpose Required -AvailableDateTime $deploymentTime -DeadlineDateTime $deploymentTime -TimeBaseOn LocalTime -UserNotification HideAll -DistributeContent -DistributionPointName $siteServer -Comment 'LABZ1 single-device MECM Application marker proof.' -ErrorAction Stop | Out-Null
        $deploymentCreated = $true
        $changed = $true
    }

    Invoke-CMClientNotification -DeviceName $targetDeviceName -NotificationType RequestMachinePolicyNow -ErrorAction Stop

    [ordered]@{
        site_code = $siteCode
        device_name = $targetDeviceName
        collection_id = [string]$collection[0].CollectionID
        application_name = $applicationName
        deployment_type_name = $deploymentTypeName
        application_created = [bool]$applicationCreated
        deployment_created = [bool]$deploymentCreated
        machine_policy_requested = $true
        changed = [bool]$changed
    } | ConvertTo-Json -Compress
}
finally {
    Pop-Location
}
