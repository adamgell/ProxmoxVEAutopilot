function Get-APHVToolsConfig {
    <#
    .SYNOPSIS
            Gets the APHVTools configuration with improved formatting

    .DESCRIPTION
    Retrieves and displays the APHVTools configuration in an organized, readable format

    .PARAMETER Raw
        Returns the raw configuration object without formatting

        .EXAMPLE
    Get-APHVToolsConfig

    Displays the configuration in a formatted view

    .EXAMPLE
    Get-APHVToolsConfig -Raw

        Returns the raw configuration object
    #>
    [cmdletbinding()]
    param (
        [switch]$Raw
    )
    try {
        $configPointerPath = "$env:USERPROFILE\.hvtoolscfgpath"
        if (-not (Test-Path -Path $configPointerPath -ErrorAction SilentlyContinue)) {
            throw "Could not find APHVTools configuration pointer file at $configPointerPath. Run Initialize-APHVTools first."
        }

        $resolvedConfigPath = Get-Content -Path $configPointerPath -ErrorAction Stop | Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($resolvedConfigPath)) {
            throw "Configuration pointer file is empty. Run Initialize-APHVTools first."
        }

        if (-not (Test-Path -Path $resolvedConfigPath -ErrorAction SilentlyContinue)) {
            throw "Could not find APHVTools configuration file at '$resolvedConfigPath'. Run Initialize-APHVTools first."
        }

        $configRaw = Get-Content -Path $resolvedConfigPath -Raw -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($configRaw)) {
            throw "APHVTools configuration file is empty at '$resolvedConfigPath'."
        }

        $script:hvConfig = $configRaw | ConvertFrom-Json
        if ($script:hvConfig) {

            if ($Raw) {
                return $script:hvConfig
            }

            # Create formatted output
            Write-Host "`n==== APHVTools Configuration ====" -ForegroundColor Cyan
            Write-Host "Configuration Path: " -NoNewline -ForegroundColor Yellow
            Write-Host $script:hvConfig.hvConfigPath

            Write-Host "`nVM Storage Path: " -NoNewline -ForegroundColor Yellow
            Write-Host $script:hvConfig.vmPath

            # Tenant Information
            Write-Host "`n---- Tenant Configuration ----" -ForegroundColor Green
            if ($script:hvConfig.tenantConfig -and $script:hvConfig.tenantConfig.Count -gt 0) {
                $tenantTable = $script:hvConfig.tenantConfig | ForEach-Object {
                    [PSCustomObject]@{
                        TenantName = $_.TenantName
                        AdminUPN = $_.AdminUpn
                        DefaultImage = $_.ImageName
                    }
                }
                $tenantTable | Format-Table -AutoSize | Out-String | Write-Host
            }
            else {
                Write-Host "No tenants configured" -ForegroundColor Yellow
            }

            # Image Information
            Write-Host "---- Image Configuration ----" -ForegroundColor Green
            if ($script:hvConfig.images -and $script:hvConfig.images.Count -gt 0) {
                $imageTable = $script:hvConfig.images | ForEach-Object {
                    [PSCustomObject]@{
                        ImageName = $_.imageName
                        ISOPath   = $_.imagePath
                    }
                }
                $imageTable | Format-Table -AutoSize | Out-String | Write-Host
            }
            else {
                Write-Host "No images configured" -ForegroundColor Yellow
            }

            # Proxmox Configuration
            Write-Host "---- Proxmox Configuration ----" -ForegroundColor Green
            if ($script:hvConfig.proxmoxConfig) {
                $px = $script:hvConfig.proxmoxConfig
                Write-Host "Host: " -NoNewline
                Write-Host $px.host -ForegroundColor White
                Write-Host "Default Node: " -NoNewline
                Write-Host $px.defaultNode -ForegroundColor White
                Write-Host "Storage: " -NoNewline
                Write-Host $px.defaultStorage -ForegroundColor White
                Write-Host "Bridge: " -NoNewline
                Write-Host $px.defaultBridge -ForegroundColor White
                if ($px.virtIOIsoPath) {
                    Write-Host "VirtIO ISO: " -NoNewline
                    Write-Host $px.virtIOIsoPath -ForegroundColor White
                }
                Write-Host "Skip Cert Check: " -NoNewline
                Write-Host $px.skipCertificateCheck -ForegroundColor White
            }
            else {
                Write-Host "Not configured (run Add-ProxmoxToConfig)" -ForegroundColor Yellow
            }

            # OEM Profiles
            Write-Host "`n---- OEM Profiles ----" -ForegroundColor Green
            if ($script:hvConfig.oemProfiles -and @($script:hvConfig.oemProfiles).Count -gt 0) {
                Write-Host "Profiles configured: " -NoNewline
                Write-Host @($script:hvConfig.oemProfiles).Count -ForegroundColor White
                $script:hvConfig.oemProfiles | Sort-Object -Property profileName | ForEach-Object {
                    Write-Host "  $($_.profileName): $($_.manufacturer) $($_.product)" -ForegroundColor Gray
                }
            }
            else {
                Write-Host "No OEM profiles configured" -ForegroundColor Yellow
            }

            Write-Host "`n===============================" -ForegroundColor Cyan

            # Return a summary object for pipeline use
            $summary = [PSCustomObject]@{
                ConfigPath      = $script:hvConfig.hvConfigPath
                VMPath          = $script:hvConfig.vmPath
                TenantCount     = @($script:hvConfig.tenantConfig).Count
                ImageCount      = @($script:hvConfig.images).Count
                ProxmoxHost     = if ($script:hvConfig.proxmoxConfig) { $script:hvConfig.proxmoxConfig.host } else { $null }
                ProxmoxNode     = if ($script:hvConfig.proxmoxConfig) { $script:hvConfig.proxmoxConfig.defaultNode } else { $null }
                OemProfileCount = if ($script:hvConfig.oemProfiles) { @($script:hvConfig.oemProfiles).Count } else { 0 }
            }

            return $summary
        }
        else {
            throw "Could not find APHVTools configuration data. Run Initialize-APHVTools to create the configuration file."
        }
    }
    catch {
        Write-Warning $_.Exception.Message
        return $null
    }
}