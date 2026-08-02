[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^LAB$')][string]$SiteCode,
    [Parameter(Mandatory)][ValidatePattern('^192\.168\.16\.0/24$')][string]$ClientSubnet,
    [Parameter(Mandatory)][ValidatePattern('^LABZ1 Client Network$')][string]$BoundaryGroupName,
    [Parameter(Mandatory)][ValidatePattern('^LABZ1-CM01\.test\.gell\.one$')][string]$DistributionPointFqdn
)

$ErrorActionPreference = 'Stop'
$changed = $false
$namespace = "root\SMS\site_$SiteCode"
$adminUiPath = [string]$env:SMS_ADMIN_UI_PATH
if ([string]::IsNullOrWhiteSpace($adminUiPath)) {
    throw 'SMS_ADMIN_UI_PATH is required to load the Configuration Manager module.'
}
$modulePath = Join-Path (Split-Path (Split-Path -Parent $adminUiPath) -Parent) 'ConfigurationManager.psd1'
if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
    throw "Configuration Manager module was not found at $modulePath."
}

Import-Module -Name $modulePath -Force -ErrorAction Stop
$siteDrive = Get-PSDrive -PSProvider CMSite -ErrorAction Stop |
    Where-Object { $_.Name -eq $SiteCode } |
    Select-Object -First 1
if (-not $siteDrive) {
    throw "Configuration Manager site drive $SiteCode`: was not found."
}

Push-Location -LiteralPath "$SiteCode`:"
try {
    $boundary = Get-CimInstance -Namespace $namespace -ClassName SMS_Boundary `
        -Filter "BoundaryType = 0 AND Value = '$ClientSubnet'" -ErrorAction Stop |
        Select-Object -First 1
    if (-not $boundary) {
        New-CMBoundary -BoundaryType IPSubnet -Value $ClientSubnet `
            -Description $BoundaryGroupName -ErrorAction Stop | Out-Null
        $boundary = Get-CimInstance -Namespace $namespace -ClassName SMS_Boundary `
            -Filter "BoundaryType = 0 AND Value = '$ClientSubnet'" -ErrorAction Stop |
            Select-Object -First 1
        if (-not $boundary) { throw "Boundary $ClientSubnet was not created." }
        $changed = $true
    }

    $group = Get-CMBoundaryGroup -Name $BoundaryGroupName -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $group) {
        New-CMBoundaryGroup -Name $BoundaryGroupName -ErrorAction Stop | Out-Null
        $group = Get-CMBoundaryGroup -Name $BoundaryGroupName -ErrorAction Stop |
            Select-Object -First 1
        if (-not $group) { throw "Boundary group $BoundaryGroupName was not created." }
        $changed = $true
    }

    $members = @(Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroupMembers `
        -Filter "GroupID = $([int]$group.GroupID)" -ErrorAction Stop)
    $unexpectedMember = $members | Where-Object { [int]$_.BoundaryID -ne [int]$boundary.BoundaryID } |
        Select-Object -First 1
    if ($unexpectedMember) {
        throw "Boundary group $BoundaryGroupName contains unexpected boundary ID $($unexpectedMember.BoundaryID)."
    }
    if (-not ($members | Where-Object { [int]$_.BoundaryID -eq [int]$boundary.BoundaryID })) {
        Add-CMBoundaryToGroup -BoundaryGroupId ([int]$group.GroupID) `
            -BoundaryId ([int]$boundary.BoundaryID) -ErrorAction Stop
        $changed = $true
    }

    $siteSystems = @(Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroupSiteSystems `
        -Filter "GroupID = $([int]$group.GroupID)" -ErrorAction Stop)
    $expectedSystem = $siteSystems | Where-Object {
        ([string]$_.ServerNALPath) -match [regex]::Escape($DistributionPointFqdn)
    } | Select-Object -First 1
    $unexpectedSystem = $siteSystems | Where-Object {
        ([string]$_.ServerNALPath) -notmatch [regex]::Escape($DistributionPointFqdn)
    } | Select-Object -First 1
    if ($unexpectedSystem) {
        throw "Boundary group $BoundaryGroupName contains an unexpected site system."
    }
    if (-not $expectedSystem) {
        Set-CMBoundaryGroup -Name $BoundaryGroupName `
            -AddSiteSystemServerName $DistributionPointFqdn -ErrorAction Stop
        $changed = $true
    }

    $readBoundary = Get-CimInstance -Namespace $namespace -ClassName SMS_Boundary `
        -Filter "BoundaryID = $([int]$boundary.BoundaryID)" -ErrorAction Stop |
        Select-Object -First 1 -Property BoundaryID, BoundaryType, DisplayName, Value
    $readGroup = Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroup `
        -Filter "GroupID = $([int]$group.GroupID)" -ErrorAction Stop |
        Select-Object -First 1 -Property GroupID, Name, Description, DefaultSiteCode
    $readSystems = @(Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroupSiteSystems `
        -Filter "GroupID = $([int]$group.GroupID)" -ErrorAction Stop |
        Select-Object -Property GroupID, ServerNALPath, Flags)

    [ordered]@{
        site_code = $SiteCode
        client_subnet = $ClientSubnet
        boundary_group_name = $BoundaryGroupName
        distribution_point_fqdn = $DistributionPointFqdn
        boundary = $readBoundary
        boundary_group = $readGroup
        distribution_points = @($readSystems)
        changed = [bool]$changed
        errors = @()
    } | ConvertTo-Json -Compress -Depth 6
}
finally {
    Pop-Location
}
