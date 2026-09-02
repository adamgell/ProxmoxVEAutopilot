[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$targetMac = 'BC-24-11-9C-43-E6'
$targetIp = '192.168.16.103'
$targetPrefixLength = 24
$targetGateway = '192.168.16.1'
$targetDns = '192.168.16.12'
$targetDc = 'LABZ1-DC02.test.gell.one'

function Normalize-MacAddress {
    param([Parameter(Mandatory)][string]$Value)
    (($Value -replace '[:-]', '').ToUpperInvariant() -replace '(..)(?!$)', '$1-')
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory)][string]$ComputerName,
        [Parameter(Mandatory)][int]$Port
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync($ComputerName, $Port)
        if (-not $connect.Wait(5000)) { return $false }
        $client.Connected
    }
    finally {
        $client.Dispose()
    }
}

$matches = @(
    Get-NetAdapter -Physical |
        Where-Object {
            $_.Status -eq 'Up' -and
            (Normalize-MacAddress -Value $_.MacAddress) -eq $targetMac
        }
)
if ($matches.Count -ne 1) {
    throw "Expected exactly one active adapter with MAC $targetMac; found $($matches.Count)."
}

$adapter = $matches[0]
$interfaceIndex = $adapter.ifIndex
Set-NetIPInterface -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
Get-NetRoute -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Remove-NetRoute -Confirm:$false -ErrorAction Stop
Get-NetIPAddress -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Remove-NetIPAddress -Confirm:$false -ErrorAction Stop
New-NetIPAddress -InterfaceIndex $interfaceIndex -IPAddress $targetIp -PrefixLength $targetPrefixLength -DefaultGateway $targetGateway -ErrorAction Stop | Out-Null
Set-DnsClientServerAddress -InterfaceIndex $interfaceIndex -ServerAddresses $targetDns -ErrorAction Stop
Clear-DnsClientCache -ErrorAction Stop

$ipv4 = @(
    Get-NetIPAddress -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -eq $targetIp }
)
$gateway = @(
    Get-NetRoute -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
        Sort-Object RouteMetric |
        Select-Object -First 1
)
$dnsServers = @(
    Get-DnsClientServerAddress -InterfaceIndex $interfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
        ForEach-Object ServerAddresses |
        Where-Object { $_ }
)
if ($ipv4.Count -ne 1 -or $ipv4[0].PrefixLength -ne $targetPrefixLength -or
    $gateway.Count -ne 1 -or $gateway[0].NextHop -ne $targetGateway -or
    $dnsServers.Count -ne 1 -or $dnsServers[0] -ne $targetDns) {
    throw 'Post-change LABZ1 network readback did not match the fixed contract.'
}

Resolve-DnsName -Name $targetDc -Type A -ErrorAction Stop | Out-Null
& "$env:WINDIR\System32\nltest.exe" /dsgetdc:test.gell.one | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Domain-controller discovery failed after the LABZ1 network repair.'
}
$tcp53 = Test-TcpPort -ComputerName $targetDc -Port 53
$tcp445 = Test-TcpPort -ComputerName $targetDc -Port 445
if (-not $tcp53 -or -not $tcp445) {
    throw 'DC02 TCP 53/445 connectivity failed after the LABZ1 network repair.'
}

[ordered]@{
    adapter_mac = Normalize-MacAddress -Value $adapter.MacAddress
    ipv4_address = $ipv4[0].IPAddress
    prefix_length = $ipv4[0].PrefixLength
    default_gateway = $gateway[0].NextHop
    dns_servers = @($dnsServers)
    dc_lookup = $true
    tcp_53 = $tcp53
    tcp_445 = $tcp445
    errors = @()
} | ConvertTo-Json -Compress -Depth 4
