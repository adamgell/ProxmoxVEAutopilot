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

if (-not ('AutopilotAgentTokenNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class AutopilotAgentTokenNative
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);

    [DllImport("advapi32.dll", SetLastError = true)]
    public static extern bool OpenProcessToken(IntPtr processHandle, uint desiredAccess, out IntPtr tokenHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr handle);
}
'@
}

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
$consoleProcessTokens = @(
    Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
        Where-Object { $_.Name -in @('Microsoft.ConfigurationManagement.exe', 'AdminConsole.exe') } |
        ForEach-Object {
            $process = $_
            $processHandle = [AutopilotAgentTokenNative]::OpenProcess(0x1000, $false, [int]$process.ProcessId)
            if ($processHandle -eq [IntPtr]::Zero) {
                $processHandle = [AutopilotAgentTokenNative]::OpenProcess(0x0400, $false, [int]$process.ProcessId)
            }
            if ($processHandle -eq [IntPtr]::Zero) {
                return [ordered]@{
                    process_id = [int]$process.ProcessId
                    process_name = [string]$process.Name
                    error = 'Unable to open the Configuration Manager console process token.'
                }
            }
            $tokenHandle = [IntPtr]::Zero
            try {
                if (-not [AutopilotAgentTokenNative]::OpenProcessToken($processHandle, 0x0008, [ref]$tokenHandle)) {
                    throw 'Unable to query the Configuration Manager console process token.'
                }
                $identity = [System.Security.Principal.WindowsIdentity]::new($tokenHandle)
                try {
                    $tokenGroupSids = @($identity.Groups | ForEach-Object { $_.Value })
                    [ordered]@{
                        process_id = [int]$process.ProcessId
                        process_name = [string]$process.Name
                        user_sid = $identity.User.Value
                        domain_admins_member = [bool]($tokenGroupSids -contains $domainAdminsSid.Value)
                        sms_admins_member = [bool]($tokenGroupSids -contains $smsAdminsSid.Value)
                    }
                }
                finally {
                    $identity.Dispose()
                }
            }
            catch {
                [ordered]@{
                    process_id = [int]$process.ProcessId
                    process_name = [string]$process.Name
                    error = $_.Exception.Message
                }
            }
            finally {
                if ($tokenHandle -ne [IntPtr]::Zero) {
                    [void][AutopilotAgentTokenNative]::CloseHandle($tokenHandle)
                }
                [void][AutopilotAgentTokenNative]::CloseHandle($processHandle)
            }
        }
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
$smsProviderLogPath = Join-Path -Path $env:ProgramFiles -ChildPath 'Microsoft Configuration Manager\Logs\SMSProv.log'
$smsProviderLog = [ordered]@{
    found = Test-Path -LiteralPath $smsProviderLogPath -PathType Leaf
    path = $smsProviderLogPath
    tail = if (Test-Path -LiteralPath $smsProviderLogPath -PathType Leaf) {
        (Get-Content -LiteralPath $smsProviderLogPath -Tail 240 -ErrorAction Stop | Out-String).Trim()
    }
    else {
        ''
    }
    relevant_lines = if (Test-Path -LiteralPath $smsProviderLogPath -PathType Leaf) {
        $interactivePrincipalNames = @($interactivePrincipals | ForEach-Object { $_.principal })
        @(
            Get-Content -LiteralPath $smsProviderLogPath -Tail 12000 -ErrorAction Stop |
                Where-Object {
                    $line = [string]$_
                    ($line -match '(?i)access denied|not authorized|unauthorized|authentication|rbac|user context') -or
                    @($interactivePrincipalNames | Where-Object { $line -like "*$_*" }).Count -gt 0
                } |
                Select-Object -Last 180
        ) -join [Environment]::NewLine
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
    console_process_tokens = $consoleProcessTokens
    dcom = $dcom
    console_log = $consoleLog
    sms_provider_log = $smsProviderLog
    recent_distributed_com_events = $recentDcomEvents
    errors = @($errors)
} | ConvertTo-Json -Depth 8 -Compress
