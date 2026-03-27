function Get-DefaultOemProfiles {
    <#
    .SYNOPSIS
        Returns the 13 built-in default OEM profiles for seeding into config
    #>
    return @(
        [pscustomobject]@{ profileName = 'lenovo-p520';        manufacturer = 'Lenovo';                  product = 'ThinkStation P520';            family = 'ThinkStation'; sku = '30BFS44D00';      chassisType = 3 }
        [pscustomobject]@{ profileName = 'lenovo-t14';         manufacturer = 'Lenovo';                  product = 'ThinkPad T14 Gen 4';           family = 'ThinkPad';     sku = '21HES06600';      chassisType = 10 }
        [pscustomobject]@{ profileName = 'lenovo-x1carbon';    manufacturer = 'Lenovo';                  product = 'ThinkPad X1 Carbon Gen 11';    family = 'ThinkPad';     sku = '21HMCTO1WW';      chassisType = 10 }
        [pscustomobject]@{ profileName = 'dell-optiplex-7090'; manufacturer = 'Dell Inc.';               product = 'OptiPlex 7090';                family = 'OptiPlex';     sku = '0YNTKM';          chassisType = 3 }
        [pscustomobject]@{ profileName = 'dell-latitude-5540'; manufacturer = 'Dell Inc.';               product = 'Latitude 5540';                family = 'Latitude';     sku = '0DV6GY';          chassisType = 10 }
        [pscustomobject]@{ profileName = 'dell-xps-15';        manufacturer = 'Dell Inc.';               product = 'XPS 15 9530';                  family = 'XPS';          sku = '0284MX';          chassisType = 10 }
        [pscustomobject]@{ profileName = 'hp-elitedesk-800';   manufacturer = 'HP';                      product = 'HP EliteDesk 800 G8 SFF';      family = 'EliteDesk';    sku = '0A60h';           chassisType = 3 }
        [pscustomobject]@{ profileName = 'hp-elitebook-840';   manufacturer = 'HP';                      product = 'HP EliteBook 840 G10';         family = 'EliteBook';    sku = '215E';            chassisType = 10 }
        [pscustomobject]@{ profileName = 'hp-zbook-g10';       manufacturer = 'HP';                      product = 'HP ZBook Fury 16 G10';         family = 'ZBook';        sku = '215A';            chassisType = 10 }
        [pscustomobject]@{ profileName = 'surface-pro-10';     manufacturer = 'Microsoft Corporation';   product = 'Surface Pro 10';               family = 'Surface';      sku = 'Surface_Pro_10';  chassisType = 9 }
        [pscustomobject]@{ profileName = 'surface-laptop-6';   manufacturer = 'Microsoft Corporation';   product = 'Surface Laptop 6';             family = 'Surface';      sku = 'Surface_Laptop_6'; chassisType = 10 }
        [pscustomobject]@{ profileName = 'generic-desktop';    manufacturer = 'Proxmox';                 product = 'Virtual Desktop';              family = 'Virtual';      sku = 'YOURSKU';         chassisType = 3 }
        [pscustomobject]@{ profileName = 'generic-laptop';     manufacturer = 'Proxmox';                 product = 'Virtual Laptop';               family = 'Virtual';      sku = 'YOURSKU';         chassisType = 10 }
    )
}

function Get-OemProfile {
    <#
    .SYNOPSIS
        Returns OEM hardware profiles for SMBIOS configuration on Proxmox VMs

    .DESCRIPTION
        Retrieves OEM hardware profiles from the APHVTools configuration. Profiles are
        stored in hvconfig.json and can be managed with Add-OemProfileToConfig and
        Remove-OemProfileFromConfig. If no profiles exist in config, the 13 built-in
        defaults are auto-seeded on first use.

    .PARAMETER ProfileName
        Name of the OEM profile to retrieve (e.g. "lenovo-t14", "dell-latitude-5540").

    .PARAMETER List
        List all available profiles in a formatted table.

    .EXAMPLE
        Get-OemProfile -List

    .EXAMPLE
        Get-OemProfile -ProfileName "lenovo-t14"
    #>
    [CmdletBinding(DefaultParameterSetName = 'ByName')]
    param (
        [Parameter(ParameterSetName = 'ByName', Position = 0)]
        [string]$ProfileName,

        [Parameter(ParameterSetName = 'List')]
        [switch]$List
    )

    # Chassis type labels
    $chassisLabels = @{
        1 = 'Other'; 3 = 'Desktop'; 9 = 'Laptop'; 10 = 'Notebook'
        13 = 'All-in-One'; 31 = 'Convertible'; 35 = 'Mini PC'; 36 = 'Tablet'
    }

    # Load profiles from config, auto-seeding defaults if empty
    if (-not $script:hvConfig) {
        throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
    }

    if (-not $script:hvConfig.PSObject.Properties.Name.Contains('oemProfiles') -or
        -not $script:hvConfig.oemProfiles -or
        @($script:hvConfig.oemProfiles).Count -eq 0) {

        Write-Verbose "No OEM profiles found in config. Seeding defaults..."
        $defaults = Get-DefaultOemProfiles

        if (-not $script:hvConfig.PSObject.Properties.Name.Contains('oemProfiles')) {
            $script:hvConfig | Add-Member -MemberType NoteProperty -Name 'oemProfiles' -Value $defaults
        }
        else {
            $script:hvConfig.oemProfiles = $defaults
        }

        # Save to disk if config path is available
        if ($script:hvConfig.hvConfigPath) {
            $script:hvConfig | ConvertTo-Json -Depth 20 | Out-File -FilePath $script:hvConfig.hvConfigPath -Encoding ascii -Force
        }
    }

    $profiles = @($script:hvConfig.oemProfiles)

    if ($List) {
        $result = foreach ($p in $profiles | Sort-Object -Property profileName) {
            $label = if ($chassisLabels.ContainsKey([int]$p.chassisType)) { $chassisLabels[[int]$p.chassisType] } else { 'Other' }
            [PSCustomObject]@{
                ProfileName  = $p.profileName
                Manufacturer = $p.manufacturer
                Product      = $p.product
                Family       = $p.family
                SKU          = $p.sku
                ChassisType  = [int]$p.chassisType
                ChassisLabel = $label
            }
        }
        return $result
    }

    if (-not $ProfileName) {
        throw "Specify -ProfileName or use -List to see available profiles."
    }

    $match = $profiles | Where-Object { $_.profileName -eq $ProfileName }
    if (-not $match) {
        $available = ($profiles | Sort-Object -Property profileName | ForEach-Object { $_.profileName }) -join ', '
        throw "Unknown OEM profile '$ProfileName'. Available profiles: $available"
    }

    $label = if ($chassisLabels.ContainsKey([int]$match.chassisType)) { $chassisLabels[[int]$match.chassisType] } else { 'Other' }

    return [PSCustomObject]@{
        ProfileName  = $match.profileName
        Manufacturer = $match.manufacturer
        Product      = $match.product
        Family       = $match.family
        SKU          = $match.sku
        ChassisType  = [int]$match.chassisType
        ChassisLabel = $label
    }
}
