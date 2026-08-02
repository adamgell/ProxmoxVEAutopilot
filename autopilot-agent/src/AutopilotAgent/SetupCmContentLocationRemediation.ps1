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
$modulePath = Join-Path (Split-Path -Parent $adminUiPath) 'ConfigurationManager.psd1'
if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
    throw "Configuration Manager module was not found at $modulePath."
}

Import-Module -Name $modulePath -Force -ErrorAction Stop
function Get-ContentLocationSystemHost([string]$ServerNalPath) {
    $match = [regex]::Match($ServerNalPath, 'Display=\\\\(?<host>[^"\\\]\[]+)')
    if (-not $match.Success) {
        throw 'Boundary group site system ServerNALPath does not contain a Display host.'
    }
    return $match.Groups['host'].Value.TrimEnd('.').ToLowerInvariant()
}

$expectedHost = $DistributionPointFqdn.TrimEnd('.').ToLowerInvariant()
$siteDrive = Get-PSDrive -PSProvider CMSite -ErrorAction Stop |
    Where-Object { $_.Name -eq $SiteCode } |
    Select-Object -First 1
if (-not $siteDrive) {
    New-PSDrive -Name $SiteCode -PSProvider CMSite -Root $DistributionPointFqdn `
        -Description 'LABZ1 content location remediation' -ErrorAction Stop | Out-Null
    $siteDrive = Get-PSDrive -Name $SiteCode -PSProvider CMSite -ErrorAction Stop
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
        (Get-ContentLocationSystemHost ([string]$_.ServerNALPath)) -eq $expectedHost
    } | Select-Object -First 1
    $unexpectedSystem = $siteSystems | Where-Object {
        (Get-ContentLocationSystemHost ([string]$_.ServerNALPath)) -ne $expectedHost
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
    $readSystemHosts = @($readSystems | ForEach-Object {
        Get-ContentLocationSystemHost ([string]$_.ServerNALPath)
    })
    if ($readSystemHosts.Count -ne 1 -or $readSystemHosts[0] -ne $expectedHost) {
        throw "Boundary group $BoundaryGroupName did not read back exactly one expected distribution point."
    }

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
