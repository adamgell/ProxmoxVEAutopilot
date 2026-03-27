function Add-OemProfileToConfig {
    <#
    .SYNOPSIS
        Adds or updates an OEM hardware profile in the APHVTools configuration

    .DESCRIPTION
        Stores an OEM SMBIOS profile in hvconfig.json for use with Proxmox VM creation.
        Profiles define manufacturer, product, family, SKU, and chassis type fields
        that make VMs appear as real hardware for Autopilot/Intune testing.

    .PARAMETER ProfileName
        Unique name for the profile (e.g. "lenovo-t14", "my-custom-kiosk")

    .PARAMETER Manufacturer
        SMBIOS manufacturer (e.g. "Lenovo", "Dell Inc.", "HP")

    .PARAMETER Product
        SMBIOS product name (e.g. "ThinkPad T14 Gen 4")

    .PARAMETER Family
        SMBIOS family (e.g. "ThinkPad"). Defaults to Manufacturer if omitted.

    .PARAMETER SKU
        SMBIOS SKU number (e.g. "21HES06600"). Defaults to "YOURSKU" if omitted.

    .PARAMETER ChassisType
        SMBIOS chassis type. Common values: 3=Desktop, 9=Laptop, 10=Notebook,
        13=All-in-One, 31=Convertible, 35=Mini PC, 36=Tablet. Default: 3

    .PARAMETER Force
        Overwrite an existing profile with the same name.

    .EXAMPLE
        Add-OemProfileToConfig -ProfileName "my-kiosk" -Manufacturer "ACME Corp" -Product "Kiosk 3000"

    .EXAMPLE
        Add-OemProfileToConfig -ProfileName "lenovo-t14" -Manufacturer "Lenovo" -Product "ThinkPad T14 Gen 5" -Force
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ProfileName,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Manufacturer,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Product,

        [Parameter(Mandatory = $false)]
        [string]$Family,

        [Parameter(Mandatory = $false)]
        [string]$SKU,

        [Parameter(Mandatory = $false)]
        [ValidateRange(1, 36)]
        [int]$ChassisType = 3,

        [switch]$Force
    )
    try {
        if (-not $script:hvConfig -or -not $script:hvConfig.hvConfigPath) {
            throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
        }

        # Default Family to Manufacturer if not provided
        if (-not $Family) { $Family = $Manufacturer }
        # Default SKU if not provided
        if (-not $SKU) { $SKU = 'YOURSKU' }

        # Ensure oemProfiles property exists
        if (-not $script:hvConfig.PSObject.Properties.Name.Contains('oemProfiles')) {
            $script:hvConfig | Add-Member -MemberType NoteProperty -Name 'oemProfiles' -Value @()
        }
        if ($null -eq $script:hvConfig.oemProfiles) {
            $script:hvConfig.oemProfiles = @()
        }

        # Check for existing profile with same name
        $existing = @($script:hvConfig.oemProfiles) | Where-Object { $_.profileName -eq $ProfileName }
        if ($existing) {
            if (-not $Force) {
                Write-Warning "OEM profile '$ProfileName' already exists. Use -Force to overwrite."
                return
            }
            # Remove old entry
            $script:hvConfig.oemProfiles = @($script:hvConfig.oemProfiles | Where-Object { $_.profileName -ne $ProfileName })
        }

        Write-Host "Adding OEM profile '$ProfileName' to config.. " -ForegroundColor Cyan -NoNewline

        $newProfile = [pscustomobject]@{
            profileName  = $ProfileName
            manufacturer = $Manufacturer
            product      = $Product
            family       = $Family
            sku          = $SKU
            chassisType  = $ChassisType
        }

        if ($PSCmdlet.ShouldProcess($ProfileName, "Add OEM profile to configuration")) {
            $script:hvConfig.oemProfiles = @($script:hvConfig.oemProfiles) + $newProfile
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
