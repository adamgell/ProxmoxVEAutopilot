function Import-RequiredModule {
    <#
    .SYNOPSIS
        Imports a module only if it's not already loaded
    
    .DESCRIPTION
        Checks if a module is already imported before attempting to import it.
        Optionally installs the module if not available.
    
    .PARAMETER ModuleName
        The name of the module to import
    
    .PARAMETER MinimumVersion
        The minimum version required
    
    .PARAMETER Install
        If specified, installs the module if not available
    
    .PARAMETER UseWindowsPowerShell
        For PowerShell 7, imports using Windows PowerShell compatibility
    
    .EXAMPLE
        Import-RequiredModule -ModuleName 'Microsoft.Graph.Authentication' -MinimumVersion '2.0.0'
    #>
    [CmdletBinding()]
    param (
        [Parameter(Mandatory = $true)]
        [string]$ModuleName,
        
        [Parameter()]
        [version]$MinimumVersion,
        
        [Parameter()]
        [switch]$Install,
        
        [Parameter()]
        [switch]$UseWindowsPowerShell
    )
    
    try {
        # Check if module is already imported with required version
        $importedModule = Get-Module -Name $ModuleName
        if ($importedModule) {
            if ($MinimumVersion) {
                if ($importedModule.Version -ge $MinimumVersion) {
                    Write-Verbose "Module '$ModuleName' version $($importedModule.Version) is already imported"
                    return $true
                }
                else {
                    Write-Verbose "Module '$ModuleName' is imported but version $($importedModule.Version) is less than required $MinimumVersion"
                    Remove-Module -Name $ModuleName -Force -ErrorAction SilentlyContinue
                }
            }
            else {
                Write-Verbose "Module '$ModuleName' is already imported"
                return $true
            }
        }
        
        # Check if module is available
        $availableModule = Get-Module -ListAvailable -Name $ModuleName | 
            Where-Object { 
                if ($MinimumVersion) { $_.Version -ge $MinimumVersion } else { $true }
            } | 
            Sort-Object Version -Descending | 
            Select-Object -First 1
        
        if (-not $availableModule -and $Install) {
            Write-Verbose "Installing module '$ModuleName'..."
            $installParams = @{
                Name = $ModuleName
                Force = $true
                AllowClobber = $true
                ErrorAction = 'Stop'
            }
            if ($MinimumVersion) {
                $installParams['MinimumVersion'] = $MinimumVersion
            }
            Install-Module @installParams
            
            # Re-check availability after installation
            $availableModule = Get-Module -ListAvailable -Name $ModuleName | 
                Where-Object { 
                    if ($MinimumVersion) { $_.Version -ge $MinimumVersion } else { $true }
                } | 
                Sort-Object Version -Descending | 
                Select-Object -First 1
        }
        
        if (-not $availableModule) {
            throw "Module '$ModuleName' is not available. Use -Install switch to install it."
        }
        
        # Import the module
        Write-Verbose "Importing module '$ModuleName' version $($availableModule.Version)..."
        
        if ($PSVersionTable.PSVersion.Major -eq 7 -and $UseWindowsPowerShell) {
            # For PS7 with Windows PowerShell compatibility
            Import-Module -Name (Split-Path $availableModule.ModuleBase -Parent) -UseWindowsPowerShell -ErrorAction Stop 3>$null
        }
        else {
            # Standard import
            $importParams = @{
                Name = $ModuleName
                Force = $true
                ErrorAction = 'Stop'
            }
            if ($MinimumVersion) {
                $importParams['MinimumVersion'] = $MinimumVersion
            }
            Import-Module @importParams
        }
        
        Write-Verbose "Successfully imported module '$ModuleName'"
        return $true
    }
    catch {
        Write-Warning "Failed to import module '$ModuleName': $_"
        return $false
    }
}