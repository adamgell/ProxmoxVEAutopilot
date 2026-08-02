[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Z0-9]{3}$')][string]$SiteCode,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9-]{1,63}$')][string]$TargetComputerName,
    [switch]$RemediateSourceAccess
)

$ErrorActionPreference = 'Stop'
$errors = [System.Collections.Generic.List[string]]::new()

function Get-DiagnosticValue {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action
    )

    try { & $Action }
    catch {
        $errors.Add($Name)
        $null
    }
}

function Get-AccessRuleSidValue {
    param([Parameter(Mandatory)]$IdentityReference)

    try {
        $IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    }
    catch { $null }
}

function Get-MatchingReadAndExecuteAce {
    param(
        [Parameter(Mandatory)]$AccessRules,
        [Parameter(Mandatory)][string]$MachineSid
    )

    $requiredInheritance = [int](
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    @(
        $AccessRules |
            Where-Object {
                $_.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
                (Get-AccessRuleSidValue -IdentityReference $_.IdentityReference) -eq $MachineSid -and
                (([int64]$_.FileSystemRights -band [int64][System.Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq
                    [int64][System.Security.AccessControl.FileSystemRights]::ReadAndExecute) -and
                (([int]$_.InheritanceFlags -band $requiredInheritance) -eq $requiredInheritance) -and
                $_.PropagationFlags -eq [System.Security.AccessControl.PropagationFlags]::None
            } |
            Select-Object IdentityReference, AccessControlType, FileSystemRights,
                InheritanceFlags, PropagationFlags, IsInherited
    )
}

$shareName = "SMS_$SiteCode"
$clientPath = 'C:\Program Files\Microsoft Configuration Manager\Client'
$domainName = Get-DiagnosticValue -Name 'computer_domain' -Action {
    (Get-CimInstance -ClassName Win32_ComputerSystem).Domain
}
$machineSid = Get-DiagnosticValue -Name 'target_machine_sid' -Action {
    ([System.Security.Principal.NTAccount]::new(
        [string]$domainName,
        "$TargetComputerName`$"
    )).Translate([System.Security.Principal.SecurityIdentifier]).Value
}
$shareAccess = Get-DiagnosticValue -Name 'share_access' -Action {
    @(
        Get-SmbShareAccess -Name $shareName |
            Select-Object AccountName, AccessControlType, AccessRight
    )
}
$clientFolderAccess = Get-DiagnosticValue -Name 'client_folder_access' -Action {
    @(
        (Get-Acl -LiteralPath $clientPath).Access |
            Select-Object IdentityReference, AccessControlType, FileSystemRights,
                InheritanceFlags, PropagationFlags, IsInherited
    )
}
$cifsSpns = Get-DiagnosticValue -Name 'cifs_spns' -Action {
    @(
        & "$env:WINDIR\System32\setspn.exe" -L $env:COMPUTERNAME 2>$null |
            Where-Object { $_ -match '^\s*cifs/' } |
            ForEach-Object { $_.Trim() }
    )
}
$sourceAccessRemediation = $null
if ($RemediateSourceAccess) {
    if (-not $machineSid) {
        throw 'The target machine SID could not be resolved.'
    }
    $acl = Get-Acl -LiteralPath $clientPath
    $before = @(Get-MatchingReadAndExecuteAce -AccessRules $acl.Access -MachineSid $machineSid)
    $changed = $false
    if ($before.Count -eq 0) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            [System.Security.Principal.SecurityIdentifier]::new($machineSid),
            [System.Security.AccessControl.FileSystemRights]::ReadAndExecute,
            ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $clientPath -AclObject $acl
        $changed = $true
    }
    $after = @(Get-MatchingReadAndExecuteAce -AccessRules (Get-Acl -LiteralPath $clientPath).Access -MachineSid $machineSid)
    if ($after.Count -eq 0) {
        throw 'The target machine ReadAndExecute ACE was not present after Set-Acl.'
    }
    $sourceAccessRemediation = [ordered]@{
        changed = $changed
        target_machine_sid = $machineSid
        matching_aces = $after
    }
}

[ordered]@{
    site_code = $SiteCode
    target_computer_name = $TargetComputerName
    target_machine_sid = $machineSid
    share_access = $shareAccess
    client_folder_access = $clientFolderAccess
    cifs_spns = $cifsSpns
    source_access_remediation = $sourceAccessRemediation
    errors = @($errors)
} | ConvertTo-Json -Compress -Depth 4
