function Set-TenantAuthCredentials {
    <#
    .SYNOPSIS
        Configure Azure AD app registration credentials for a tenant

    .DESCRIPTION
        Interactive menu to set up and manage Azure AD app registration credentials
        for automated Autopilot profile retrieval. Credentials are stored securely
        in the tenant's .hvtools directory structure.

    .PARAMETER TenantName
        The name of the tenant to configure credentials for

    .PARAMETER AppId
        Azure AD Application (client) ID. If not provided, will prompt interactively.

    .PARAMETER TenantId
        Azure AD Directory (tenant) ID. If not provided, will prompt interactively.

    .PARAMETER AppSecret
        Azure AD Client Secret as SecureString. If not provided, will prompt interactively.

    .PARAMETER Remove
        Remove stored credentials for the tenant

    .PARAMETER Show
        Display currently stored credentials (AppId and TenantId only, not secret)

    .EXAMPLE
        Set-TenantAuthCredentials -TenantName "Contoso"

        Interactive setup for Contoso tenant credentials

    .EXAMPLE
        Set-TenantAuthCredentials -TenantName "Contoso" -Show

        Display currently stored credentials for Contoso

    .EXAMPLE
        Set-TenantAuthCredentials -TenantName "Contoso" -Remove

        Remove stored credentials for Contoso tenant

    .EXAMPLE
        $secret = Read-Host "Enter secret" -AsSecureString
        Set-TenantAuthCredentials -TenantName "Contoso" -AppId "..." -TenantId "..." -AppSecret $secret

        Non-interactive credential setup
    #>
    [CmdletBinding(DefaultParameterSetName = 'Interactive')]
    param (
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$TenantName,

        [Parameter(Mandatory = $false, ParameterSetName = 'NonInteractive')]
        [string]$AppId,

        [Parameter(Mandatory = $false, ParameterSetName = 'NonInteractive')]
        [string]$TenantId,

        [Parameter(Mandatory = $false, ParameterSetName = 'NonInteractive')]
        [securestring]$AppSecret,

        [Parameter(Mandatory = $false, ParameterSetName = 'Remove')]
        [switch]$Remove,

        [Parameter(Mandatory = $false, ParameterSetName = 'Show')]
        [switch]$Show
    )

    try {
        # Load configuration - FIXED: Use correct function name
        $script:hvConfig = Get-APHVToolsConfig
        $tenantPath = Join-Path $script:hvConfig.vmPath $TenantName
        $hvToolsPath = Join-Path $tenantPath ".hvtools"
        $credsPath = Join-Path $hvToolsPath "appcredentials.xml"

        # Handle Show parameter
        if ($Show) {
            if (Test-Path $credsPath) {
                Write-Host "`n📋 Stored Credentials for Tenant: $TenantName" -ForegroundColor Cyan
                Write-Host "=" * 60 -ForegroundColor Cyan

                $creds = Import-Clixml -Path $credsPath
                Write-Host "Application ID: $($creds.AppId)" -ForegroundColor White
                Write-Host "Tenant ID: $($creds.TenantId)" -ForegroundColor White
                Write-Host "Client Secret: ********** (hidden)" -ForegroundColor Gray
                Write-Host "Storage Path: $credsPath" -ForegroundColor Gray
                Write-Host ""
                return
            }
            else {
                Write-Host "No credentials stored for tenant: $TenantName" -ForegroundColor Yellow
                Write-Host "Run without -Show to configure credentials" -ForegroundColor Gray
                return
            }
        }

        # Handle Remove parameter
        if ($Remove) {
            if (Test-Path $credsPath) {
                $confirm = Read-Host "Are you sure you want to remove stored credentials for $TenantName? (Y/N)"
                if ($confirm -eq 'Y' -or $confirm -eq 'y') {
                    Remove-Item $credsPath -Force
                    Write-Host "✅ Credentials removed for tenant: $TenantName" -ForegroundColor Green
                }
                else {
                    Write-Host "Removal cancelled" -ForegroundColor Yellow
                }
            }
            else {
                Write-Host "No credentials found to remove for tenant: $TenantName" -ForegroundColor Yellow
            }
            return
        }

        # Non-interactive mode
        if ($AppId -and $TenantId -and $AppSecret) {
            # Ensure directory exists
            if (!(Test-Path $hvToolsPath)) {
                New-Item -Path $hvToolsPath -ItemType Directory -Force | Out-Null
            }

            # Store credentials
            $credObject = [PSCustomObject]@{
                AppId = $AppId
                TenantId = $TenantId
                AppSecret = $AppSecret
                StoredDate = Get-Date
            }

            $credObject | Export-Clixml -Path $credsPath -Force
            Write-Host "✅ Credentials saved for tenant: $TenantName" -ForegroundColor Green
            Write-Host "Location: $credsPath" -ForegroundColor Gray
            return
        }

        # Interactive mode - Show menu
        Write-Host ""
        Write-Host "╔════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
        Write-Host "║  Azure AD App Registration Credential Setup               ║" -ForegroundColor Cyan
        Write-Host "╚════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "Tenant: $TenantName" -ForegroundColor White
        Write-Host "Storage: $credsPath" -ForegroundColor Gray
        Write-Host ""

        # Check if credentials already exist
        if (Test-Path $credsPath) {
            Write-Host "⚠️  Credentials already exist for this tenant" -ForegroundColor Yellow
            Write-Host ""

            $existingCreds = Import-Clixml -Path $credsPath
            Write-Host "Current Configuration:" -ForegroundColor Cyan
            Write-Host "  Application ID: $($existingCreds.AppId)" -ForegroundColor Gray
            Write-Host "  Tenant ID: $($existingCreds.TenantId)" -ForegroundColor Gray
            Write-Host "  Stored Date: $($existingCreds.StoredDate)" -ForegroundColor Gray
            Write-Host ""

            $overwrite = Read-Host "Do you want to overwrite existing credentials? (Y/N)"
            if ($overwrite -ne 'Y' -and $overwrite -ne 'y') {
                Write-Host "Setup cancelled. Existing credentials preserved." -ForegroundColor Yellow
                return
            }
            Write-Host ""
        }

        # Display setup instructions
        Write-Host "📝 You will need the following from Azure Portal:" -ForegroundColor Cyan
        Write-Host "   1. Application (client) ID" -ForegroundColor White
        Write-Host "   2. Directory (tenant) ID" -ForegroundColor White
        Write-Host "   3. Client secret VALUE" -ForegroundColor White
        Write-Host ""
        Write-Host "💡 Setup Guide:" -ForegroundColor Cyan
        Write-Host "   • Portal: https://portal.azure.com" -ForegroundColor Gray
        Write-Host "   • Navigate: Azure AD > App registrations > Your App" -ForegroundColor Gray
        Write-Host "   • Copy IDs from the Overview page" -ForegroundColor Gray
        Write-Host "   • Create secret in Certificates & secrets" -ForegroundColor Gray
        Write-Host ""
        Write-Host "📚 Detailed docs: docs\AUTOPILOT-AUTHENTICATION.md" -ForegroundColor Gray
        Write-Host ""
        Write-Host "─" * 60 -ForegroundColor Gray
        Write-Host ""

        # Collect credentials interactively
        $newAppId = Read-Host "Enter Application (client) ID"
        if ([string]::IsNullOrWhiteSpace($newAppId)) {
            Write-Host "❌ Application ID is required" -ForegroundColor Red
            return
        }

        $newTenantId = Read-Host "Enter Directory (tenant) ID"
        if ([string]::IsNullOrWhiteSpace($newTenantId)) {
            Write-Host "❌ Tenant ID is required" -ForegroundColor Red
            return
        }

        $newAppSecret = Read-Host "Enter Client Secret VALUE" -AsSecureString
        if ($newAppSecret.Length -eq 0) {
            Write-Host "❌ Client Secret is required" -ForegroundColor Red
            return
        }

        Write-Host ""
        Write-Host "🔍 Validating input..." -ForegroundColor Cyan

        # Basic validation
        if ($newAppId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
            Write-Host "⚠️  Application ID doesn't look like a valid GUID" -ForegroundColor Yellow
            $continue = Read-Host "Continue anyway? (Y/N)"
            if ($continue -ne 'Y' -and $continue -ne 'y') {
                return
            }
        }

        if ($newTenantId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
            Write-Host "⚠️  Tenant ID doesn't look like a valid GUID" -ForegroundColor Yellow
            $continue = Read-Host "Continue anyway? (Y/N)"
            if ($continue -ne 'Y' -and $continue -ne 'y') {
                return
            }
        }

        Write-Host ""
        Write-Host "📊 Summary:" -ForegroundColor Cyan
        Write-Host "  Tenant Name: $TenantName" -ForegroundColor White
        Write-Host "  Application ID: $newAppId" -ForegroundColor White
        Write-Host "  Tenant ID: $newTenantId" -ForegroundColor White
        Write-Host "  Client Secret: **********" -ForegroundColor Gray
        Write-Host "  Storage Path: $credsPath" -ForegroundColor Gray
        Write-Host ""

        $confirm = Read-Host "Save these credentials? (Y/N)"
        if ($confirm -ne 'Y' -and $confirm -ne 'y') {
            Write-Host "Setup cancelled. No changes made." -ForegroundColor Yellow
            return
        }

        # Ensure directory exists
        if (!(Test-Path $hvToolsPath)) {
            New-Item -Path $hvToolsPath -ItemType Directory -Force | Out-Null
        }

        # Store credentials
        $credObject = [PSCustomObject]@{
            AppId = $newAppId
            TenantId = $newTenantId
            AppSecret = $newAppSecret
            StoredDate = Get-Date
        }

        $credObject | Export-Clixml -Path $credsPath -Force

        Write-Host ""
        Write-Host "✅ Success! Credentials saved securely" -ForegroundColor Green
        Write-Host ""
        Write-Host "🧪 Testing authentication..." -ForegroundColor Cyan

        # Offer to test
        $test = Read-Host "Do you want to test authentication now? (Y/N)"
        if ($test -eq 'Y' -or $test -eq 'y') {
            Write-Host ""
            Write-Host "Testing with -WhatIf (no VMs will be created)..." -ForegroundColor Yellow
            Write-Host ""

            try {
                New-ProxmoxClientVM -TenantName $TenantName -NumberOfVMs 1 -CPUsPerVM 2 `
                    -AppId $newAppId -TenantId $newTenantId -AppSecret $newAppSecret `
                    -WhatIf -Verbose -ErrorAction Stop

                Write-Host ""
                Write-Host "✅ Authentication test successful!" -ForegroundColor Green
            }
            catch {
                Write-Host ""
                Write-Host "❌ Authentication test failed!" -ForegroundColor Red
                Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
                Write-Host ""
                Write-Host "Troubleshooting:" -ForegroundColor Yellow
                Write-Host "  • Verify App ID and Tenant ID are correct" -ForegroundColor Gray
                Write-Host "  • Check client secret hasn't expired" -ForegroundColor Gray
                Write-Host "  • Ensure API permissions are granted with admin consent" -ForegroundColor Gray
                Write-Host "  • Review: docs\AUTOPILOT-AUTHENTICATION.md" -ForegroundColor Gray
                Write-Host ""

                $remove = Read-Host "Remove the saved credentials? (Y/N)"
                if ($remove -eq 'Y' -or $remove -eq 'y') {
                    Remove-Item $credsPath -Force
                    Write-Host "Credentials removed" -ForegroundColor Yellow
                }
            }
        }
        else {
            Write-Host ""
            Write-Host "💡 Next Steps:" -ForegroundColor Cyan
            Write-Host "  1. Test authentication:" -ForegroundColor White
            Write-Host "     New-ProxmoxClientVM -TenantName '$TenantName' -NumberOfVMs 1 -CPUsPerVM 2 -WhatIf" -ForegroundColor Gray
            Write-Host ""
            Write-Host "  2. Create VMs (will use stored credentials automatically):" -ForegroundColor White
            Write-Host "     New-ProxmoxClientVM -TenantName '$TenantName' -NumberOfVMs 5 -CPUsPerVM 4" -ForegroundColor Gray
            Write-Host ""
            Write-Host "  3. View stored credentials:" -ForegroundColor White
            Write-Host "     Set-TenantAuthCredentials -TenantName '$TenantName' -Show" -ForegroundColor Gray
            Write-Host ""
        }

    }
    catch {
        Write-Error "Failed to configure credentials: $($_.Exception.Message)"
        throw
    }
}
