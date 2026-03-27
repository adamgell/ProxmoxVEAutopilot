function Add-ProxmoxToConfig {
    <#
    .SYNOPSIS
        Adds Proxmox connection settings to the APHVTools configuration

    .DESCRIPTION
        Stores Proxmox host, API token, default node, storage, bridge, ISO storage,
        optional template VMID, and certificate settings in hvconfig.json under a
        proxmoxConfig section.

    .PARAMETER ProxmoxHost
        Proxmox host and port (e.g. "192.168.1.100:8006")

    .PARAMETER ApiToken
        Proxmox API token in USER@REALM!TOKENID=UUID format

    .PARAMETER DefaultNode
        Default Proxmox node name (e.g. "pve")

    .PARAMETER DefaultStorage
        Default storage for VM disks (e.g. "local-lvm")

    .PARAMETER DefaultBridge
        Default network bridge (e.g. "vmbr0")

    .PARAMETER IsoStorage
        Storage location for ISO images (e.g. "local")

    .PARAMETER VirtIOIsoPath
        Path to VirtIO drivers ISO on Proxmox storage (e.g. "local:iso/virtio-win.iso")

    .PARAMETER TemplateVmid
        Optional Proxmox VMID of a prepared Windows template to use for clone-based
        provisioning instead of ISO-based installs.

    .PARAMETER SkipCertificateCheck
        Skip TLS certificate validation when connecting to Proxmox

    .EXAMPLE
        Add-ProxmoxToConfig -ProxmoxHost "192.168.1.100:8006" -ApiToken "root@pam!mytoken=uuid-value" -DefaultNode "pve"

    .EXAMPLE
        Add-ProxmoxToConfig -ProxmoxHost "pve.local:8006" -ApiToken "root@pam!mytoken=uuid" -DefaultNode "pve" -DefaultStorage "local-lvm" -DefaultBridge "vmbr0" -TemplateVmid 100 -SkipCertificateCheck
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [parameter(Position = 1, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$ProxmoxHost,

        [parameter(Position = 2, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$ApiToken,

        [parameter(Position = 3, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$DefaultNode,

        [parameter(Position = 4, Mandatory = $false)]
        [string]$DefaultStorage = 'local-lvm',

        [parameter(Position = 5, Mandatory = $false)]
        [string]$DefaultBridge = 'vmbr0',

        [parameter(Position = 6, Mandatory = $false)]
        [string]$IsoStorage = 'local',

        [parameter(Position = 7, Mandatory = $false)]
        [string]$VirtIOIsoPath,

        [parameter(Position = 8, Mandatory = $false)]
        [int]$TemplateVmid,

        [parameter(Position = 9, Mandatory = $false)]
        [switch]$SkipCertificateCheck
    )
    try {
        if (-not $script:hvConfig -or -not $script:hvConfig.hvConfigPath) {
            throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
        }

        Write-Host "Adding Proxmox configuration to config.. " -ForegroundColor Cyan -NoNewline

        $existingProxmoxConfig = $script:hvConfig.proxmoxConfig

        $proxmoxConfig = [pscustomobject]@{
            host                 = $ProxmoxHost
            apiToken             = $ApiToken
            defaultNode          = $DefaultNode
            defaultStorage       = $DefaultStorage
            defaultBridge        = $DefaultBridge
            isoStorage           = $IsoStorage
            virtIOIsoPath        = $VirtIOIsoPath
            templateVmid         = if ($PSBoundParameters.ContainsKey('TemplateVmid')) { $TemplateVmid } elseif ($existingProxmoxConfig -and $existingProxmoxConfig.templateVmid) { [int]$existingProxmoxConfig.templateVmid } else { $null }
            skipCertificateCheck = [bool]$SkipCertificateCheck
        }

        if ($PSCmdlet.ShouldProcess($ProxmoxHost, "Update Proxmox configuration")) {
            if ($script:hvConfig.PSObject.Properties.Name.Contains('proxmoxConfig')) {
                $script:hvConfig.proxmoxConfig = $proxmoxConfig
            }
            else {
                $script:hvConfig | Add-Member -MemberType NoteProperty -Name 'proxmoxConfig' -Value $proxmoxConfig
            }
            $script:hvConfig | ConvertTo-Json -Depth 20 | Out-File -FilePath $script:hvConfig.hvConfigPath -Encoding ascii -Force
        }
    }
    catch {
        $errorMsg = $_
    }
    finally {
        if ($errorMsg) {
            Write-Warning $errorMsg
        }
        else {
            Write-Host $script:tick -ForegroundColor Green
        }
    }
}
