[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^LAB$')][string]$SiteCode,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9-]{1,63}$')][string]$TargetComputerName,
    [Parameter(Mandatory)][ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')][string]$ClientIpv4
)

$ErrorActionPreference = 'Stop'
$errors = [System.Collections.Generic.List[string]]::new()
$maximumRows = 100
$namespace = "root\SMS\site_$SiteCode"
$clientPackageId = "${SiteCode}00003"

function Get-DiagnosticValue {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action,
        $Fallback
    )

    try { & $Action }
    catch {
        $errors.Add($Name)
        $Fallback
    }
}

function Get-ClientSubnet {
    param([Parameter(Mandatory)][string]$Ipv4)

    $address = [System.Net.IPAddress]::Parse($Ipv4)
    if ($address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'ClientIpv4 must be an IPv4 address.'
    }
    $bytes = $address.GetAddressBytes()
    "{0}.{1}.{2}.0/24" -f $bytes[0], $bytes[1], $bytes[2]
}

function Test-ClientAddressInRange {
    param(
        [Parameter(Mandatory)][string]$Ipv4,
        [Parameter(Mandatory)][string]$Range
    )

    if ($Range -notmatch '^\s*(?<start>\d{1,3}(\.\d{1,3}){3})\s*-\s*(?<end>\d{1,3}(\.\d{1,3}){3})\s*$') {
        return $false
    }
    $toUInt32 = {
        param([string]$Address)
        $bytes = [System.Net.IPAddress]::Parse($Address).GetAddressBytes()
        [Array]::Reverse($bytes)
        [BitConverter]::ToUInt32($bytes, 0)
    }
    $value = & $toUInt32 $Ipv4
    $start = & $toUInt32 $Matches.start
    $end = & $toUInt32 $Matches.end
    $value -ge $start -and $value -le $end
}

$clientSubnet = Get-DiagnosticValue -Name 'client_subnet' -Fallback '' -Action {
    Get-ClientSubnet -Ipv4 $ClientIpv4
}
$allBoundaries = Get-DiagnosticValue -Name 'boundaries' -Fallback @() -Action {
    @(Get-CimInstance -Namespace $namespace -ClassName SMS_Boundary -ErrorAction Stop)
}
$matchingBoundaries = @(
    $allBoundaries |
        Where-Object {
            $type = [int]$_.BoundaryType
            ($type -eq 0 -and ([string]$_.Value).Trim() -eq $clientSubnet) -or
            ($type -eq 3 -and (Test-ClientAddressInRange -Ipv4 $ClientIpv4 -Range ([string]$_.Value)))
        } |
        Select-Object -First $maximumRows -Property BoundaryID, BoundaryType, DisplayName, Value
)
$allMemberships = Get-DiagnosticValue -Name 'boundary_group_memberships' -Fallback @() -Action {
    @(Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroupMembers -ErrorAction Stop)
}
$allBoundaryGroups = Get-DiagnosticValue -Name 'boundary_groups' -Fallback @() -Action {
    @(Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroup -ErrorAction Stop)
}
$matchingBoundaryIds = @($matchingBoundaries | ForEach-Object { [int]$_.BoundaryID })
$matchingGroupIds = @(
    $allMemberships |
        Where-Object { $matchingBoundaryIds -contains [int]$_.BoundaryID } |
        ForEach-Object { [int]$_.GroupID } |
        Select-Object -Unique
)
$boundaryGroups = @(
    $allBoundaryGroups |
        Where-Object { $matchingGroupIds -contains [int]$_.GroupID } |
        Select-Object -First $maximumRows -Property GroupID, Name, Description, DefaultSiteCode
)
$allGroupSiteSystems = Get-DiagnosticValue -Name 'boundary_group_site_systems' -Fallback @() -Action {
    @(Get-CimInstance -Namespace $namespace -ClassName SMS_BoundaryGroupSiteSystems -ErrorAction Stop)
}
$distributionPoints = @(
    $allGroupSiteSystems |
        Where-Object { $matchingGroupIds -contains [int]$_.GroupID } |
        Select-Object -First $maximumRows -Property GroupID, ServerNALPath, Flags
)
$packageDistribution = Get-DiagnosticValue -Name 'client_package_distribution' -Fallback @() -Action {
    @(
        Get-CimInstance -Namespace $namespace -ClassName SMS_PackageStatusDistPointsSummarizer -Filter "PackageID = '$clientPackageId'" -ErrorAction Stop |
            Select-Object -First $maximumRows -Property PackageID, ServerNALPath, State, SourceVersion, SummaryDate
    )
}

[ordered]@{
    site_code = $SiteCode
    target_computer_name = $TargetComputerName
    client_ipv4 = $ClientIpv4
    client_subnet = $clientSubnet
    matching_boundaries = @($matchingBoundaries)
    boundary_groups = @($boundaryGroups)
    distribution_points = @($distributionPoints)
    client_package = [ordered]@{
        package_id = $clientPackageId
        distribution_points = @($packageDistribution)
    }
    errors = @($errors)
} | ConvertTo-Json -Compress -Depth 6
