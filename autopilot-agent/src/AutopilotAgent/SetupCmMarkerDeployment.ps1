[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$siteCode = 'LAB'
$siteServer = 'LABZ1-CM01.test.gell.one'
$targetDeviceName = 'RING0IVY24-01'
$collectionName = 'LABZ1 MECM Marker - RING0IVY24-01'
$packageName = 'LABZ1 MECM Marker'
$programName = 'Write MECM Marker'
$markerContent = 'LABZ1 MECM Marker deployment succeeded'
$sourcePath = 'C:\ProgramData\SetupCm\MecmMarkerDeployment'
$sourceScriptPath = Join-Path $sourcePath 'Install-MecmMarker.ps1'
$changed = $false

if ($env:COMPUTERNAME -ine 'LABZ1-CM01') {
    throw 'MECM marker deployment may only run on LABZ1-CM01.'
}

$sourceScript = @"
`$ErrorActionPreference = 'Stop'
`$markerPath = 'C:\ProgramData\SetupCm\MecmMarkerDeployment\installed-by-mecm.txt'
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
        New-CMDeviceCollection -Name $collectionName -LimitingCollectionName 'All Systems' -RefreshType Continuous -Comment 'Single-device MECM marker deployment proof.' -ErrorAction Stop | Out-Null
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

    $package = @(Get-CMPackage -Name $packageName -ErrorAction Stop)
    if ($package.Count -gt 1) {
        throw "Configuration Manager returned multiple packages named $packageName."
    }
    $packageCreated = $false
    if ($package.Count -eq 0) {
        New-CMPackage -Name $packageName -Path $sourcePath -Description 'Harmless LABZ1 MECM marker deployment.' -ErrorAction Stop | Out-Null
        $packageCreated = $true
        $changed = $true
        $package = @(Get-CMPackage -Name $packageName -ErrorAction Stop)
    }
    if ($package.Count -ne 1) {
        throw "Configuration Manager did not read back exactly one package named $packageName."
    }
    $program = @(Get-CMProgram -PackageId $package[0].PackageID -ProgramName $programName -ErrorAction Stop)
    if ($program.Count -gt 1) {
        throw "Configuration Manager returned multiple programs named $programName."
    }
    if ($program.Count -eq 0) {
        New-CMProgram -PackageId $package[0].PackageID -StandardProgramName $programName -CommandLine 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File Install-MecmMarker.ps1' -RunType Hidden -RunMode RunWithAdministrativeRights -ProgramRunType WhetherOrNotUserIsLoggedOn -UserInteraction $false -ErrorAction Stop | Out-Null
        $changed = $true
    }

    $contentDistributionRequested = $false
    if ($packageCreated) {
        Start-CMContentDistribution -PackageId $package[0].PackageID -DistributionPointName $siteServer -ErrorAction Stop | Out-Null
        $contentDistributionRequested = $true
    }

    $deployments = @(Get-CMPackageDeployment -PackageId $package[0].PackageID -ErrorAction Stop | Where-Object { $_.CollectionID -eq $collection[0].CollectionID })
    if ($deployments.Count -gt 1) {
        throw "Configuration Manager returned multiple marker deployments for $targetDeviceName."
    }
    $deploymentCreated = $false
    if ($deployments.Count -eq 0) {
        $schedule = New-CMSchedule -Start (Get-Date).AddMinutes(1) -Nonrecurring -ErrorAction Stop
        New-CMPackageDeployment -StandardProgram -PackageId $package[0].PackageID -ProgramName $programName -CollectionId $collection[0].CollectionID -DeployPurpose Required -FastNetworkOption DownloadContentFromDistributionPointAndRunLocally -SlowNetworkOption DownloadContentFromDistributionPointAndLocally -RerunBehavior RerunIfFailedPreviousAttempt -RunFromSoftwareCenter $false -SystemRestart $false -SoftwareInstallation $true -Schedule $schedule -Comment 'LABZ1 single-device MECM marker proof.' -ErrorAction Stop | Out-Null
        $deploymentCreated = $true
        $changed = $true
    }

    Invoke-CMClientNotification -DeviceName $targetDeviceName -NotificationType RequestMachinePolicyNow -ErrorAction Stop

    [ordered]@{
        site_code = $siteCode
        device_name = $targetDeviceName
        collection_id = [string]$collection[0].CollectionID
        package_id = [string]$package[0].PackageID
        program_name = $programName
        deployment_created = [bool]$deploymentCreated
        content_distribution_requested = [bool]$contentDistributionRequested
        machine_policy_requested = $true
        changed = [bool]$changed
    } | ConvertTo-Json -Compress
}
finally {
    Pop-Location
}
