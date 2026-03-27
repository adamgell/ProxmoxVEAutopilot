function Connect-ProxmoxHost {
    <#
    .SYNOPSIS
        Establishes a connection to a Proxmox host via cv4pve

    .DESCRIPTION
        Loads the cv4pve module and connects to a Proxmox cluster using an API token
        or credentials. Stores the PveTicket in $script:pveTicket for reuse.

    .PARAMETER Host
        Proxmox host and port. If not specified, uses the value from proxmoxConfig.

    .PARAMETER ApiToken
        API token in USER@REALM!TOKENID=UUID format. If not specified, uses proxmoxConfig.

    .PARAMETER Credentials
        PSCredential for username/password auth (alternative to ApiToken).

    .PARAMETER SkipCertificateCheck
        Skip TLS certificate validation. If not specified, uses proxmoxConfig.

    .PARAMETER Force
        Force a new connection even if one already exists.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        [string]$HostAndPort,

        [parameter(Mandatory = $false)]
        [string]$ApiToken,

        [parameter(Mandatory = $false)]
        [PSCredential]$Credentials,

        [parameter(Mandatory = $false)]
        [switch]$SkipCertificateCheck,

        [parameter(Mandatory = $false)]
        [switch]$Force
    )

    # Return existing ticket if valid and not forcing reconnect
    if ($script:pveTicket -and -not $Force) {
        Write-Verbose "Reusing existing Proxmox connection"
        return $script:pveTicket
    }

    # Load cv4pve module from the submodule path
    $cv4pvePath = Join-Path $PSScriptRoot '..\..\cv4pve-api-powershell\Corsinvest.ProxmoxVE.Api'
    $cv4pveManifest = Join-Path $cv4pvePath 'Corsinvest.ProxmoxVE.Api.psd1'

    if (-not (Test-Path $cv4pveManifest)) {
        throw "cv4pve module not found at '$cv4pveManifest'. Ensure the cv4pve-api-powershell submodule is initialized (git submodule update --init)."
    }

    if (-not (Get-Module -Name 'Corsinvest.ProxmoxVE.Api')) {
        Write-Verbose "Loading cv4pve module from: $cv4pvePath"
        Import-Module $cv4pveManifest -Force -ErrorAction Stop
    }

    # Resolve parameters from config if not explicitly provided
    $pxConfig = $script:hvConfig.proxmoxConfig

    if (-not $HostAndPort) {
        if ($pxConfig -and $pxConfig.host) {
            $HostAndPort = $pxConfig.host
        }
        else {
            throw "Proxmox host not specified and no proxmoxConfig found. Run Add-ProxmoxToConfig first."
        }
    }

    if (-not $ApiToken -and -not $Credentials) {
        if ($pxConfig -and $pxConfig.apiToken) {
            $ApiToken = $pxConfig.apiToken
        }
    }

    if (-not $PSBoundParameters.ContainsKey('SkipCertificateCheck') -and $pxConfig) {
        $SkipCertificateCheck = [bool]$pxConfig.skipCertificateCheck
    }

    # Build connection parameters
    $connectParams = @{
        HostsAndPorts = @($HostAndPort)
    }

    if ($ApiToken) {
        $connectParams['ApiToken'] = $ApiToken
    }
    elseif ($Credentials) {
        $connectParams['Credentials'] = $Credentials
    }
    else {
        throw "No API token or credentials provided. Specify -ApiToken, -Credentials, or configure via Add-ProxmoxToConfig."
    }

    if ($SkipCertificateCheck) {
        $connectParams['SkipCertificateCheck'] = $true
    }

    Write-Host "Connecting to Proxmox host $HostAndPort.. " -ForegroundColor Cyan -NoNewline
    try {
        $script:pveTicket = Connect-PveCluster @connectParams
        Write-Host $script:tick -ForegroundColor Green
        return $script:pveTicket
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        throw "Failed to connect to Proxmox host '$HostAndPort': $_"
    }
}
