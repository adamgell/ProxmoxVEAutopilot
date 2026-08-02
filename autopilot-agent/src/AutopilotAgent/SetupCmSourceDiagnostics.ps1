[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Z0-9]{3}$')][string]$SiteCode,
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9-]{1,63}$')][string]$TargetComputerName
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

[ordered]@{
    site_code = $SiteCode
    target_computer_name = $TargetComputerName
    target_machine_sid = $machineSid
    share_access = $shareAccess
    client_folder_access = $clientFolderAccess
    cifs_spns = $cifsSpns
    errors = @($errors)
} | ConvertTo-Json -Compress -Depth 4
