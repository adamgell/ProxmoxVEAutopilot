function Show-APHVToolsConfig {
    <#
    .SYNOPSIS
            Displays detailed APHVTools configuration information

    .DESCRIPTION
    Shows specific sections of the APHVTools configuration with detailed formatting options

    .PARAMETER Section
        The configuration section to display: All, Tenants, Images, Network, Tools, Paths

    .PARAMETER TenantName
        Filter to show configuration for a specific tenant

    .PARAMETER ImageName
        Filter to show configuration for a specific image

    .PARAMETER ExportPath
        Export the configuration to a file (JSON or CSV format based on extension)

    .EXAMPLE
        Show-APHVToolsConfig

        Shows all configuration sections

    .EXAMPLE
        Show-APHVToolsConfig -Section Tenants

        Shows only tenant configuration

    .EXAMPLE
        Show-APHVToolsConfig -TenantName "Contoso"

        Shows configuration specific to the Contoso tenant

    .EXAMPLE
        Show-APHVToolsConfig -ExportPath "C:\Temp\hvconfig.json"

        Exports the configuration to a JSON file
    #>
    [CmdletBinding()]
    param (
        [Parameter(Position = 0)]
        [ValidateSet('All', 'Tenants', 'Images', 'Paths', 'Summary', 'Proxmox', 'OemProfiles')]
        [string]$Section = 'All',

        [Parameter()]
        [string]$TenantName,

        [Parameter()]
        [string]$ImageName,

        [Parameter()]
        [string]$ExportPath
    )

    try {
        # Get the raw configuration
        $config = Get-APHVToolsConfig -Raw

        if (-not $config) {
            throw "Unable to retrieve APHVTools configuration"
        }

        # Filter by tenant if specified
        if ($TenantName) {
            $tenant = $config.tenantConfig | Where-Object { $_.TenantName -eq $TenantName }
            if (-not $tenant) {
                Write-Warning "Tenant '$TenantName' not found"
                return
            }

            Write-Host "`n==== Configuration for Tenant: $TenantName ====" -ForegroundColor Cyan
            Write-Host "Admin UPN: " -NoNewline -ForegroundColor Yellow
            Write-Host $tenant.AdminUpn
            Write-Host "Default Image: " -NoNewline -ForegroundColor Yellow
            Write-Host $tenant.ImageName
            Write-Host "Config Path: " -NoNewline -ForegroundColor Yellow
            Write-Host $tenant.pathToConfig

            # Show related image info
            $image = $config.images | Where-Object { $_.imageName -eq $tenant.ImageName }
            if ($image) {
                Write-Host "`nAssociated Image Details:" -ForegroundColor Green
                if ($image.imagePath) {
                    Write-Host "  ISO: " -NoNewline
                    Write-Host (Split-Path $image.imagePath -Leaf) -ForegroundColor White
                }
            }

            return
        }

        # Filter by image if specified
        if ($ImageName) {
            $image = $config.images | Where-Object { $_.imageName -eq $ImageName }
            if (-not $image) {
                Write-Warning "Image '$ImageName' not found"
                return
            }

            Write-Host "`n==== Configuration for Image: $ImageName ====" -ForegroundColor Cyan
            if ($image.imagePath) {
                Write-Host "ISO Path: " -ForegroundColor Yellow
                Write-Host "  $($image.imagePath)" -ForegroundColor White
            }

            # Show tenants using this image
            $tenantsUsingImage = $config.tenantConfig | Where-Object { $_.ImageName -eq $ImageName }
            if ($tenantsUsingImage) {
                Write-Host "`nTenants using this image:" -ForegroundColor Green
                $tenantsUsingImage | ForEach-Object {
                    Write-Host "  - $($_.TenantName)" -ForegroundColor White
                }
            }

            return
        }

        # Display based on section
        switch ($Section) {
            'Summary' {
                Write-Host "`n==== APHVTools Configuration Summary ====" -ForegroundColor Cyan
                Write-Host "Configuration File: " -NoNewline -ForegroundColor Yellow
                Write-Host $config.hvConfigPath
                Write-Host "Total Tenants: " -NoNewline -ForegroundColor Yellow
                Write-Host $config.tenantConfig.Count
                Write-Host "Total Images: " -NoNewline -ForegroundColor Yellow
                Write-Host $config.images.Count
            }

            'Paths' {
                Write-Host "`n==== Path Configuration ====" -ForegroundColor Cyan
                Write-Host "Configuration File:" -ForegroundColor Yellow
                Write-Host "  $($config.hvConfigPath)" -ForegroundColor White
                Write-Host "VM Storage Path:" -ForegroundColor Yellow
                Write-Host "  $($config.vmPath)" -ForegroundColor White
            }

            'Network' {
                Write-Warning "Network configuration section has been removed."
            }

            'Tenants' {
                Write-Host "`n==== Tenant Configuration ====" -ForegroundColor Cyan
                if ($config.tenantConfig -and $config.tenantConfig.Count -gt 0) {
                    $config.tenantConfig | ForEach-Object {
                        Write-Host "`nTenant: " -NoNewline -ForegroundColor Yellow
                        Write-Host $_.TenantName -ForegroundColor White
                        Write-Host "  Admin UPN: $($_.AdminUpn)"
                        Write-Host "  Default Image: $($_.ImageName)"
                        Write-Host "  Config Path: $(Split-Path $_.pathToConfig -Leaf)"
                    }
                }
                else {
                    Write-Host "No tenants configured" -ForegroundColor Yellow
                }
            }

            'Images' {
                Write-Host "`n==== Image Configuration ====" -ForegroundColor Cyan
                if ($config.images -and $config.images.Count -gt 0) {
                    $config.images | ForEach-Object {
                        Write-Host "`nImage: " -NoNewline -ForegroundColor Yellow
                        Write-Host $_.imageName -ForegroundColor White
                        if ($_.imagePath) {
                            Write-Host "  ISO: $(Split-Path $_.imagePath -Leaf)"
                            Write-Host "  Full ISO Path: $($_.imagePath)" -ForegroundColor DarkGray
                        }
                    }
                }
                else {
                    Write-Host "No images configured" -ForegroundColor Yellow
                }
            }\n\n            'Proxmox' {
                Write-Host "`n==== Proxmox Configuration ====" -ForegroundColor Cyan
                if ($config.proxmoxConfig) {
                    $px = $config.proxmoxConfig
                    Write-Host "Host: " -NoNewline -ForegroundColor Yellow
                    Write-Host $px.host
                    Write-Host "API Token: " -NoNewline -ForegroundColor Yellow
                    # Mask the token value for security
                    $tokenDisplay = if ($px.apiToken -and $px.apiToken.Length -gt 20) {
                        $px.apiToken.Substring(0, 15) + '...'
                    } else { $px.apiToken }
                    Write-Host $tokenDisplay
                    Write-Host "Default Node: " -NoNewline -ForegroundColor Yellow
                    Write-Host $px.defaultNode
                    Write-Host "Default Storage: " -NoNewline -ForegroundColor Yellow
                    Write-Host $px.defaultStorage
                    Write-Host "Default Bridge: " -NoNewline -ForegroundColor Yellow
                    Write-Host $px.defaultBridge
                    Write-Host "ISO Storage: " -NoNewline -ForegroundColor Yellow
                    Write-Host $px.isoStorage
                    if ($px.virtIOIsoPath) {
                        Write-Host "VirtIO ISO: " -NoNewline -ForegroundColor Yellow
                        Write-Host $px.virtIOIsoPath
                    }
                    Write-Host "Skip Cert Check: " -NoNewline -ForegroundColor Yellow
                    Write-Host $px.skipCertificateCheck
                }
                else {
                    Write-Host "Not configured" -ForegroundColor Yellow
                    Write-Host "Run Add-ProxmoxToConfig to set up Proxmox connection" -ForegroundColor DarkGray
                }
            }

            'OemProfiles' {
                Write-Host "`n==== OEM Profiles ====" -ForegroundColor Cyan
                if ($config.oemProfiles -and @($config.oemProfiles).Count -gt 0) {
                    $profileTable = @($config.oemProfiles) | Sort-Object -Property profileName | ForEach-Object {
                        [PSCustomObject]@{
                            Profile      = $_.profileName
                            Manufacturer = $_.manufacturer
                            Product      = $_.product
                            SKU          = $_.sku
                            Chassis      = $_.chassisType
                        }
                    }
                    $profileTable | Format-Table -AutoSize | Out-String | Write-Host
                }
                else {
                    Write-Host "No OEM profiles configured" -ForegroundColor Yellow
                    Write-Host "Run Add-OemProfileToConfig or Initialize-APHVTools to seed defaults" -ForegroundColor DarkGray
                }
            }

            'All' {
                # Show all sections
                'Summary', 'Paths', 'Proxmox', 'OemProfiles', 'Tenants', 'Images' | ForEach-Object {
                    Show-APHVToolsConfig -Section $_
                }
            }
        }

        # Export if requested
        if ($ExportPath) {
            $extension = [System.IO.Path]::GetExtension($ExportPath).ToLower()

            switch ($extension) {
                '.json' {
                    $config | ConvertTo-Json -Depth 10 | Out-File -FilePath $ExportPath -Encoding UTF8
                    Write-Host "`nConfiguration exported to: $ExportPath" -ForegroundColor Green
                }
                '.csv' {
                    # Create a flattened view for CSV
                    $flatConfig = @()

                    # Add tenant data
                    $config.tenantConfig | ForEach-Object {
                        $flatConfig += [PSCustomObject]@{
                            Type = 'Tenant'
                            Name = $_.TenantName
                            Value1 = $_.AdminUpn
                            Value2 = $_.ImageName
                            Value3 = $_.pathToConfig
                        }
                    }

                    # Add image data
                    $config.images | ForEach-Object {
                        $flatConfig += [PSCustomObject]@{
                            Type = 'Image'
                            Name = $_.imageName
                            Value1 = $_.imagePath
                            Value2 = ''
                            Value3 = ''
                        }
                    }

                    $flatConfig | Export-Csv -Path $ExportPath -NoTypeInformation
                    Write-Host "`nConfiguration exported to: $ExportPath" -ForegroundColor Green
                }
                default {
                    Write-Warning "Unsupported export format. Use .json or .csv extension"
                }
            }
        }
    }
    catch {
        Write-Warning "Error displaying configuration: $_"
    }
}