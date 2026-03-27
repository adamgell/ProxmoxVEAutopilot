function Add-TenantToConfig {
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [parameter(Position = 1, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]
        $TenantName,

        [parameter(Position = 2, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]
        $ImageName,

        [parameter(Position = 3, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]
        $AdminUpn,

        [parameter(Position = 4, Mandatory = $false)]
        [string]
        $OemProfile
    )
    try {
        if (-not $script:hvConfig -or -not $script:hvConfig.hvConfigPath) {
            throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
        }

        Write-Host "Adding $TenantName to config.. " -ForegroundColor Cyan -NoNewline

        # Ensure the tenantConfig property exists
        if (-not $script:hvConfig.PSObject.Properties.Name.Contains('tenantConfig')) {
            $script:hvConfig | Add-Member -MemberType NoteProperty -Name 'tenantConfig' -Value @()
        }

        # Ensure tenantConfig is an array
        if ($null -eq $script:hvConfig.tenantConfig) {
            $script:hvConfig.tenantConfig = @()
        }

        # Check for existing tenant with same name
        $existing = @($script:hvConfig.tenantConfig) | Where-Object { $_.TenantName -eq $TenantName }
        if ($existing) {
            Write-Host "(already exists, updating).. " -ForegroundColor Yellow -NoNewline
            $script:hvConfig.tenantConfig = @($script:hvConfig.tenantConfig | Where-Object { $_.TenantName -ne $TenantName })
        }

        $newTenant = [pscustomobject]@{
            TenantName = $TenantName
            ImageName  = $ImageName
            AdminUpn   = $AdminUpn
            OemProfile = if ($OemProfile) { $OemProfile } else { $null }
        }
        if ($PSCmdlet.ShouldProcess($TenantName, "Add tenant configuration")) {
            $script:hvConfig.tenantConfig = @($script:hvConfig.tenantConfig) + $newTenant
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