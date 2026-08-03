[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$siteCode = 'LAB'
$siteServer = 'LABZ1-CM01.test.gell.one'
$roleName = 'Full Administrator'
$scopeName = 'Default'
$changed = $false
$olePath = 'HKLM:\SOFTWARE\Microsoft\Ole'
$dcomRemoteActivationRights = 0x15 # Execute + Remote Execute + Remote Activate

function Test-DcomRemoteActivation {
    param(
        [byte[]]$SecurityDescriptor,
        [string]$SidValue
    )

    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($SecurityDescriptor, 0)
    foreach ($ace in @($descriptor.DiscretionaryAcl)) {
        if ($ace -is [System.Security.AccessControl.KnownAce] -and
            $ace.AceType -eq [System.Security.AccessControl.AceType]::AccessAllowed -and
            $ace.SecurityIdentifier.Value -eq $SidValue -and
            (($ace.AccessMask -band $dcomRemoteActivationRights) -eq $dcomRemoteActivationRights)) {
            return $true
        }
    }
    return $false
}

function Grant-DcomRemoteActivation {
    param(
        [string]$ValueName,
        [System.Security.Principal.SecurityIdentifier]$PrincipalSid
    )

    $current = [byte[]](Get-ItemPropertyValue -LiteralPath $olePath -Name $ValueName -ErrorAction Stop)
    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($current, 0)
    if (-not $descriptor.DiscretionaryAcl) {
        throw "$ValueName has no discretionary ACL."
    }
    foreach ($ace in @($descriptor.DiscretionaryAcl)) {
        if ($ace -is [System.Security.AccessControl.KnownAce] -and
            $ace.AceType -eq [System.Security.AccessControl.AceType]::AccessDenied -and
            $ace.SecurityIdentifier.Value -eq $PrincipalSid.Value) {
            throw "$ValueName explicitly denies the SMS Admins group; refusing to override that deny ACE."
        }
    }
    if (-not (Test-DcomRemoteActivation -SecurityDescriptor $current -SidValue $PrincipalSid.Value)) {
        $newAce = [System.Security.AccessControl.CommonAce]::new(
            $false,
            [System.Security.AccessControl.AceQualifier]::AccessAllowed,
            $dcomRemoteActivationRights,
            $PrincipalSid,
            $false,
            $null)
        $descriptor.DiscretionaryAcl.InsertAce($descriptor.DiscretionaryAcl.Count, $newAce)
        $updated = [byte[]]::new($descriptor.BinaryLength)
        $descriptor.GetBinaryForm($updated, 0)
        Set-ItemProperty -LiteralPath $olePath -Name $ValueName -Value $updated -Type Binary -ErrorAction Stop
        $script:changed = $true
    }
    $readBack = [byte[]](Get-ItemPropertyValue -LiteralPath $olePath -Name $ValueName -ErrorAction Stop)
    if (-not (Test-DcomRemoteActivation -SecurityDescriptor $readBack -SidValue $PrincipalSid.Value)) {
        throw "$ValueName did not read back Remote Activation for SMS Admins."
    }
}

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
$smsAdminsSid = ([System.Security.Principal.NTAccount]::new(
    $env:COMPUTERNAME,
    'SMS Admins'
)).Translate([System.Security.Principal.SecurityIdentifier])
$smsAdminsPrincipal = $smsAdminsSid.Translate([System.Security.Principal.NTAccount]).Value

$smsAdminsMembers = @(Get-LocalGroupMember -Group 'SMS Admins' -ErrorAction Stop)
if (-not ($smsAdminsMembers | Where-Object { $_.SID -eq $domainAdminsSid.Value })) {
    Add-LocalGroupMember -Group 'SMS Admins' -Member $principal -ErrorAction Stop
    $changed = $true
}
$smsAdminsReadBack = @(Get-LocalGroupMember -Group 'SMS Admins' -ErrorAction Stop)
if (-not ($smsAdminsReadBack | Where-Object { $_.SID -eq $domainAdminsSid.Value })) {
    throw 'Domain Admins did not read back as a member of local SMS Admins.'
}

Grant-DcomRemoteActivation -ValueName 'MachineLaunchRestriction' -PrincipalSid $smsAdminsSid
Grant-DcomRemoteActivation -ValueName 'DefaultLaunchPermission' -PrincipalSid $smsAdminsSid

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
        sms_admins_membership = $true
        machine_launch_remote_activation = $true
        default_launch_remote_activation = $true
        changed = [bool]$changed
    } | ConvertTo-Json -Compress
}
finally {
    Pop-Location
}
