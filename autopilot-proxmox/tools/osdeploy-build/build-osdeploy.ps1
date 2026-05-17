param(
    [string]$Arch = "amd64",
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$OSDeployVersion = "26.1.30.5",
    [string]$OSDBuilderVersion = "24.10.8.1",
    [string]$ADKVersion = "10.1.26100.1",
    [string]$SourceMediaPath = $env:OSDEPLOY_SOURCE_MEDIA,
    [string]$ImageName = "Windows Server 2025 Datacenter",
    [int]$ImageIndex = 4,
    [string]$OSVersion = "Windows Server 2025",
    [string]$OSEdition = "Datacenter",
    [string]$OSLanguage = "en-us",
    [string]$ControllerUrl = $env:AUTOPILOT_BASE_URL,
    [string]$FallbackControllerUrl = $env:AUTOPILOT_FALLBACK_BASE_URL,
    [string]$OSDBuilderPath = $(if ($env:OSDBUILDER_HOME) { $env:OSDBUILDER_HOME } else { "C:\OSDBuilder" }),
    [switch]$NativeMediaBuild,
    [switch]$SkipUpdates
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-FileHashHex {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Resolve-OSDeployControllerUrl {
    param([AllowNull()][string]$Value)
    $resolved = ([string]$Value).Trim().TrimEnd('/')
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        throw "OSDeploy ControllerUrl is required so generated PE media calls back to this controller."
    }
    return $resolved
}

function Write-OSDeployConfigForBuild {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath,
        [Parameter(Mandatory = $true)][string]$BuildSha,
        [Parameter(Mandatory = $true)][string]$ControllerUrl,
        [AllowNull()][string]$FallbackControllerUrl
    )
    $config = Get-Content -LiteralPath $SourcePath -Raw | ConvertFrom-Json
    $config.build_sha = $BuildSha
    $config.osdeploy_module_version = $OSDeployVersion
    $config.osdbuilder_module_version = $OSDBuilderVersion
    $config.flask_base_url = Resolve-OSDeployControllerUrl -Value $ControllerUrl
    if ([string]::IsNullOrWhiteSpace($FallbackControllerUrl)) {
        $config.fallback_base_url = ""
    } else {
        $config.fallback_base_url = ([string]$FallbackControllerUrl).Trim().TrimEnd('/')
    }
    $config |
        ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath $DestinationPath -Encoding UTF8
}

function Import-RequiredModule {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )
    $module = Get-Module -ListAvailable -Name $Name |
        ForEach-Object {
            if ($_.PSObject.Properties.Name -contains "VersionedModules") {
                $_.VersionedModules
            }
            else {
                $_
            }
        } |
        Where-Object { $_.Version.ToString() -eq $ExpectedVersion } |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if (-not $module) {
        throw "Required module '$Name' version '$ExpectedVersion' is not installed on this OSDeploy build host."
    }
    $moduleSpec = @{ ModuleName = $Name; RequiredVersion = $ExpectedVersion }
    Import-Module -FullyQualifiedName $moduleSpec -Force
    return @{
        Name = $module.Name
        ExpectedVersion = $ExpectedVersion
        ActualVersion = $module.Version.ToString()
        ModuleBase = $module.ModuleBase
    }
}

function Get-ServerEditionId {
    param([Parameter(Mandatory = $true)][string]$Edition)
    switch -Regex ($Edition) {
        "Datacenter" { return "ServerDatacenter" }
        "Standard" { return "ServerStandard" }
        default { return $Edition }
    }
}

function Get-LatestFile {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Filter
    )
    return Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $Filter -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
}

function Resolve-OSDeployOutputImageIndex {
    param(
        [Parameter(Mandatory = $true)][string]$WimPath,
        [Parameter(Mandatory = $true)][string]$RequestedImageName,
        [Parameter(Mandatory = $true)][int]$RequestedImageIndex
    )
    try {
        $images = @(Get-WindowsImage -ImagePath $WimPath)
        $namedImage = $images |
            Where-Object { $_.ImageName -eq $RequestedImageName } |
            Select-Object -First 1
        if ($namedImage) { return [int]$namedImage.ImageIndex }
        if ($images.Count -eq 1) { return [int]$images[0].ImageIndex }
    } catch {
        Write-Warning "Could not inspect output WIM image indexes: $($_.Exception.Message)"
    }
    return $RequestedImageIndex
}

function Resolve-CopypePath {
    param(
        [Parameter(Mandatory = $true)][string]$WinPeRoot,
        [Parameter(Mandatory = $true)][string]$Arch
    )
    foreach ($candidate in @(
        (Join-Path $WinPeRoot 'copype.cmd'),
        (Join-Path $WinPeRoot "$Arch\copype.cmd")
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw "ADK copype.cmd not installed under $WinPeRoot"
}

function Resolve-VirtioInf {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$InfName,
        [Parameter(Mandatory = $true)][string]$Arch
    )
    $candidates = Get-ChildItem -Path $Root -Recurse -Filter $InfName -ErrorAction SilentlyContinue |
        Where-Object FullName -match "\\$Arch\\"
    $preferred = $candidates |
        Where-Object FullName -match "\\w11\\$Arch\\|\\$Arch\\w11\\" |
        Sort-Object FullName |
        Select-Object -First 1
    if ($preferred) { return $preferred }
    return $candidates | Sort-Object FullName | Select-Object -First 1
}

function Add-OSDeployWinPEDrivers {
    param(
        [Parameter(Mandatory = $true)][string]$MountRoot,
        [Parameter(Mandatory = $true)][string]$Arch
    )
    $virtioInfNames = @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')
    $virtioCandidates = @(
        $env:AUTOPILOT_VIRTIO_ROOT,
        'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio-win',
        'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio',
        'C:\BuildRoot\inputs\virtio-win',
        'C:\BuildRoot\inputs\virtio',
        'E:\BuildRoot\inputs\virtio-win',
        'E:\BuildRoot\inputs\virtio',
        'E:\',
        'D:\virtio',
        'D:\',
        'F:\BuildRoot\inputs\virtio-win',
        'F:\BuildRoot\inputs\virtio',
        'F:\'
    ) | Where-Object { $_ }
    $virtioRoot = $null
    foreach ($candidate in $virtioCandidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $missingInf = $false
        foreach ($infName in $virtioInfNames) {
            if (-not (Resolve-VirtioInf -Root $candidate -InfName $infName -Arch $Arch)) {
                $missingInf = $true
                break
            }
        }
        if (-not $missingInf) { $virtioRoot = $candidate; break }
    }
    if (-not $virtioRoot) {
        throw "OSDeploy WinPE boot image needs VirtIO drivers. Checked: $($virtioCandidates -join ', ')"
    }
    foreach ($infName in $virtioInfNames) {
        $inf = Resolve-VirtioInf -Root $virtioRoot -InfName $infName -Arch $Arch
        Write-Host "Adding VirtIO driver $infName from $($inf.FullName)"
        & dism.exe /Image:$MountRoot /Add-Driver /Driver:$($inf.FullName) /ForceUnsigned | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Add-Driver $infName failed: $LASTEXITCODE" }
    }
}

function Copy-RequiredFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required build output was not found: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Invoke-Cmd {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$CommandLine
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & cmd.exe /d /c $CommandLine 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($output) { $output | ForEach-Object { Write-Host "$_" } }
    if ($exitCode -ne 0) { throw "$Label failed: $exitCode" }
}

function Test-DismPackageNotApplicable {
    param([int]$ExitCode)
    return $ExitCode -eq -2146498530 -or $ExitCode -eq 0x800f081e
}

function Reset-OSDeployBuilderState {
    param([Parameter(Mandatory = $true)][string]$Root)
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return
    }
    & dism.exe /Cleanup-Wim | Out-Null
    foreach ($mount in Get-ChildItem -LiteralPath (Join-Path $Root 'Mount') -Directory -ErrorAction SilentlyContinue) {
        & dism.exe /Unmount-Image /MountDir:$($mount.FullName) /Discard | Out-Null
    }
    foreach ($relativeRoot in @('OSMedia', 'OSImport', 'OSBuilds')) {
        $path = Join-Path $Root $relativeRoot
        if (-not (Test-Path -LiteralPath $path -PathType Container)) { continue }
        Get-ChildItem -LiteralPath $path -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'Windows Server *' -or $_.Name -like 'build*' } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Copy-IsoTree {
    param(
        [Parameter(Mandatory = $true)][string]$IsoPath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $image = Mount-DiskImage -ImagePath $IsoPath -PassThru
    try {
        $volume = $image | Get-Volume
        $root = "$($volume.DriveLetter):\"
        Copy-Item -Path (Join-Path $root '*') -Destination $Destination -Recurse -Force
        Get-ChildItem -LiteralPath $Destination -Recurse -Force | ForEach-Object {
            $_.Attributes = $_.Attributes -band (-bnot [System.IO.FileAttributes]::ReadOnly)
        }
    } finally {
        Dismount-DiskImage -ImagePath $IsoPath -ErrorAction SilentlyContinue | Out-Null
    }
}

function Copy-SourceTree {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Copy-Item -Path (Join-Path $SourceRoot '*') -Destination $Destination -Recurse -Force
    Get-ChildItem -LiteralPath $Destination -Recurse -Force | ForEach-Object {
        $_.Attributes = $_.Attributes -band (-bnot [System.IO.FileAttributes]::ReadOnly)
    }
}

function Find-OSDeployInstallImage {
    param([Parameter(Mandatory = $true)][string]$SourceRoot)
    $installWim = Join-Path $SourceRoot 'sources\install.wim'
    if (Test-Path -LiteralPath $installWim -PathType Leaf) {
        return $installWim
    }
    $installEsd = Join-Path $SourceRoot 'sources\install.esd'
    if (Test-Path -LiteralPath $installEsd -PathType Leaf) {
        return $installEsd
    }
    throw "Source media root did not contain sources\install.wim or sources\install.esd: $SourceRoot"
}

function Resolve-OSDeploySourceMedia {
    param([string]$SourceMediaPath)
    if ([string]::IsNullOrWhiteSpace($SourceMediaPath)) {
        return $null
    }

    $mountedImage = $null
    if (Test-Path -LiteralPath $SourceMediaPath -PathType Container) {
        $sourceRoot = (Resolve-Path -LiteralPath $SourceMediaPath).Path
        return [pscustomobject]@{
            SourceType = 'mounted'
            SourceRoot = $sourceRoot
            InstallImagePath = Find-OSDeployInstallImage -SourceRoot $sourceRoot
            MountedImage = $null
        }
    }
    if (-not (Test-Path -LiteralPath $SourceMediaPath -PathType Leaf)) {
        throw "Source media path was not found: $SourceMediaPath"
    }
    if ([System.IO.Path]::GetExtension($SourceMediaPath) -ine ".iso") {
        throw "Source media file must be an ISO: $SourceMediaPath"
    }

    $mountedImage = Mount-DiskImage -ImagePath $SourceMediaPath -PassThru
    try {
        $volume = $mountedImage | Get-Volume | Where-Object { $_.DriveLetter } | Select-Object -First 1
        if (-not $volume) {
            throw "Mounted source media did not expose a drive letter: $SourceMediaPath"
        }
        $sourceRoot = "$($volume.DriveLetter):\"
        return [pscustomobject]@{
            SourceType = 'iso'
            SourceRoot = $sourceRoot
            InstallImagePath = Find-OSDeployInstallImage -SourceRoot $sourceRoot
            MountedImage = $mountedImage
        }
    } catch {
        if ($mountedImage) {
            Dismount-DiskImage -ImagePath $SourceMediaPath -ErrorAction SilentlyContinue | Out-Null
        }
        throw
    }
}

function Inject-OSDeployWinPEBridge {
    param(
        [Parameter(Mandatory = $true)][string]$IsoPath,
        [Parameter(Mandatory = $true)][string]$OutputIsoPath,
        [Parameter(Mandatory = $true)][string]$BridgeRoot,
        [Parameter(Mandatory = $true)][string]$OscdimgPath,
        [Parameter(Mandatory = $true)][string]$Arch,
        [string]$SourceRoot = "",
        [string]$InstallWimPath = ""
    )
    $stagingRoot = Join-Path $OutputDir "$prefix-iso-staging"
    $mountRoot = Join-Path $OutputDir "$prefix-bootwim-mount"
    $adkRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $OscdimgPath)))
    $peRoot = Join-Path $adkRoot "Windows Preinstallation Environment"
    $dandISetEnv = Join-Path $adkRoot "Deployment Tools\DandISetEnv.bat"
    $copyPe = Resolve-CopypePath -WinPeRoot $peRoot -Arch $Arch
    $adkWorkRoot = Join-Path $OutputDir "$prefix-adk-winpe"
    Remove-Item -LiteralPath $adkWorkRoot -Recurse -Force -ErrorAction SilentlyContinue
    Invoke-Cmd `
        -Label 'copype' `
        -CommandLine "call `"$dandISetEnv`" && call `"$copyPe`" $Arch `"$adkWorkRoot`""
    $adkBootWim = Join-Path $adkWorkRoot 'media\sources\boot.wim'
    if (-not (Test-Path -LiteralPath $adkBootWim -PathType Leaf)) {
        throw "ADK copype did not create boot.wim: $adkBootWim"
    }

    if (-not [string]::IsNullOrWhiteSpace($SourceRoot)) {
        Copy-SourceTree -SourceRoot $SourceRoot -Destination $stagingRoot
    } else {
        Copy-IsoTree -IsoPath $IsoPath -Destination $stagingRoot
    }
    if (-not [string]::IsNullOrWhiteSpace($InstallWimPath)) {
        Copy-RequiredFile -Source $InstallWimPath -Destination (Join-Path $stagingRoot 'sources\install.wim')
    }
    $bootWim = Join-Path $stagingRoot 'sources\boot.wim'
    if (-not (Test-Path -LiteralPath $bootWim -PathType Leaf)) {
        throw "OSDeploy build ISO did not contain sources\boot.wim: $IsoPath"
    }
    Copy-Item -LiteralPath $adkBootWim -Destination $bootWim -Force
    (Get-Item -LiteralPath $bootWim).Attributes =
        (Get-Item -LiteralPath $bootWim).Attributes -band (-bnot [System.IO.FileAttributes]::ReadOnly)

    $wimInfo = & dism.exe /English /Get-WimInfo /WimFile:$bootWim
    $indexes = @()
    foreach ($line in $wimInfo) {
        if ($line -match 'Index\s*:\s*(\d+)') { $indexes += [int]$Matches[1] }
    }
    if (-not $indexes) { $indexes = @(1) }

    foreach ($index in $indexes) {
        Remove-Item -LiteralPath $mountRoot -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $mountRoot -Force | Out-Null
        & dism.exe /Mount-Image /ImageFile:$bootWim /Index:$index /MountDir:$mountRoot | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "dism /Mount-Image failed for boot.wim index ${index}: $LASTEXITCODE" }
        try {
            $pkgRoot = Join-Path $adkRoot "Windows Preinstallation Environment\$Arch\WinPE_OCs"
            foreach ($pkg in @('WinPE-WMI', 'WinPE-NetFx', 'WinPE-Scripting', 'WinPE-PowerShell', 'WinPE-StorageWMI', 'WinPE-DismCmdlets')) {
                $cab = Join-Path $pkgRoot "$pkg.cab"
                if (Test-Path -LiteralPath $cab) {
                    & dism.exe /Image:$mountRoot /Add-Package /PackagePath:$cab | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        if (Test-DismPackageNotApplicable -ExitCode $LASTEXITCODE) {
                            Write-Warning "Add-Package $pkg was not applicable for boot.wim index ${index}; continuing."
                        } else {
                            throw "Add-Package $pkg failed for boot.wim index ${index}: $LASTEXITCODE"
                        }
                    }
                }
                $langCab = Join-Path $pkgRoot "en-us\${pkg}_en-us.cab"
                if (Test-Path -LiteralPath $langCab) {
                    & dism.exe /Image:$mountRoot /Add-Package /PackagePath:$langCab | Out-Null
                    if ($LASTEXITCODE -ne 0) {
                        if (Test-DismPackageNotApplicable -ExitCode $LASTEXITCODE) {
                            Write-Warning "Add-Package ${pkg}_en-us was not applicable for boot.wim index ${index}; continuing."
                        } else {
                            throw "Add-Package ${pkg}_en-us failed for boot.wim index ${index}: $LASTEXITCODE"
                        }
                    }
                }
            }
            $powershellPath = Join-Path $mountRoot 'Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
            if (-not (Test-Path -LiteralPath $powershellPath -PathType Leaf)) {
                throw "OSDeploy WinPE bridge requires PowerShell in boot.wim: $powershellPath"
            }
            Add-OSDeployWinPEDrivers -MountRoot $mountRoot -Arch $Arch
            $autopilotDir = Join-Path $mountRoot 'autopilot'
            New-Item -ItemType Directory -Path $autopilotDir -Force | Out-Null
            Copy-Item (Join-Path $BridgeRoot 'autopilot\Invoke-OSDeployBridge.ps1') -Destination $autopilotDir -Force
            Copy-Item (Join-Path $BridgeRoot 'autopilot\config.json') -Destination $autopilotDir -Force
            Copy-Item (Join-Path $BridgeRoot 'startnet.cmd') -Destination (Join-Path $mountRoot 'Windows\System32\startnet.cmd') -Force
            @"
[LaunchApps]
%SYSTEMROOT%\System32\cmd.exe, /c X:\Windows\System32\startnet.cmd
"@ | Set-Content -LiteralPath (Join-Path $mountRoot 'Windows\System32\winpeshl.ini') -Encoding ASCII
        } finally {
            & dism.exe /Unmount-Image /MountDir:$mountRoot /Commit | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "dism /Unmount-Image failed for boot.wim index ${index}: $LASTEXITCODE" }
        }
    }

    $oscdimgRoot = Split-Path -Parent $OscdimgPath
    $etfsBoot = Join-Path $oscdimgRoot 'etfsboot.com'
    $efiNoPrompt = Join-Path $oscdimgRoot 'efisys_noprompt.bin'
    if (Test-Path -LiteralPath $etfsBoot) {
        $bootData = "-bootdata:2#p0,e,b`"$etfsBoot`"#pEF,e,b`"$efiNoPrompt`""
    } else {
        $bootData = "-bootdata:1#pEF,e,b`"$efiNoPrompt`""
    }
    Invoke-Cmd `
        -Label 'oscdimg' `
        -CommandLine "`"$OscdimgPath`" -m -o -u2 -udfver102 $bootData `"$stagingRoot`" `"$OutputIsoPath`""
}

function Invoke-NativeServerMediaBuild {
    param(
        [Parameter(Mandatory = $true)]$SourceMedia,
        [Parameter(Mandatory = $true)][string]$SourceMediaPath,
        [Parameter(Mandatory = $true)][string]$WimPath,
        [Parameter(Mandatory = $true)][string]$IsoPath,
        [Parameter(Mandatory = $true)][string]$ImageName,
        [Parameter(Mandatory = $true)][int]$ImageIndex,
        [Parameter(Mandatory = $true)][string]$BridgeRoot,
        [Parameter(Mandatory = $true)][string]$OscdimgPath,
        [Parameter(Mandatory = $true)][string]$Arch
    )
    if (-not $SourceMedia) {
        throw "Native Server media build requires SourceMediaPath."
    }
    Write-Host "Native Server media build: exporting image index $ImageIndex from $($SourceMedia.InstallImagePath)"
    & dism.exe /Export-Image /SourceImageFile:$($SourceMedia.InstallImagePath) /SourceIndex:$ImageIndex /DestinationImageFile:$WimPath /DestinationName:$ImageName /Compress:max /CheckIntegrity | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "dism /Export-Image failed for Server image index ${ImageIndex}: $LASTEXITCODE"
    }
    Inject-OSDeployWinPEBridge `
        -IsoPath $SourceMediaPath `
        -SourceRoot $SourceMedia.SourceRoot `
        -InstallWimPath $WimPath `
        -OutputIsoPath $IsoPath `
        -BridgeRoot $BridgeRoot `
        -OscdimgPath $OscdimgPath `
        -Arch $Arch
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$osdModule = Import-RequiredModule -Name "OSD" -ExpectedVersion $OSDeployVersion
$osdBuilderModule = Import-RequiredModule -Name "OSDBuilder" -ExpectedVersion $OSDBuilderVersion

$oscdimg = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools" `
    -Recurse -Filter oscdimg.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $oscdimg) {
    throw "Windows ADK oscdimg.exe was not found. Install Windows ADK $ADKVersion on the build host."
}

$buildSha = (Get-Date -Format "yyyyMMddHHmmss")
$prefix = "osdeploy-server-$Arch-$buildSha"
$wimPath = Join-Path $OutputDir "$prefix.wim"
$isoPath = Join-Path $OutputDir "$prefix.iso"
$manifestPath = Join-Path $OutputDir "$prefix.json"
$peBridgeRoot = Join-Path $OutputDir "$prefix-pe-bridge"
$peAutopilotRoot = Join-Path $peBridgeRoot "autopilot"
$buildEngine = "osdbuilder"
Remove-Item -LiteralPath $peBridgeRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $peAutopilotRoot -Force | Out-Null
Copy-Item "$PSScriptRoot\Invoke-OSDeployBridge.ps1" -Destination $peAutopilotRoot -Force
Write-OSDeployConfigForBuild `
    -SourcePath "$PSScriptRoot\config.json" `
    -DestinationPath (Join-Path $peAutopilotRoot 'config.json') `
    -BuildSha $buildSha `
    -ControllerUrl $ControllerUrl `
    -FallbackControllerUrl $FallbackControllerUrl
Copy-Item "$PSScriptRoot\startnet.cmd" -Destination (Join-Path $peBridgeRoot 'startnet.cmd') -Force

$sourceMedia = $null
$customBuildCommand = $env:OSDEPLOY_SERVER_BUILD_COMMAND
if (-not [string]::IsNullOrWhiteSpace($customBuildCommand)) {
    $env:OSDEPLOY_OUTPUT_WIM = $wimPath
    $env:OSDEPLOY_OUTPUT_ISO = $isoPath
    $env:OSDEPLOY_SOURCE_MEDIA = $SourceMediaPath
    $env:OSDEPLOY_IMAGE_INDEX = [string]$ImageIndex
    $env:OSDEPLOY_IMAGE_NAME = $ImageName
    $env:OSDEPLOY_OS_VERSION = $OSVersion
    $env:OSDEPLOY_OS_EDITION = $OSEdition
    $env:OSDEPLOY_OS_LANGUAGE = $OSLanguage
    Invoke-Expression $customBuildCommand
    if (Test-Path -LiteralPath $isoPath -PathType Leaf) {
        $customIsoPath = Join-Path $OutputDir ([System.IO.Path]::GetFileNameWithoutExtension($isoPath) + ".custom.iso")
        Move-Item -LiteralPath $isoPath -Destination $customIsoPath -Force
        Inject-OSDeployWinPEBridge `
            -IsoPath $customIsoPath `
            -OutputIsoPath $isoPath `
            -BridgeRoot $peBridgeRoot `
            -OscdimgPath $oscdimg.FullName `
            -Arch $Arch
    }
} else {
    New-Item -ItemType Directory -Path $OSDBuilderPath -Force | Out-Null
    try {
        Reset-OSDeployBuilderState -Root $OSDBuilderPath
        $sourceMedia = Resolve-OSDeploySourceMedia -SourceMediaPath $SourceMediaPath

        if ($NativeMediaBuild -or $OSVersion -like "Windows Server*") {
            $buildEngine = if ($OSVersion -like "Windows Server*") { "native-server-media" } else { "native-media" }
            Invoke-NativeServerMediaBuild `
                -SourceMedia $sourceMedia `
                -SourceMediaPath $SourceMediaPath `
                -WimPath $wimPath `
                -IsoPath $isoPath `
                -ImageName $ImageName `
                -ImageIndex $ImageIndex `
                -BridgeRoot $peBridgeRoot `
                -OscdimgPath $oscdimg.FullName `
                -Arch $Arch
        } else {
            $editionId = Get-ServerEditionId -Edition $OSEdition
            $importParams = @{
                EditionId = $editionId
                SkipGrid = $true
                ShowInfo = $true
            }
            if ($sourceMedia) {
                $importParams["Path"] = $sourceMedia.SourceRoot
            }
            if ($ImageIndex -gt 0) {
                $importParams["ImageIndex"] = $ImageIndex
            }
            Import-OSMedia @importParams

            $osMedia = @("OSMedia", "OSImport") |
                ForEach-Object { Join-Path $OSDBuilderPath $_ } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
                ForEach-Object { Get-ChildItem -LiteralPath $_ -Directory -ErrorAction SilentlyContinue } |
                Sort-Object LastWriteTimeUtc -Descending |
                Select-Object -First 1
            if (-not $osMedia) {
                throw "Import-OSMedia did not create an OS media entry under $OSDBuilderPath."
            }

            $updateParams = @{
                Name = $osMedia.Name
                Execute = $true
                HideCleanupProgress = $true
            }
            if ($SkipUpdates) {
                $updateParams["SkipUpdates"] = $true
            } else {
                $updateParams["Download"] = $true
            }
            Update-OSMedia @updateParams

            $buildParams = @{
                Name = $osMedia.Name
                Execute = $true
                CreateISO = $true
                SkipTask = $true
                EnableNetFX = $true
                HideCleanupProgress = $true
            }
            if ($SkipUpdates) {
                $buildParams["SkipUpdates"] = $true
                $buildParams["SkipUpdatesPE"] = $true
            } else {
                $buildParams["Download"] = $true
            }
            New-OSBuild @buildParams

            $latestWim = Get-LatestFile -Root $OSDBuilderPath -Filter "install.wim"
            $latestIso = Get-LatestFile -Root $OSDBuilderPath -Filter "*.iso"
            if (-not $latestWim) {
                throw "New-OSBuild did not produce an install.wim under $OSDBuilderPath."
            }
            if (-not $latestIso) {
                throw "New-OSBuild -CreateISO did not produce an ISO under $OSDBuilderPath."
            }
            Copy-RequiredFile -Source $latestWim.FullName -Destination $wimPath
            Inject-OSDeployWinPEBridge `
                -IsoPath $latestIso.FullName `
                -OutputIsoPath $isoPath `
                -BridgeRoot $peBridgeRoot `
                -OscdimgPath $oscdimg.FullName `
                -Arch $Arch
        }
    } finally {
        if ($sourceMedia -and $sourceMedia.MountedImage) {
            Dismount-DiskImage -ImagePath $SourceMediaPath -ErrorAction SilentlyContinue | Out-Null
        }
    }
}
if (-not (Test-Path -LiteralPath $wimPath -PathType Leaf)) {
    throw "OSDeploy build command did not create WIM output: $wimPath"
}
if (-not (Test-Path -LiteralPath $isoPath -PathType Leaf)) {
    throw "OSDeploy build command did not create ISO output: $isoPath"
}

$sourceMediaType = ""
$sourceInstallImage = ""
if ($sourceMedia) {
    $sourceMediaType = $sourceMedia.SourceType
    $sourceInstallImage = $sourceMedia.InstallImagePath
}
$outputImageIndex = Resolve-OSDeployOutputImageIndex `
    -WimPath $wimPath `
    -RequestedImageName $ImageName `
    -RequestedImageIndex $ImageIndex
if ($buildEngine -eq "native-server-media") {
    $effectiveImageIndex = $ImageIndex
} else {
    $effectiveImageIndex = $outputImageIndex
}

$manifest = [ordered]@{
    schema_version = 1
    architecture = $Arch
    osdeploy_module_version = $OSDeployVersion
    osdbuilder_module_version = $OSDBuilderVersion
    adk_version = $ADKVersion
    detected_modules = @($osdModule, $osdBuilderModule)
    adk_oscdimg = $oscdimg.FullName
    build_sha = $buildSha
    build_engine = $buildEngine
    output_wim = $wimPath
    output_iso = $isoPath
    wim_sha256 = Get-FileHashHex -Path $wimPath
    iso_sha256 = Get-FileHashHex -Path $isoPath
    source_media = $SourceMediaPath
    source_media_type = $sourceMediaType
    source_install_image = $sourceInstallImage
    pe_bridge_root = $peBridgeRoot
    pe_bridge_script = (Join-Path $peAutopilotRoot 'Invoke-OSDeployBridge.ps1')
    pe_startnet = (Join-Path $peBridgeRoot 'startnet.cmd')
    image_name = $ImageName
    image_index = $effectiveImageIndex
    output_image_index = $outputImageIndex
    os_version = $OSVersion
    os_edition = $OSEdition
    os_language = $OSLanguage
    controller_url = (Resolve-OSDeployControllerUrl -Value $ControllerUrl)
    built_by_host = $env:COMPUTERNAME
    built_at = (Get-Date).ToUniversalTime().ToString("o")
}

$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host $manifestPath
