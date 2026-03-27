function Import-VMHashToAutopilot {
    <#
    .SYNOPSIS
        Uploads hardware hash CSV files to Intune for Windows Autopilot registration

    .DESCRIPTION
        Takes one or more hardware hash CSV files (captured by Get-VMHardwareHash) and
        imports them into Windows Autopilot via the Intune Graph API. Supports both
        interactive authentication and app-based (service principal) authentication.

        The CSV files must contain at minimum: "Device Serial Number" and "Hardware Hash"
        columns, matching the format produced by Get-WindowsAutoPilotInfo.

    .PARAMETER CsvPath
        Path to one or more hardware hash CSV files, or a directory containing them.
        When a directory is specified, all *_hwid.csv files in it are processed.

    .PARAMETER GroupTag
        Optional Autopilot group tag to apply to all imported devices

    .PARAMETER AppId
        Azure AD App Registration Application (Client) ID for app-based authentication

    .PARAMETER TenantId
        Azure AD Tenant (Directory) ID for app-based authentication

    .PARAMETER AppSecret
        Azure AD App Registration Client Secret as SecureString

    .PARAMETER AssignedUser
        Optional UPN of the user to assign to the devices

    .EXAMPLE
        Import-VMHashToAutopilot -CsvPath "C:\VMs\Contoso\.hvtools\HardwareHashes"

        Uploads all hash CSVs from the directory using interactive authentication

    .EXAMPLE
        Import-VMHashToAutopilot -CsvPath "C:\hash.csv" -GroupTag "BulkTest" `
            -AppId $appId -TenantId $tenantId -AppSecret $secret

        Uploads a single CSV using app-based authentication with a group tag
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [Parameter(Mandatory = $true, Position = 0)]
        [string[]]$CsvPath,

        [Parameter()]
        [string]$GroupTag,

        [Parameter()]
        [string]$AppId,

        [Parameter()]
        [string]$TenantId,

        [Parameter()]
        [securestring]$AppSecret,

        [Parameter()]
        [string]$AssignedUser
    )

    try {
        # Resolve CSV files
        $csvFiles = @()
        foreach ($path in $CsvPath) {
            if (Test-Path $path -PathType Container) {
                $csvFiles += Get-ChildItem -Path $path -Filter '*_hwid.csv' -File
            }
            elseif (Test-Path $path -PathType Leaf) {
                $csvFiles += Get-Item $path
            }
            else {
                Write-Warning "Path not found: $path"
            }
        }

        if ($csvFiles.Count -eq 0) {
            Write-Warning "No hardware hash CSV files found at the specified path(s)."
            return
        }

        Write-Host "Found $($csvFiles.Count) hash CSV file(s) to import" -ForegroundColor Cyan

        # Ensure WindowsAutoPilotIntune module is available
        $apModule = Import-Module WindowsAutopilotIntune -MinimumVersion 5.4.0 -PassThru -ErrorAction SilentlyContinue
        if (-not $apModule) {
            Write-Host "Installing WindowsAutopilotIntune module..." -ForegroundColor Yellow
            Install-Module WindowsAutopilotIntune -Force -Scope CurrentUser
            Import-Module WindowsAutopilotIntune -MinimumVersion 5.4.0
        }

        $graphAuth = Import-Module Microsoft.Graph.Authentication -PassThru -ErrorAction SilentlyContinue
        if (-not $graphAuth) {
            Write-Host "Installing Microsoft.Graph.Authentication module..." -ForegroundColor Yellow
            Install-Module Microsoft.Graph.Authentication -Force -Scope CurrentUser
            Import-Module Microsoft.Graph.Authentication
        }

        # Authenticate
        if ($AppId -and $TenantId -and $AppSecret) {
            $plainSecret = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($AppSecret)
            )
            Connect-MSGraphApp -Tenant $TenantId -AppId $AppId -AppSecret $plainSecret | Out-Null
            Write-Host "Connected to Intune tenant $TenantId via app-based auth" -ForegroundColor Green
        }
        else {
            Connect-MgGraph -Scopes "DeviceManagementServiceConfig.ReadWrite.All" | Out-Null
            $ctx = Get-MgContext
            Write-Host "Connected to Intune tenant $($ctx.TenantId) via interactive auth" -ForegroundColor Green
        }

        # Import each CSV
        $imported = 0
        $failed = 0

        foreach ($csv in $csvFiles) {
            $hashData = Import-Csv $csv.FullName

            foreach ($device in $hashData) {
                $serial = $device.'Device Serial Number'
                $hash = $device.'Hardware Hash'

                if (-not $hash) {
                    Write-Warning "No hardware hash found in $($csv.Name) for serial $serial — skipping"
                    $failed++
                    continue
                }

                if ($PSCmdlet.ShouldProcess("Serial: $serial", "Import to Autopilot")) {
                    try {
                        $importParams = @{
                            serialNumber       = $serial
                            hardwareIdentifier = $hash
                        }
                        if ($GroupTag) { $importParams.groupTag = $GroupTag }
                        if ($AssignedUser) { $importParams.assignedUser = $AssignedUser }

                        Add-AutopilotImportedDevice @importParams | Out-Null

                        Write-Host "  Imported: $serial" -ForegroundColor Green
                        $imported++
                    }
                    catch {
                        Write-Warning "  Failed to import $serial : $_"
                        $failed++
                    }
                }
            }
        }

        Write-Host "`nAutopilot import complete: $imported imported, $failed failed" -ForegroundColor Cyan

        if ($imported -gt 0) {
            Write-Host "Waiting for Autopilot sync to process devices..." -ForegroundColor Yellow
            Write-Host "  Check status: Get-AutopilotDevice | Select-Object serialNumber, deploymentProfileAssignmentStatus" -ForegroundColor Gray
        }
    }
    catch {
        Write-Error "Autopilot import failed: $_"
    }
}
