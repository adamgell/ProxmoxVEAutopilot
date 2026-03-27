function Get-TenantAuthCredentials {
    <#
    .SYNOPSIS
        Retrieves stored Azure AD app registration credentials for a tenant

    .DESCRIPTION
        Private helper function to load stored credentials from the tenant's
        .hvtools directory. Returns null if no credentials are stored.

    .PARAMETER TenantPath
        Full path to the tenant directory

    .PARAMETER TenantName
        Name of the tenant (for logging purposes)

    .OUTPUTS
        PSCustomObject with AppId, TenantId, and AppSecret properties, or $null if not found
    #>
    [CmdletBinding()]
    param (
        [Parameter(Mandatory = $true)]
        [string]$TenantPath,

        [Parameter(Mandatory = $false)]
        [string]$TenantName
    )

    try {
        $hvToolsPath = Join-Path $TenantPath ".hvtools"
        $credsPath = Join-Path $hvToolsPath "appcredentials.xml"

        if (Test-Path $credsPath) {
            Write-Verbose "Found stored credentials at: $credsPath"

            try {
                $creds = Import-Clixml -Path $credsPath

                # Validate credential object has required properties
                if ($creds.AppId -and $creds.TenantId -and $creds.AppSecret) {
                    Write-Verbose "Successfully loaded credentials for tenant: $TenantName"
                    Write-Verbose "  AppId: $($creds.AppId)"
                    Write-Verbose "  TenantId: $($creds.TenantId)"

                    # Log storage date if available
                    if ($creds.StoredDate) {
                        Write-Verbose "  Stored: $($creds.StoredDate)"
                    }

                    return $creds
                }
                else {
                    Write-Warning "Stored credentials file is missing required properties. Run Set-TenantAuthCredentials to reconfigure."
                    return $null
                }
            }
            catch {
                Write-Warning "Failed to load stored credentials: $($_.Exception.Message)"
                Write-Verbose "Credentials file may be corrupted. Run Set-TenantAuthCredentials to reconfigure."
                return $null
            }
        }
        else {
            Write-Verbose "No stored credentials found for tenant: $TenantName"
            Write-Verbose "Expected path: $credsPath"
            return $null
        }
    }
    catch {
        Write-Verbose "Error checking for stored credentials: $($_.Exception.Message)"
        return $null
    }
}
