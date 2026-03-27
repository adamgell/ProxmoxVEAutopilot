function Remove-OemProfileFromConfig {
    <#
    .SYNOPSIS
        Removes an OEM hardware profile from the APHVTools configuration

    .DESCRIPTION
        Removes a named OEM SMBIOS profile from hvconfig.json. Warns if any tenants
        reference the profile but proceeds with removal.

    .PARAMETER ProfileName
        Name of the profile to remove.

    .EXAMPLE
        Remove-OemProfileFromConfig -ProfileName "generic-desktop"

    .EXAMPLE
        Remove-OemProfileFromConfig -ProfileName "my-custom-kiosk"
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ProfileName
    )
    try {
        if (-not $script:hvConfig -or -not $script:hvConfig.hvConfigPath) {
            throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
        }

        if (-not $script:hvConfig.PSObject.Properties.Name.Contains('oemProfiles') -or
            -not $script:hvConfig.oemProfiles) {
            throw "No OEM profiles found in configuration."
        }

        # Check profile exists
        $existing = @($script:hvConfig.oemProfiles) | Where-Object { $_.profileName -eq $ProfileName }
        if (-not $existing) {
            Write-Warning "OEM profile '$ProfileName' not found in configuration."
            return
        }

        # Warn about tenants referencing this profile
        if ($script:hvConfig.tenantConfig) {
            $referencingTenants = @($script:hvConfig.tenantConfig | Where-Object { $_.OemProfile -eq $ProfileName })
            if ($referencingTenants.Count -gt 0) {
                $tenantNames = ($referencingTenants | ForEach-Object { $_.TenantName }) -join ', '
                Write-Warning "The following tenants reference profile '$ProfileName': $tenantNames. Their OemProfile setting will become invalid."
            }
        }

        Write-Host "Removing OEM profile '$ProfileName' from config.. " -ForegroundColor Cyan -NoNewline

        if ($PSCmdlet.ShouldProcess($ProfileName, "Remove OEM profile from configuration")) {
            $script:hvConfig.oemProfiles = @($script:hvConfig.oemProfiles | Where-Object { $_.profileName -ne $ProfileName })
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
