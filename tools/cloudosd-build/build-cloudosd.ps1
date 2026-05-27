<#
.SYNOPSIS
    Build the static CloudOSD PE ISO for ProxmoxVEAutopilot.

.DESCRIPTION
    Wraps Microsoft ADK + DISM. Produces cloudosd-autopilot-amd64-<sha>.iso
    plus a sibling .wim and a manifest .json. Run-specific identity, tokens,
    and workflow JSON are deliberately not baked into the ISO.

.PARAMETER Arch
    amd64. CloudOSD arm64 can be added later after the first Proxmox path works.

.PARAMETER OutputDir
    Where to drop the artifacts. Default: F:\BuildRoot\outputs.

.PARAMETER OSDCloudVersion
    Pinned OSDCloud module version copied into the mounted WIM.

.PARAMETER CurlCacheDir
    Optional directory for an architecture-matched curl.exe payload. When the
    build host curl.exe does not match the target PE architecture, the builder
    downloads and verifies the official curl-for-Windows package into this
    cache.

.PARAMETER DryRun
    Resolve outputs and print planned paths without invoking ADK.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('amd64')]
    [string] $Arch,

    [string] $OutputDir = 'F:\BuildRoot\outputs',

    [string] $OSDCloudVersion = '26.4.17.1',

    [string] $CurlCacheDir = '',

    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-CloudOSDComponentHashes {
    $files = @(
        'build-cloudosd.ps1',
        'Invoke-CloudOSDBridge.ps1',
        'PVEAutopilot-FirstBoot.ps1',
        'config.json',
        'startnet.cmd'
    )
    $hashes = [ordered]@{}
    foreach ($file in $files) {
        $path = Join-Path $PSScriptRoot $file
        if (Test-Path -LiteralPath $path) {
            $hashes[$file] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        } else {
            $hashes[$file] = 'missing'
        }
    }
    return $hashes
}

function Get-BuildSha {
    param(
        [Parameter(Mandatory)] [string] $Arch,
        [Parameter(Mandatory)] [string] $OSDCloudVersion,
        [Parameter(Mandatory)] [System.Collections.IDictionary] $ComponentHashes
    )
    $inputs = @($PSScriptRoot, $Arch, $OSDCloudVersion)
    foreach ($file in @($ComponentHashes.Keys | Sort-Object)) {
        $inputs += "${file}:$($componentHashes[$file])"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($inputs -join '|'))
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return ($hash[0..7] | ForEach-Object { $_.ToString('x2') }) -join ''
}

function Resolve-CopypePath {
    param(
        [Parameter(Mandatory)] [string] $WinPeRoot,
        [Parameter(Mandatory)] [string] $Arch
    )
    $candidates = @(
        (Join-Path $WinPeRoot 'copype.cmd'),
        (Join-Path $WinPeRoot "$Arch\copype.cmd")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw "ADK + WinPE add-on not installed (looked for $($candidates -join ', '))"
}

function Invoke-Cmd {
    param(
        [Parameter(Mandatory)] [string] $Label,
        [Parameter(Mandatory)] [string] $CommandLine
    )
    $oldEap = $ErrorActionPreference
    $oldNative = $null
    $hasNative = Test-Path variable:PSNativeCommandUseErrorActionPreference
    if ($hasNative) {
        $oldNative = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = 'Continue'
        $output = & cmd.exe /d /c $CommandLine 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldEap
        if ($hasNative) { $PSNativeCommandUseErrorActionPreference = $oldNative }
    }
    if ($output) { $output | Write-Host }
    if ($exitCode -ne 0) { throw "$Label failed: $exitCode" }
}

function Resolve-VirtioInf {
    param(
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [string] $InfName,
        [Parameter(Mandatory)] [string] $Arch
    )
    $candidates = Get-ChildItem -Path $Root -Recurse -Filter $InfName |
        Where-Object FullName -match "\\$Arch\\"
    $preferred = $candidates |
        Where-Object FullName -match "\\w11\\$Arch\\|\\$Arch\\w11\\" |
        Sort-Object FullName |
        Select-Object -First 1
    if ($preferred) { return $preferred }
    return $candidates | Sort-Object FullName | Select-Object -First 1
}

function Write-CloudOSDConfigForBuild {
    param(
        [Parameter(Mandatory)] [string] $SourcePath,
        [Parameter(Mandatory)] [string] $DestinationPath,
        [Parameter(Mandatory)] [string] $BuildSha
    )
    $config = Get-Content -LiteralPath $SourcePath -Raw | ConvertFrom-Json
    $config.build_sha = $BuildSha
    $config |
        ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath $DestinationPath -Encoding UTF8
}

function Get-CloudOSDExecutableMachine {
    param([Parameter(Mandatory)] [string] $Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = [System.IO.BinaryReader]::new($stream)
        $stream.Seek(0x3c, [System.IO.SeekOrigin]::Begin) | Out-Null
        $peOffset = $reader.ReadInt32()
        $stream.Seek($peOffset + 4, [System.IO.SeekOrigin]::Begin) | Out-Null
        return $reader.ReadUInt16()
    } finally {
        $stream.Dispose()
    }
}

function Test-CloudOSDExecutableArchitecture {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Arch
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $machine = Get-CloudOSDExecutableMachine -Path $Path
    if ($Arch -eq 'amd64') { return $machine -eq 0x8664 }
    if ($Arch -eq 'arm64') { return $machine -eq 0xaa64 }
    return $false
}

function Get-CloudOSDCurlPackageInfo {
    param([Parameter(Mandatory)] [string] $Arch)

    if ($Arch -eq 'amd64') {
        return [pscustomobject]@{
            Url = 'https://curl.se/windows/dl-8.20.0_2/curl-8.20.0_2-win64-mingw.zip'
            Sha256 = '57b07ba8f3634ffb7773d7fe1321f720316d11acc2ed5654fce97589c2e8a7d1'
            Machine = 0x8664
        }
    }
    throw "No curl package is pinned for architecture $Arch"
}

function Resolve-CurlCacheDir {
    param([string] $Requested)

    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        return $Requested
    }
    if (Test-Path -LiteralPath 'F:\BuildRoot') {
        return 'F:\BuildRoot\inputs\curl'
    }
    return (Join-Path $env:TEMP 'cloudosd-curl')
}

function Resolve-CurlPath {
    param(
        [Parameter(Mandatory)] [string] $Arch,
        [string] $CurlCacheDir
    )

    $candidates = @()
    $archEnvName = "CLOUDOSD_CURL_$($Arch.ToUpperInvariant())_PATH"
    if ([Environment]::GetEnvironmentVariable($archEnvName)) {
        $candidates += [Environment]::GetEnvironmentVariable($archEnvName)
    }
    if ($env:CLOUDOSD_CURL_PATH) {
        $candidates += $env:CLOUDOSD_CURL_PATH
    }
    $cacheDir = Resolve-CurlCacheDir -Requested $CurlCacheDir
    $candidates += (Join-Path $cacheDir "$Arch\curl.exe")
    if ($env:SystemRoot) {
        $candidates += (Join-Path $env:SystemRoot 'System32\curl.exe')
    }
    $command = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (Test-CloudOSDExecutableArchitecture -Path $candidate -Arch $Arch) {
            return [pscustomobject]@{
                Path = $candidate
                SourceUrl = ''
                PackageSha256 = ''
                Machine = (Get-CloudOSDExecutableMachine -Path $candidate)
            }
        }
    }

    $package = Get-CloudOSDCurlPackageInfo -Arch $Arch
    $archCache = Join-Path $cacheDir $Arch
    New-Item -ItemType Directory -Path $archCache -Force | Out-Null
    $zipPath = Join-Path $archCache ([System.IO.Path]::GetFileName($package.Url))
    if (-not (Test-Path -LiteralPath $zipPath) -or
        ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $package.Sha256)) {
        Invoke-WebRequest -Uri $package.Url -OutFile $zipPath
    }
    $actualSha = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha -ne $package.Sha256) {
        throw "Downloaded curl package hash mismatch. Expected $($package.Sha256), got $actualSha"
    }

    $extractPath = Join-Path $archCache 'expanded'
    Remove-Item -LiteralPath $extractPath -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
    $downloadedCurl = Get-ChildItem -LiteralPath $extractPath -Filter curl.exe -Recurse |
        Where-Object { Test-CloudOSDExecutableArchitecture -Path $_.FullName -Arch $Arch } |
        Select-Object -First 1
    if (-not $downloadedCurl) {
        throw "Downloaded curl package did not contain an executable for $Arch"
    }
    $cacheExe = Join-Path $archCache 'curl.exe'
    Copy-Item -LiteralPath $downloadedCurl.FullName -Destination $cacheExe -Force
    return [pscustomobject]@{
        Path = $cacheExe
        SourceUrl = $package.Url
        PackageSha256 = $package.Sha256
        Machine = (Get-CloudOSDExecutableMachine -Path $cacheExe)
    }
}

$componentHashes = Get-CloudOSDComponentHashes
$sha = Get-BuildSha -Arch $Arch -OSDCloudVersion $OSDCloudVersion -ComponentHashes $componentHashes
$base = "cloudosd-autopilot-$Arch-$sha"
$wimPath = [System.IO.Path]::Combine($OutputDir, "$base.wim")
$isoPath = [System.IO.Path]::Combine($OutputDir, "$base.iso")
$manifestPath = [System.IO.Path]::Combine($OutputDir, "$base.json")

Write-Output $manifestPath
Write-Output $wimPath
Write-Output $isoPath

if ($DryRun) { return }

$adkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit"
$dandISetEnv = "$adkRoot\Deployment Tools\DandISetEnv.bat"
$oscdimgRoot = "$adkRoot\Deployment Tools\$Arch\Oscdimg"
$peRoot = "$adkRoot\Windows Preinstallation Environment"
$copyPe = Resolve-CopypePath -WinPeRoot $peRoot -Arch $Arch
$curlPayload = Resolve-CurlPath -Arch $Arch -CurlCacheDir $CurlCacheDir
$curlPath = $curlPayload.Path
if (-not (Test-Path -LiteralPath $dandISetEnv)) {
    throw "ADK Deployment Tools not installed (looked for $dandISetEnv)"
}
$oscdimg = "$oscdimgRoot\oscdimg.exe"
$efiNoPrompt = "$oscdimgRoot\efisys_noprompt.bin"
if (-not (Test-Path -LiteralPath $oscdimg)) {
    throw "ADK oscdimg.exe not installed (looked for $oscdimg)"
}
if (-not (Test-Path -LiteralPath $efiNoPrompt)) {
    throw "ADK UEFI no-prompt boot image not installed (looked for $efiNoPrompt)"
}

$workRoot = Join-Path $env:TEMP "cloudosd-build-$Arch-$(New-Guid)"
Invoke-Cmd `
    -Label 'copype' `
    -CommandLine "call `"$dandISetEnv`" && call `"$copyPe`" $Arch `"$workRoot`""

$mountDir = Join-Path $workRoot 'mount'
$bootWim = Join-Path $workRoot 'media\sources\boot.wim'

& dism.exe /Mount-Image /ImageFile:$bootWim /Index:1 /MountDir:$mountDir | Out-Null
if ($LASTEXITCODE -ne 0) { throw "dism /Mount-Image failed: $LASTEXITCODE" }

try {
    $optionalPackages = @(
        'WinPE-WMI', 'WinPE-NetFx', 'WinPE-Scripting', 'WinPE-PowerShell',
        'WinPE-StorageWMI', 'WinPE-DismCmdlets', 'WinPE-SecureStartup'
    )
    $pkgRoot = "$peRoot\$Arch\WinPE_OCs"
    foreach ($pkg in $optionalPackages) {
        & dism.exe /Image:$mountDir /Add-Package /PackagePath:"$pkgRoot\$pkg.cab" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Add-Package $pkg failed: $LASTEXITCODE" }
        $langCab = "$pkgRoot\en-us\${pkg}_en-us.cab"
        if (Test-Path -LiteralPath $langCab) {
            & dism.exe /Image:$mountDir /Add-Package /PackagePath:$langCab | Out-Null
        }
    }

    $virtioRoot = $null
    $virtioInfNames = @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')
    $virtioCandidates = @(
        $env:AUTOPILOT_VIRTIO_ROOT,
        'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio-win',
        'C:\BuildRoot\ProxmoxVEAutopilot\inputs\virtio',
        'C:\BuildRoot\inputs\virtio-win',
        'C:\BuildRoot\inputs\virtio',
        'D:\virtio',
        'D:\',
        'F:\BuildRoot\inputs\virtio-win',
        'F:\BuildRoot\inputs\virtio'
    ) | Where-Object { $_ }
    foreach ($candidate in $virtioCandidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $missingInf = $false
        foreach ($infName in $virtioInfNames) {
            $inf = Resolve-VirtioInf -Root $candidate -InfName $infName -Arch $Arch
            if (-not $inf) {
                $missingInf = $true
                break
            }
        }
        if (-not $missingInf) { $virtioRoot = $candidate; break }
    }
    if (-not $virtioRoot) {
        throw "CloudOSD build needs VirtIO drivers. Checked: $($virtioCandidates -join ', ')"
    }
    foreach ($infName in $virtioInfNames) {
        $inf = Resolve-VirtioInf -Root $virtioRoot -InfName $infName -Arch $Arch
        Write-Host "Adding VirtIO driver $infName from $($inf.FullName)"
        & dism.exe /Image:$mountDir /Add-Driver /Driver:$($inf.FullName) /ForceUnsigned | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Add-Driver $infName failed: $LASTEXITCODE" }
    }

    $moduleRoot = Join-Path $mountDir 'Program Files\WindowsPowerShell\Modules'
    New-Item -ItemType Directory -Path $moduleRoot -Force | Out-Null
    $osdCloudModuleRoot = Join-Path $moduleRoot 'OSDCloud'
    $osdCloudVersionRoot = Join-Path $osdCloudModuleRoot $OSDCloudVersion
    $osdCloudPackage = Join-Path $env:TEMP "OSDCloud-$OSDCloudVersion.zip"
    $osdCloudPackageUrl = "https://www.powershellgallery.com/api/v2/package/OSDCloud/$OSDCloudVersion"
    Remove-Item -LiteralPath $osdCloudModuleRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $osdCloudPackage -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $osdCloudVersionRoot -Force | Out-Null
    Invoke-Cmd `
        -Label 'download OSDCloud module' `
        -CommandLine "`"$curlPath`" --fail --location --retry 3 --connect-timeout 30 --max-time 600 -o `"$osdCloudPackage`" `"$osdCloudPackageUrl`""
    Expand-Archive -LiteralPath $osdCloudPackage -DestinationPath $osdCloudVersionRoot -Force

    Copy-Item $curlPath -Destination (Join-Path $mountDir 'Windows\System32\curl.exe') -Force

    $autopilotDir = Join-Path $mountDir 'autopilot'
    New-Item -ItemType Directory -Path $autopilotDir -Force | Out-Null
    Copy-Item "$PSScriptRoot\Invoke-CloudOSDBridge.ps1" -Destination $autopilotDir -Force
    Copy-Item "$PSScriptRoot\PVEAutopilot-FirstBoot.ps1" -Destination $autopilotDir -Force
    Write-CloudOSDConfigForBuild `
        -SourcePath "$PSScriptRoot\config.json" `
        -DestinationPath (Join-Path $autopilotDir 'config.json') `
        -BuildSha $sha
    Copy-Item "$PSScriptRoot\startnet.cmd" -Destination (Join-Path $mountDir 'Windows\System32\startnet.cmd') -Force
} finally {
    & dism.exe /Unmount-Image /MountDir:$mountDir /Commit | Out-Null
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}
Copy-Item $bootWim -Destination $wimPath -Force

$mediaRoot = Join-Path $workRoot 'media'
$etfsBoot = "$oscdimgRoot\etfsboot.com"
if (Test-Path -LiteralPath $etfsBoot) {
    $bootData = "-bootdata:2#p0,e,b`"$etfsBoot`"#pEF,e,b`"$efiNoPrompt`""
} else {
    $bootData = "-bootdata:1#pEF,e,b`"$efiNoPrompt`""
}
Invoke-Cmd `
    -Label 'oscdimg' `
    -CommandLine "`"$oscdimg`" -m -o -u2 -udfver102 $bootData `"$mediaRoot`" `"$isoPath`""

$wimSha = (Get-FileHash -LiteralPath $wimPath -Algorithm SHA256).Hash.ToLowerInvariant()
$isoSha = (Get-FileHash -LiteralPath $isoPath -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [pscustomobject]@{
    architecture = $Arch
    osdcloud_module_version = $OSDCloudVersion
    build_sha = $sha
    component_sha256 = $componentHashes
    output_wim = $wimPath
    output_iso = $isoPath
    output_manifest = $manifestPath
    wim_sha256 = $wimSha
    iso_sha256 = $isoSha
    adk_root = $adkRoot
    copype = $copyPe
    curl_source = $curlPath
    curl_source_url = $curlPayload.SourceUrl
    curl_package_sha256 = $curlPayload.PackageSha256
    curl_machine = ('0x{0:x4}' -f $curlPayload.Machine)
    optional_packages = $optionalPackages
    built_by_host = $env:COMPUTERNAME
    built_at = (Get-Date).ToString('o')
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
