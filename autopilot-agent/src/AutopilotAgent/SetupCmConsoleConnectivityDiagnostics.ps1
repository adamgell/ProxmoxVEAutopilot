[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$siteCode = 'LAB'
$siteServer = 'LABZ1-CM01.test.gell.one'
$errors = [System.Collections.Generic.List[string]]::new()

function Get-DcomAceSummary {
    param(
        [byte[]]$SecurityDescriptor,
        [string]$DomainAdminsSid,
        [string]$SmsAdminsSid
    )

    if (-not $SecurityDescriptor -or $SecurityDescriptor.Length -eq 0) {
        return [ordered]@{
            present = $false
            domain_admins_remote_activation = $false
            sms_admins_remote_activation = $false
            builtin_administrators_remote_activation = $false
        }
    }

    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new($SecurityDescriptor, 0)
    $allow = [System.Security.AccessControl.AceType]::AccessAllowed
    $remoteActivation = 0x10
    $domainAdminsRemoteActivation = $false
    $smsAdminsRemoteActivation = $false
    $builtinAdministratorsRemoteActivation = $false
    foreach ($ace in @($descriptor.DiscretionaryAcl)) {
        if ($ace.AceType -ne $allow -or -not ($ace -is [System.Security.AccessControl.KnownAce])) {
            continue
        }
        $hasRemoteActivation = (($ace.AccessMask -band $remoteActivation) -eq $remoteActivation)
        if ($hasRemoteActivation -and $ace.SecurityIdentifier.Value -eq $DomainAdminsSid) {
            $domainAdminsRemoteActivation = $true
        }
        if ($hasRemoteActivation -and $ace.SecurityIdentifier.Value -eq $SmsAdminsSid) {
            $smsAdminsRemoteActivation = $true
        }
        if ($hasRemoteActivation -and $ace.SecurityIdentifier.Value -eq 'S-1-5-32-544') {
            $builtinAdministratorsRemoteActivation = $true
        }
    }
    return [ordered]@{
        present = $true
        domain_admins_remote_activation = [bool]$domainAdminsRemoteActivation
        sms_admins_remote_activation = [bool]$smsAdminsRemoteActivation
        builtin_administrators_remote_activation = [bool]$builtinAdministratorsRemoteActivation
    }
}

$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
if (-not $computerSystem.PartOfDomain -or [string]::IsNullOrWhiteSpace($computerSystem.Domain)) {
    throw 'CM01 must be joined to a domain before diagnosing MECM console connectivity.'
}

$domainAdminsSid = ([System.Security.Principal.NTAccount]::new(
    [string]$computerSystem.Domain,
    'Domain Admins'
)).Translate([System.Security.Principal.SecurityIdentifier])
if (-not $domainAdminsSid.Value.EndsWith('-512', [System.StringComparison]::Ordinal)) {
    throw 'The resolved Domain Admins principal does not have the built-in RID 512.'
}
$domainAdminsPrincipal = $domainAdminsSid.Translate([System.Security.Principal.NTAccount]).Value
$smsAdminsSid = ([System.Security.Principal.NTAccount]::new(
    $env:COMPUTERNAME,
    'SMS Admins'
)).Translate([System.Security.Principal.SecurityIdentifier])

$interactivePrincipals = @(
    Get-CimInstance -ClassName Win32_LogonSession -Filter 'LogonType = 2 OR LogonType = 10' |
        ForEach-Object {
            $session = $_
            Get-CimAssociatedInstance -InputObject $session -Association Win32_LoggedOnUser -ResultClassName Win32_UserAccount |
                Where-Object { $_.Domain -and $_.Name -and $_.SID } |
                ForEach-Object {
                    [pscustomobject]@{
                        principal = "$($_.Domain)\\$($_.Name)"
                        sid = [string]$_.SID
                        logon_type = [int]$session.LogonType
                    }
                }
        } |
        Sort-Object principal -Unique
)

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
    $administrativeUsers = @(Get-CMAdministrativeUser -Name $domainAdminsPrincipal -ErrorAction Stop)
    if ($administrativeUsers.Count -ne 1) {
        throw "Configuration Manager did not return exactly one administrative-user record for $domainAdminsPrincipal."
    }
    $admin = $administrativeUsers[0]
    $rbac = [ordered]@{
        full_administrator = [bool](@($admin.RoleNames) -contains 'Full Administrator')
        default_scope = [bool](@($admin.CategoryNames) -contains 'Default')
    }
}
finally {
    Pop-Location
}

$ole = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Ole' -ErrorAction Stop
$dcom = [ordered]@{
    machine_launch_restriction = Get-DcomAceSummary -SecurityDescriptor $ole.MachineLaunchRestriction -DomainAdminsSid $domainAdminsSid.Value -SmsAdminsSid $smsAdminsSid.Value
    default_launch_permission = Get-DcomAceSummary -SecurityDescriptor $ole.DefaultLaunchPermission -DomainAdminsSid $domainAdminsSid.Value -SmsAdminsSid $smsAdminsSid.Value
}

$consoleLogCandidates = @(
    (Join-Path -Path ${env:ProgramFiles(x86)} -ChildPath 'Microsoft Configuration Manager\AdminConsole\AdminUILog\SMSAdminUI.log'),
    (Join-Path -Path $env:ProgramFiles -ChildPath 'Microsoft Configuration Manager\AdminConsole\AdminUILog\SMSAdminUI.log')
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
$consoleLogPath = @($consoleLogCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1)[0]
$consoleLog = [ordered]@{
    found = -not [string]::IsNullOrWhiteSpace($consoleLogPath)
    path = $consoleLogPath
    tail = if ($consoleLogPath) {
        (Get-Content -LiteralPath $consoleLogPath -Tail 180 -ErrorAction Stop | Out-String).Trim()
    }
    else {
        ''
    }
}

$recentDcomEvents = @(
    Get-WinEvent -FilterHashtable @{
        LogName = 'System'
        ProviderName = 'Microsoft-Windows-DistributedCOM'
        StartTime = (Get-Date).AddMinutes(-30)
    } -MaxEvents 20 -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -in 10016, 10036 } |
        Select-Object -First 8 |
        ForEach-Object {
            [ordered]@{
                id = [int]$_.Id
                time_utc = $_.TimeCreated.ToUniversalTime().ToString('o')
                message = ([string]$_.Message).Substring(0, [Math]::Min(600, ([string]$_.Message).Length))
            }
        }
)

[ordered]@{
    domain_admins_principal = $domainAdminsPrincipal
    rbac = $rbac
    interactive_principals = $interactivePrincipals
    dcom = $dcom
    console_log = $consoleLog
    recent_distributed_com_events = $recentDcomEvents
    errors = @($errors)
} | ConvertTo-Json -Depth 8 -Compress
