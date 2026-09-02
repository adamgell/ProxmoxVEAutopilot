$ErrorActionPreference = 'Stop'

$ipv4Addresses = @(
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.PrefixOrigin -ne 'WellKnown'
        } |
        Select-Object -ExpandProperty IPAddress -Unique |
        Sort-Object
)

[ordered]@{
    command_id       = 'endpoint_facts'
    computer_name    = $env:COMPUTERNAME
    os_version       = [System.Environment]::OSVersion.Version.ToString()
    powershell_version = $PSVersionTable.PSVersion.ToString()
    current_user     = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    ipv4_addresses   = $ipv4Addresses
} | ConvertTo-Json -Compress -Depth 3
