[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$siteCode = 'LAB'
$siteServer = 'LABZ1-CM01.test.gell.one'
$roleName = 'Full Administrator'
$scopeName = 'Default'
$changed = $false

$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
if (-not $computerSystem.PartOfDomain -or [string]::IsNullOrWhiteSpace($computerSystem.Domain)) {
    throw 'CM01 must be joined to a domain before assigning Domain Admins.'
}

$domainAdminsSid = ([System.Security.Principal.NTAccount]::new(
    [string]$computerSystem.Domain,
    'Domain Admins'
)).Translate([System.Security.Principal.SecurityIdentifier])
if (-not $domainAdminsSid.Value.EndsWith('-512', [System.StringComparison]::Ordinal)) {
    throw 'The resolved Domain Admins principal does not have the built-in RID 512.'
}
$principal = $domainAdminsSid.Translate([System.Security.Principal.NTAccount]).Value

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
    $admin = @(Get-CMAdministrativeUser -Name $principal -ErrorAction Stop)
    if ($admin.Count -gt 1) {
        throw "Configuration Manager returned multiple administrative-user records for $principal."
    }
    if ($admin.Count -eq 0) {
        New-CMAdministrativeUser -Name $principal -RoleName $roleName -SecurityScopeName $scopeName -ErrorAction Stop | Out-Null
        $changed = $true
    }
    else {
        if (-not (@($admin[0].RoleNames) -contains $roleName)) {
            Add-CMSecurityRoleToAdministrativeUser -AdministrativeUserName $principal -RoleName $roleName -ErrorAction Stop
            $changed = $true
        }
        if (-not (@($admin[0].CategoryNames) -contains $scopeName)) {
            Add-CMSecurityScopeToAdministrativeUser -AdministrativeUserName $principal -SecurityScopeName $scopeName -ErrorAction Stop
            $changed = $true
        }
    }

    $readBack = @(Get-CMAdministrativeUser -Name $principal -ErrorAction Stop)
    if ($readBack.Count -ne 1) {
        throw "Configuration Manager did not read back exactly one administrative-user record for $principal."
    }
    $fullAdministrator = @($readBack[0].RoleNames) -contains $roleName
    $defaultScope = @($readBack[0].CategoryNames) -contains $scopeName
    if (-not $fullAdministrator -or -not $defaultScope) {
        throw "Configuration Manager did not assign $roleName with the $scopeName scope to $principal."
    }

    [ordered]@{
        principal = $principal
        full_administrator = [bool]$fullAdministrator
        default_scope = [bool]$defaultScope
        changed = [bool]$changed
    } | ConvertTo-Json -Compress
}
finally {
    Pop-Location
}
