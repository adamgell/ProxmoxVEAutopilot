function Add-ImageToConfig {
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [parameter(Position = 1, Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]
        $ImageName,
        [parameter(Position = 2, Mandatory = $false)]
        [ValidateNotNullOrEmpty()]
        [string]
        $IsoPath
    )
    try {
        if (-not $script:hvConfig -or -not $script:hvConfig.hvConfigPath) {
            throw "Unable to load APHVTools configuration. Run Initialize-APHVTools first."
        }

        Write-Host "Adding $ImageName to config.. " -ForegroundColor Cyan -NoNewline

        # Ensure the images property exists
        if (-not $script:hvConfig.PSObject.Properties.Name.Contains('images')) {
            $script:hvConfig | Add-Member -MemberType NoteProperty -Name 'images' -Value @()
        }

        # Ensure images is an array
        if ($script:hvConfig.images -eq $null) {
            $script:hvConfig.images = @()
        }

        $newImage = [pscustomobject]@{
            imageName = $ImageName
            imagePath = $IsoPath
        }
        if ($PSCmdlet.ShouldProcess($ImageName, "Add image configuration")) {
            $script:hvConfig.images += $newImage
            $script:hvConfig | ConvertTo-Json -Depth 20 | Out-File -FilePath $script:hvConfig.hvConfigPath -Encoding ascii -Force
            Write-Host $script:tick -ForegroundColor Green
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