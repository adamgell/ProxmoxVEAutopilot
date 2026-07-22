#!/usr/bin/env pwsh
<#
.SYNOPSIS
Create a ProxmoxVEAutopilot Entra app registration for a tenant.

.DESCRIPTION
Creates a single-tenant web app registration, configures redirect URIs for
ProxmoxVEAutopilot authentication, adds Microsoft Graph application
permissions for Intune/Autopilot hash upload, grants admin consent by default,
creates a client secret, and writes the three vault_entra_* values to a YAML
file that can be swapped into inventory/group_vars/all/vault.yml.

Secret text is written only to the output vault file. It is not printed.

.EXAMPLE
pwsh ./scripts/new_entra_app_registration.ps1 `
  -TenantId "00000000-0000-0000-0000-000000000000" `
  -OutputPath "./inventory/group_vars/all/vault_entra-customer.yml"
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string] $TenantId,

    [string] $DisplayName = "ProxmoxVEAutopilot",

    [string[]] $RedirectUri = @(
        "https://autopilot.gell.one/auth/callback",
        "http://192.168.2.4:5000/auth/callback",
        "http://localhost:5000/auth/callback"
    ),

    [string] $OutputPath = "./vault_entra.yml",

    [string] $SecretDisplayName = "ProxmoxVEAutopilot vault secret",

    [ValidateRange(1, 36)]
    [int] $SecretMonths = 12,

    [string[]] $GraphApplicationPermissions = @(
        "DeviceManagementServiceConfig.ReadWrite.All",
        "DeviceManagementManagedDevices.ReadWrite.All",
        "Device.ReadWrite.All",
        "Organization.Read.All"
    ),

    [string[]] $GraphDelegatedScopes = @(
        "openid",
        "profile",
        "email"
    ),

    [switch] $SkipAdminConsent
)

$ErrorActionPreference = "Stop"

function Assert-ModuleAvailable {
    param([Parameter(Mandatory = $true)] [string] $Name)

    if (-not (Get-Module -ListAvailable -Name $Name)) {
        throw "Missing PowerShell module '$Name'. Install it with: Install-Module Microsoft.Graph -Scope CurrentUser"
    }
}

function Invoke-GraphJson {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("GET", "POST", "PATCH")] [string] $Method,
        [Parameter(Mandatory = $true)] [string] $Uri,
        [object] $Body = $null
    )

    $request = @{
        Method = $Method
        Uri = $Uri
    }
    if ($null -ne $Body) {
        $request.Body = ($Body | ConvertTo-Json -Depth 20)
        $request.ContentType = "application/json"
    }
    Invoke-MgGraphRequest @request
}

function Get-GraphServicePrincipal {
    $graphAppId = "00000003-0000-0000-c000-000000000000"
    $encodedFilter = [uri]::EscapeDataString("appId eq '$graphAppId'")
    $response = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$encodedFilter"
    $principal = @($response.value)[0]
    if (-not $principal) {
        throw "Microsoft Graph service principal was not found in tenant $TenantId."
    }
    return $principal
}

function Get-GraphApplicationRole {
    param(
        [Parameter(Mandatory = $true)] $GraphServicePrincipal,
        [Parameter(Mandatory = $true)] [string] $PermissionName
    )

    $role = @($GraphServicePrincipal.appRoles | Where-Object {
        $_.value -eq $PermissionName -and @($_.allowedMemberTypes) -contains "Application"
    })[0]
    if (-not $role) {
        throw "Microsoft Graph application permission '$PermissionName' was not found."
    }
    return $role
}

function Get-GraphDelegatedScope {
    param(
        [Parameter(Mandatory = $true)] $GraphServicePrincipal,
        [Parameter(Mandatory = $true)] [string] $ScopeName
    )

    $scope = @($GraphServicePrincipal.oauth2PermissionScopes | Where-Object {
        $_.value -eq $ScopeName
    })[0]
    if (-not $scope) {
        throw "Microsoft Graph delegated scope '$ScopeName' was not found."
    }
    return $scope
}

function New-ProxmoxVEAutopilotApplication {
    param(
        [Parameter(Mandatory = $true)] $GraphServicePrincipal,
        [Parameter(Mandatory = $true)] [array] $PermissionRoles,
        [Parameter(Mandatory = $true)] [array] $DelegatedScopes
    )

    $graphAppId = "00000003-0000-0000-c000-000000000000"
    $roleAccess = @($PermissionRoles | ForEach-Object {
        @{
            id = $_.id
            type = "Role"
        }
    })
    $scopeAccess = @($DelegatedScopes | ForEach-Object {
        @{
            id = $_.id
            type = "Scope"
        }
    })
    $resourceAccess = @($roleAccess) + @($scopeAccess)
    $body = @{
        displayName = $DisplayName
        signInAudience = "AzureADMyOrg"
        web = @{
            redirectUris = @($RedirectUri)
            implicitGrantSettings = @{
                enableAccessTokenIssuance = $false
                enableIdTokenIssuance = $true
            }
        }
        requiredResourceAccess = @(
            @{
                resourceAppId = $graphAppId
                resourceAccess = $resourceAccess
            }
        )
    }

    Invoke-GraphJson -Method POST -Uri "https://graph.microsoft.com/v1.0/applications" -Body $body
}

function New-ApplicationServicePrincipal {
    param([Parameter(Mandatory = $true)] [string] $AppId)

    try {
        return Invoke-GraphJson -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals" -Body @{
            appId = $AppId
        }
    } catch {
        $encodedFilter = [uri]::EscapeDataString("appId eq '$AppId'")
        $response = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$encodedFilter"
        $principal = @($response.value)[0]
        if (-not $principal) {
            throw
        }
        return $principal
    }
}

function Grant-GraphApplicationRoles {
    param(
        [Parameter(Mandatory = $true)] $AppServicePrincipal,
        [Parameter(Mandatory = $true)] $GraphServicePrincipal,
        [Parameter(Mandatory = $true)] [array] $PermissionRoles
    )

    if ($SkipAdminConsent) {
        Write-Host "Skipping admin consent/app role assignments."
        return
    }

    $assignments = Invoke-GraphJson -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($AppServicePrincipal.id)/appRoleAssignments"
    foreach ($role in $PermissionRoles) {
        $existing = @($assignments.value | Where-Object {
            $_.resourceId -eq $GraphServicePrincipal.id -and $_.appRoleId -eq $role.id
        })
        if ($existing.Count -gt 0) {
            Write-Host "Graph application permission already granted: $($role.value)"
            continue
        }
        Invoke-GraphJson -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($AppServicePrincipal.id)/appRoleAssignments" -Body @{
            principalId = $AppServicePrincipal.id
            resourceId = $GraphServicePrincipal.id
            appRoleId = $role.id
        } | Out-Null
        Write-Host "Granted Graph application permission: $($role.value)"
    }
}

function New-ApplicationSecret {
    param([Parameter(Mandatory = $true)] [string] $ApplicationObjectId)

    $endDate = (Get-Date).ToUniversalTime().AddMonths($SecretMonths)
    Invoke-GraphJson -Method POST -Uri "https://graph.microsoft.com/v1.0/applications/$ApplicationObjectId/addPassword" -Body @{
        passwordCredential = @{
            displayName = $SecretDisplayName
            endDateTime = $endDate.ToString("o")
        }
    }
}

function ConvertTo-YamlSingleQuotedValue {
    param([Parameter(Mandatory = $true)] [string] $Value)
    "'" + ($Value -replace "'", "''") + "'"
}

function Write-VaultFile {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] [string] $AppId,
        [Parameter(Mandatory = $true)] [string] $Tenant,
        [Parameter(Mandatory = $true)] [string] $SecretText
    )

    $fullPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $parent = Split-Path -Parent $fullPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $lines = @(
        "---",
        "# Generated by scripts/new_entra_app_registration.ps1",
        "# Copy these values into inventory/group_vars/all/vault.yml when switching tenants.",
        "# Keep this file out of git and encrypt long-lived copies with ansible-vault.",
        "vault_entra_app_id: $(ConvertTo-YamlSingleQuotedValue $AppId)",
        "vault_entra_tenant_id: $(ConvertTo-YamlSingleQuotedValue $Tenant)",
        "vault_entra_app_secret: $(ConvertTo-YamlSingleQuotedValue $SecretText)"
    )
    Set-Content -LiteralPath $fullPath -Value $lines -Encoding UTF8 -NoNewline:$false
    return $fullPath
}

Assert-ModuleAvailable -Name "Microsoft.Graph.Authentication"

$scopes = @(
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "Directory.Read.All"
)

Write-Host "Connecting to Microsoft Graph tenant $TenantId..."
Connect-MgGraph -TenantId $TenantId -Scopes $scopes -NoWelcome | Out-Null

try {
    $context = Get-MgContext
    $effectiveTenantId = if ($context.TenantId) { $context.TenantId } else { $TenantId }
    $graphServicePrincipal = Get-GraphServicePrincipal
    $permissionRoles = @($GraphApplicationPermissions | ForEach-Object {
        Get-GraphApplicationRole -GraphServicePrincipal $graphServicePrincipal -PermissionName $_
    })
    $delegatedScopes = @($GraphDelegatedScopes | ForEach-Object {
        Get-GraphDelegatedScope -GraphServicePrincipal $graphServicePrincipal -ScopeName $_
    })

    if ($PSCmdlet.ShouldProcess($effectiveTenantId, "Create ProxmoxVEAutopilot Entra application '$DisplayName'")) {
        $application = New-ProxmoxVEAutopilotApplication -GraphServicePrincipal $graphServicePrincipal -PermissionRoles $permissionRoles -DelegatedScopes $delegatedScopes
        $servicePrincipal = New-ApplicationServicePrincipal -AppId $application.appId
        Grant-GraphApplicationRoles -AppServicePrincipal $servicePrincipal -GraphServicePrincipal $graphServicePrincipal -PermissionRoles $permissionRoles
        $secret = New-ApplicationSecret -ApplicationObjectId $application.id
        $vaultPath = Write-VaultFile -Path $OutputPath -AppId $application.appId -Tenant $effectiveTenantId -SecretText $secret.secretText

        Write-Host ""
        Write-Host "Created app registration: $DisplayName"
        Write-Host "Application/client ID: $($application.appId)"
        Write-Host "Tenant ID: $effectiveTenantId"
        Write-Host "Redirect URI count: $(@($RedirectUri).Count)"
        Write-Host "Vault output: $vaultPath"
        Write-Host "Client secret value was written to the vault output file and was not printed."
    }
} finally {
    Disconnect-MgGraph | Out-Null
}
