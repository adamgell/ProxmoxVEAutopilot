function New-ProxmoxClientVM {
    <#
    .SYNOPSIS
        Creates Proxmox VMs with optional OEM SMBIOS profiles, Autopilot configuration, and hardware hash capture

    .DESCRIPTION
        Creates one or more Windows VMs on a Proxmox host using cv4pve, with UEFI/Q35/VirtIO
        configuration. Supports OEM hardware profiles (Lenovo, Dell, HP, Surface) via SMBIOS
        fields, Autopilot policy injection, and hardware hash capture via the QEMU guest agent.

    .PARAMETER TenantName
        The name of the tenant to create VMs for

    .PARAMETER OSBuild
        The ISO image to use. Maps to an ISO file on Proxmox storage.
        If not specified, uses the tenant's default image name to construct the ISO path.

    .PARAMETER NumberOfVMs
        Number of VMs to create (1-999)

    .PARAMETER CPUsPerVM
        Number of CPU cores per VM (1-128)

    .PARAMETER MemoryMB
        Memory per VM in MiB (1024-65536). Default: 4096

    .PARAMETER DiskSizeGB
        OS disk size in GB. Default: 64

    .PARAMETER OemProfile
        OEM hardware profile name (e.g. "lenovo-t14", "dell-latitude-5540", "surface-pro-10").
        Sets SMBIOS manufacturer, product, family, SKU, and chassis type to match real hardware.
        Use Get-OemProfile -List to see available profiles.

    .PARAMETER SerialNumber
        Custom serial number. If not specified, auto-generated with manufacturer-specific prefix.

    .PARAMETER Manufacturer
        SMBIOS manufacturer override (e.g. "Lenovo"). Takes precedence over OemProfile value.

    .PARAMETER Product
        SMBIOS product override (e.g. "ThinkPad T14 Gen 4"). Takes precedence over OemProfile.

    .PARAMETER Family
        SMBIOS family override. Takes precedence over OemProfile value.

    .PARAMETER SKU
        SMBIOS SKU override. Takes precedence over OemProfile value.

    .PARAMETER ChassisType
        SMBIOS chassis type override (3=Desktop, 9=Laptop, 10=Notebook, etc.).

    .PARAMETER SkipAutoPilot
        Skip Autopilot configuration injection

    .PARAMETER CaptureHardwareHash
        Capture hardware hash after VM creation for Autopilot registration

    .PARAMETER AppId
        Azure AD App Registration Application (Client) ID for app-based authentication

    .PARAMETER TenantId
        Azure AD Tenant (Directory) ID for app-based authentication

    .PARAMETER AppSecret
        Azure AD App Registration Client Secret as SecureString

    .PARAMETER ProxmoxNode
        Override the default Proxmox node from config

    .PARAMETER Storage
        Override the default storage from config

    .PARAMETER Bridge
        Override the default network bridge from config

    .PARAMETER VlanTag
        Optional VLAN tag for the VM network adapter

    .PARAMETER IsoPath
        Full Proxmox ISO path (e.g. "local:iso/Win11_24H2.iso"). Overrides OSBuild.
        Ignored when using the template-clone path.

    .PARAMETER UseTemplate
        Clone from a configured Proxmox template VM instead of creating a VM from ISO media.

    .PARAMETER TemplateVmid
        Override the configured Proxmox template VMID for clone-based provisioning.

    .PARAMETER WaitForGuestAgent
        Wait for the QEMU guest agent to become available before injecting config.
        Default: true

    .PARAMETER GuestAgentTimeoutSeconds
        Maximum seconds to wait for guest agent. Default: 900 (15 min)

    .PARAMETER UseVmidInName
        When set, generated VM names use the allocated Proxmox VMID instead of the
        per-call loop index. This is useful for rerunnable test flows that should
        produce a unique VM name every time they execute.

    .EXAMPLE
        New-ProxmoxClientVM -TenantName "Contoso" -IsoPath "local:iso/Win11_24H2.iso" -OemProfile "lenovo-t14"

    .EXAMPLE
        New-ProxmoxClientVM -TenantName "Contoso" -IsoPath "local:iso/Win11_24H2.iso" `
            -OemProfile "dell-latitude-5540" -NumberOfVMs 5 -CaptureHardwareHash

    .EXAMPLE
        New-ProxmoxClientVM -TenantName "Contoso" -IsoPath "local:iso/Win11_24H2.iso" `
            -Manufacturer "Custom Corp" -Product "Custom Model" -SerialNumber "CUSTOM-001"

    .EXAMPLE
        New-ProxmoxClientVM -TenantName "Contoso" -IsoPath "local:iso/Win11_24H2.iso" `
            -UseVmidInName

    .EXAMPLE
        New-ProxmoxClientVM -TenantName "Contoso" -UseTemplate -TemplateVmid 100
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [parameter(Position = 1, Mandatory = $true)]
        [string]$TenantName,

        [parameter(Position = 2, Mandatory = $false)]
        [string]$OSBuild,

        [parameter(Position = 3, Mandatory = $false)]
        [ValidateRange(1, 999)]
        [int]$NumberOfVMs = 1,

        [parameter(Position = 4, Mandatory = $false)]
        [ValidateRange(1, 128)]
        [int]$CPUsPerVM = 2,

        [parameter(Position = 5, Mandatory = $false)]
        [ValidateRange(1024, 65536)]
        [int]$MemoryMB = 4096,

        [parameter(Position = 6, Mandatory = $false)]
        [ValidateRange(16, 2048)]
        [int]$DiskSizeGB = 64,

        [parameter(Mandatory = $false)]
        [string]$OemProfile,

        [parameter(Mandatory = $false)]
        [string]$SerialNumber,

        [parameter(Mandatory = $false)]
        [string]$Manufacturer,

        [parameter(Mandatory = $false)]
        [string]$Product,

        [parameter(Mandatory = $false)]
        [string]$Family,

        [parameter(Mandatory = $false)]
        [string]$SKU,

        [parameter(Mandatory = $false)]
        [int]$ChassisType,

        [parameter(Mandatory = $false)]
        [switch]$SkipAutoPilot,

        [parameter(Mandatory = $false)]
        [switch]$CaptureHardwareHash,

        [parameter(Mandatory = $false)]
        [string]$AppId,

        [parameter(Mandatory = $false)]
        [string]$TenantId,

        [parameter(Mandatory = $false)]
        [securestring]$AppSecret,

        [parameter(Mandatory = $false)]
        [string]$ProxmoxNode,

        [parameter(Mandatory = $false)]
        [string]$Storage,

        [parameter(Mandatory = $false)]
        [string]$Bridge,

        [parameter(Mandatory = $false)]
        [int]$VlanTag,

        [parameter(Mandatory = $false)]
        [string]$IsoPath,

        [parameter(Mandatory = $false)]
        [switch]$UseTemplate,

        [parameter(Mandatory = $false)]
        [int]$TemplateVmid,

        [parameter(Mandatory = $false)]
        [bool]$WaitForGuestAgent = $true,

        [parameter(Mandatory = $false)]
        [int]$GuestAgentTimeoutSeconds = 900,

        [parameter(Mandatory = $false)]
        [switch]$UseVmidInName
    )

    try {
        Write-Verbose "Starting New-ProxmoxClientVM..."

        #region Config
        if (-not $script:hvConfig -or -not $script:hvConfig.hvConfigPath) {
            throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
        }

        $pxConfig = $script:hvConfig.proxmoxConfig
        if (-not $pxConfig) {
            throw "No Proxmox configuration found. Run Add-ProxmoxToConfig first."
        }

        # Resolve Proxmox settings from config or parameters
        $node = if ($ProxmoxNode) { $ProxmoxNode } else { $pxConfig.defaultNode }
        $stor = if ($Storage) { $Storage } else { $pxConfig.defaultStorage }
        $brg = if ($Bridge) { $Bridge } else { $pxConfig.defaultBridge }
        $virtIOIso = $pxConfig.virtIOIsoPath
        $useTemplateClone = $UseTemplate -or $PSBoundParameters.ContainsKey('TemplateVmid')
        $resolvedTemplateVmid = if ($PSBoundParameters.ContainsKey('TemplateVmid')) { $TemplateVmid } elseif ($pxConfig -and $pxConfig.templateVmid) { [int]$pxConfig.templateVmid } else { $null }

        if (-not $node) { throw "Proxmox node not specified. Use -ProxmoxNode or configure via Add-ProxmoxToConfig." }
        if (-not $stor) { throw "Storage not specified. Use -Storage or configure via Add-ProxmoxToConfig." }
        if (-not $brg) { throw "Bridge not specified. Use -Bridge or configure via Add-ProxmoxToConfig." }
        if ($useTemplateClone -and -not $resolvedTemplateVmid) { throw "Template clone mode requested but no template VMID is configured. Use -TemplateVmid or configure it via Add-ProxmoxToConfig." }

        # Resolve tenant
        $clientDetails = $script:hvConfig.tenantConfig | Where-Object { $_.TenantName -eq $TenantName }
        if (-not $clientDetails) {
            throw "Tenant '$TenantName' not found in configuration. Run Add-TenantToConfig first."
        }

        $clientPath = "$($script:hvConfig.vmPath)\$TenantName"
        if (-not (Test-Path $clientPath)) {
            if ($PSCmdlet.ShouldProcess($clientPath, "Create directory")) {
                New-Item -ItemType Directory -Force -Path $clientPath | Out-Null
            }
        }

        # Resolve ISO path only for the install-from-ISO flow
        if (-not $useTemplateClone) {
            if (-not $IsoPath) {
                $imageName = if ($OSBuild) { $OSBuild } else { $clientDetails.ImageName }
                $isoStorage = if ($pxConfig.isoStorage) { $pxConfig.isoStorage } else { 'local' }
                $IsoPath = "${isoStorage}:iso/${imageName}.iso"
                Write-Verbose "Resolved ISO path: $IsoPath"
            }
        }
        elseif ($IsoPath) {
            Write-Verbose "Ignoring IsoPath because template clone mode is enabled."
        }

        # Resolve OEM profile: CLI param > tenant default > nothing
        $resolvedProfileName = $null
        if ($OemProfile) {
            $resolvedProfileName = $OemProfile
        }
        elseif ($clientDetails.OemProfile) {
            $resolvedProfileName = $clientDetails.OemProfile
            Write-Verbose "Using tenant default OEM profile: $resolvedProfileName"
        }

        $profileData = $null
        if ($resolvedProfileName) {
            $profileData = Get-OemProfile -ProfileName $resolvedProfileName
            Write-Verbose "Resolved OEM profile: $($profileData.Manufacturer) $($profileData.Product)"
        }

        # Resolve SMBIOS fields: explicit params > profile > nothing
        $smbiosMfg     = if ($Manufacturer)  { $Manufacturer }  elseif ($profileData) { $profileData.Manufacturer } else { $null }
        $smbiosProduct = if ($Product)        { $Product }       elseif ($profileData) { $profileData.Product }      else { $null }
        $smbiosFamily  = if ($Family)         { $Family }        elseif ($profileData) { $profileData.Family }       else { $null }
        $smbiosSku     = if ($SKU)            { $SKU }           elseif ($profileData) { $profileData.SKU }          else { $null }
        $smbiosChassis = if ($ChassisType)    { $ChassisType }   elseif ($profileData) { $profileData.ChassisType }  else { 0 }

        Write-Host "`n==== Proxmox VM Creation ====" -ForegroundColor Cyan
        Write-Host "Tenant: $TenantName" -ForegroundColor White
        Write-Host "Node: $node | Storage: $stor | Bridge: $brg" -ForegroundColor White
        if ($useTemplateClone) {
            Write-Host "Template VMID: $resolvedTemplateVmid" -ForegroundColor White
        }
        else {
            Write-Host "ISO: $IsoPath" -ForegroundColor White
        }
        Write-Host "VMs to create: $NumberOfVMs | CPUs: $CPUsPerVM | Memory: ${MemoryMB}MB | Disk: ${DiskSizeGB}GB" -ForegroundColor White
        if ($smbiosMfg) {
            Write-Host "OEM: $smbiosMfg $smbiosProduct ($smbiosSku)" -ForegroundColor White
        }
        Write-Host ""
        #endregion

        $vmNamePrefix = ($TenantName -replace '[^a-zA-Z0-9-]', '-') -replace '-{2,}', '-'
        $vmNamePrefix = $vmNamePrefix.Trim('-')
        if (-not $vmNamePrefix) {
            throw "TenantName '$TenantName' does not produce a valid Proxmox VM name prefix."
        }
        if ($vmNamePrefix -ne $TenantName) {
            Write-Verbose "Normalized Proxmox VM name prefix from '$TenantName' to '$vmNamePrefix'"
        }

        #region Connect to Proxmox
        $ticket = Connect-ProxmoxHost
        #endregion

        #region Get Autopilot policy
        $script:deviceNameTemplate = $null

        if (-not $SkipAutoPilot) {
            Write-Host "Grabbing Autopilot config.." -ForegroundColor Yellow

            # Check for stored credentials if not explicitly provided
            if (-not $AppId -and -not $TenantId -and -not $AppSecret) {
                $storedCreds = Get-TenantAuthCredentials -TenantPath $clientPath -TenantName $TenantName
                if ($storedCreds) {
                    Write-Host "Using stored app registration credentials" -ForegroundColor Cyan
                    $AppId = $storedCreds.AppId
                    $TenantId = $storedCreds.TenantId
                    $AppSecret = $storedCreds.AppSecret
                }
            }

            if ($PSCmdlet.ShouldProcess($clientPath, "Get Autopilot policy")) {
                $autopilotParams = @{
                    FileDestination = $clientPath
                }
                if ($AppId -and $TenantId -and $AppSecret) {
                    $autopilotParams['AppId'] = $AppId
                    $autopilotParams['TenantId'] = $TenantId
                    $autopilotParams['AppSecret'] = $AppSecret
                }
                Get-AutopilotPolicy @autopilotParams
            }

            # Load Autopilot naming convention
            $autopilotConfigPath = Join-Path $clientPath 'AutopilotConfigurationFile.json'
            if (Test-Path $autopilotConfigPath) {
                try {
                    $autopilotConfig = Get-Content -Path $autopilotConfigPath | ConvertFrom-Json
                    if ($autopilotConfig.CloudAssignedDeviceName) {
                        $script:deviceNameTemplate = $autopilotConfig.CloudAssignedDeviceName
                        Write-Verbose "Found device name template: $($script:deviceNameTemplate)"
                    }
                }
                catch {
                    Write-Warning "Could not parse AutopilotConfigurationFile.json: $_"
                }
            }
        }
        #endregion

        #region Create VMs
        $createdVMs = @()

        for ($i = 1; $i -le $NumberOfVMs; $i++) {
            Write-Host "`n---- VM $i of $NumberOfVMs ----" -ForegroundColor Green

            # Get next available VMID
            $vmid = Get-NextProxmoxVmid -PveTicket $ticket
            Write-Verbose "Allocated VMID: $vmid"

            # Use the allocated VMID in the name when callers need rerunnable,
            # globally unique test names without precomputing the next ID.
            $nameSuffix = if ($UseVmidInName) { $vmid } else { $i }
            $vmName = "$vmNamePrefix-$nameSuffix"

            # Generate serial number (unique per VM)
            $vmSerial = $null
            if ($smbiosMfg) {
                $vmSerial = New-ProxmoxSerialNumber -Manufacturer $smbiosMfg -CustomSerial $SerialNumber
                # For multiple VMs, only use the custom serial for the first VM; auto-generate for the rest
                if ($i -gt 1 -and $SerialNumber) {
                    $vmSerial = New-ProxmoxSerialNumber -Manufacturer $smbiosMfg
                }
                Write-Host "  Serial: $vmSerial | OEM: $smbiosMfg $smbiosProduct" -ForegroundColor Gray
            }

            if ($useTemplateClone) {
                $cloneParams = @{
                    PveTicket        = $ticket
                    Node             = $node
                    TemplateVmid     = $resolvedTemplateVmid
                    NewVmid          = $vmid
                    VMName           = $vmName
                    Cores            = $CPUsPerVM
                    MemoryMB         = $MemoryMB
                    Storage          = $stor
                    DiskSizeGB       = $DiskSizeGB
                    Bridge           = $brg
                    StartAfterCreate = $true
                }

                if ($VlanTag) {
                    $cloneParams['VlanTag'] = $VlanTag
                }
                if ($virtIOIso) {
                    $cloneParams['VirtIOIsoPath'] = $virtIOIso
                }

                if ($smbiosMfg) {
                    $cloneParams['Manufacturer'] = $smbiosMfg
                    $cloneParams['Product']      = $smbiosProduct
                    $cloneParams['Family']       = $smbiosFamily
                    $cloneParams['SKU']          = $smbiosSku
                    if ($vmSerial) {
                        $cloneParams['SerialNumber'] = $vmSerial
                    }
                    if ($smbiosChassis -gt 0) {
                        $cloneParams['ChassisType'] = $smbiosChassis
                    }
                }

                $vmResult = New-ProxmoxCloneDevice @cloneParams
            }
            else {
                $deviceParams = @{
                    PveTicket        = $ticket
                    Node             = $node
                    Vmid             = $vmid
                    VMName           = $vmName
                    Cores            = $CPUsPerVM
                    MemoryMB         = $MemoryMB
                    Storage          = $stor
                    DiskSizeGB       = $DiskSizeGB
                    IsoPath          = $IsoPath
                    Bridge           = $brg
                    StartAfterCreate = $true
                }

                if ($VlanTag) {
                    $deviceParams['VlanTag'] = $VlanTag
                }
                if ($virtIOIso) {
                    $deviceParams['VirtIOIsoPath'] = $virtIOIso
                }

                if ($smbiosMfg) {
                    $deviceParams['Manufacturer'] = $smbiosMfg
                    $deviceParams['Product']      = $smbiosProduct
                    $deviceParams['Family']       = $smbiosFamily
                    $deviceParams['SKU']          = $smbiosSku
                    if ($vmSerial) {
                        $deviceParams['SerialNumber'] = $vmSerial
                    }
                    if ($smbiosChassis -gt 0) {
                        $deviceParams['ChassisType'] = $smbiosChassis
                    }
                }

                $vmResult = New-ProxmoxDevice @deviceParams
            }

            if ($vmResult) {
                $createdVMs += [PSCustomObject]@{
                    Vmid         = $vmResult.Vmid
                    Name         = $vmResult.Name
                    Node         = $vmResult.Node
                    Status       = $vmResult.Status
                    SerialNumber = $vmResult.SerialNumber
                    OemProfile   = $vmResult.OemProfile
                    HashFile     = $null
                }
            }
        }
        #endregion

        #region Guest agent operations (Autopilot injection & hash capture)
        if ($createdVMs.Count -gt 0 -and $WaitForGuestAgent -and (-not $SkipAutoPilot -or $CaptureHardwareHash)) {
            Write-Host "`n---- Waiting for Guest Agent ----" -ForegroundColor Green
            Write-Host "VMs need to complete Windows setup and start the QEMU guest agent." -ForegroundColor Yellow
            Write-Host "This may take several minutes..." -ForegroundColor Yellow
            Write-Host "Polling Proxmox guest-ping every 10 seconds until the guest agent responds." -ForegroundColor DarkGray
            $guestAgentPollIntervalSeconds = 10

            foreach ($vm in $createdVMs) {
                Write-Host "`n  Processing VM '$($vm.Name)' (VMID: $($vm.Vmid))..." -ForegroundColor Cyan

                $agentReady = Wait-ProxmoxGuestAgent -PveTicket $ticket -Node $vm.Node -Vmid $vm.Vmid -VMName $vm.Name `
                    -TimeoutSeconds $GuestAgentTimeoutSeconds -PollIntervalSeconds $guestAgentPollIntervalSeconds

                if (-not $agentReady) {
                    continue
                }

                # Inject Autopilot config
                if (-not $SkipAutoPilot) {
                    Publish-ProxmoxAutoPilotConfig -PveTicket $ticket -Node $vm.Node -Vmid $vm.Vmid -ClientPath $clientPath
                }

                # Capture hardware hash
                if ($CaptureHardwareHash) {
                    $hashResult = Get-ProxmoxVMHardwareHash -PveTicket $ticket -Node $vm.Node -Vmid $vm.Vmid -VMName $vm.Name -ClientPath $clientPath
                    if ($hashResult) {
                        $vm.HashFile = $hashResult.HashFile
                    }
                }
            }
        }
        #endregion

        #region Summary
        Write-Host "`n==== Summary ====" -ForegroundColor Cyan
        $createdVMs | ForEach-Object {
            $serialInfo = if ($_.SerialNumber) { " | Serial: $($_.SerialNumber)" } else { '' }
            $oemInfo = if ($_.OemProfile) { " | OEM: $($_.OemProfile)" } else { '' }
            $hashInfo = if ($_.HashFile) { " | Hash: $($_.HashFile)" } else { '' }
            Write-Host "  $($_.Name) (VMID: $($_.Vmid)) - $($_.Status)$serialInfo$oemInfo$hashInfo" -ForegroundColor White
        }
        Write-Host "===============================" -ForegroundColor Cyan
        #endregion

        return $createdVMs
    }
    catch {
        Write-Error "Error in New-ProxmoxClientVM: $_"
        throw
    }
}
