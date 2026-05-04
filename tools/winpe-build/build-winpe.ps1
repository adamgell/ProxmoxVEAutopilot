<#
.SYNOPSIS
    Build a custom WinPE image for the ProxmoxVEAutopilot phase-0 agent.

.DESCRIPTION
    Wraps Microsoft ADK + DISM. Produces winpe-autopilot-<arch>-<sha>.iso
    plus a sibling .wim and a manifest .json. -DryRun returns the planned
    output paths without invoking ADK.

.PARAMETER Arch
    amd64 | arm64

.PARAMETER OutputDir
    Where to drop the artifacts. Default: F:\BuildRoot\outputs.

.PARAMETER DryRun
    Resolve all inputs and print the planned outputs, do not invoke ADK.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('amd64','arm64')]
    [string] $Arch,

    [string] $OutputDir = 'F:\BuildRoot\outputs',

    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-BuildSha {
    param([string] $Arch)
    $inputs = @(
        $PSScriptRoot,
        (Get-Item "$PSScriptRoot/Invoke-AutopilotWinPE.ps1").LastWriteTimeUtc.Ticks.ToString(),
        (Get-Item "$PSScriptRoot/config.json").LastWriteTimeUtc.Ticks.ToString(),
        (Get-Item "$PSScriptRoot/startnet.cmd").LastWriteTimeUtc.Ticks.ToString(),
        $Arch
    ) -join '|'
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($inputs)
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return ($hash[0..7] | ForEach-Object { $_.ToString('x2') }) -join ''
}

$sha = Get-BuildSha -Arch $Arch
$base = "winpe-autopilot-$Arch-$sha"
$wimPath      = [System.IO.Path]::Combine($OutputDir, "$base.wim")
$isoPath      = [System.IO.Path]::Combine($OutputDir, "$base.iso")
$manifestPath = [System.IO.Path]::Combine($OutputDir, "$base.json")

Write-Output $manifestPath
Write-Output $wimPath
Write-Output $isoPath

if ($DryRun) { return }

$adkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit"
$peRoot = "$adkRoot\Windows Preinstallation Environment"
$copyPe = "$peRoot\copype.cmd"
if (-not (Test-Path -LiteralPath $copyPe)) {
    throw "ADK + WinPE add-on not installed (looked for $copyPe)"
}

$workRoot = Join-Path $env:TEMP "winpe-build-$Arch-$(New-Guid)"
& cmd /c "`"$copyPe`" $Arch `"$workRoot`"" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "copype failed: $LASTEXITCODE" }

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

    # Bake-in WinPE-time drivers. vioscsi is REQUIRED so WinPE can see
    # the virtio-scsi-pci disk (otherwise diskpart's `select disk 0`
    # will fail). NetKVM is REQUIRED so the agent can phone home.
    # vioserial is included so any phase-0 fallback to QGA still works.
    # Other drivers (Balloon, vioinput, viogpudo) are needed only by the
    # OS post-apply and are injected into V:\ via dism /add-driver in
    # phase 0 from the still-attached VirtIO ISO -- not baked into WinPE.
    $virtioRoot = $null
    foreach ($candidate in @('D:\virtio','F:\BuildRoot\inputs\virtio-win')) {
        if (Test-Path -LiteralPath $candidate) { $virtioRoot = $candidate; break }
    }
    if (-not $virtioRoot) {
        throw "WinPE build needs a VirtIO driver source at D:\virtio or F:\BuildRoot\inputs\virtio-win"
    }
    foreach ($infName in @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')) {
        $inf = Get-ChildItem -Path $virtioRoot -Recurse -Filter $infName |
            Where-Object FullName -match "\\$Arch\\" |
            Select-Object -First 1
        if (-not $inf) {
            throw "WinPE build cannot find $infName under $virtioRoot for $Arch"
        }
        & dism.exe /Image:$mountDir /Add-Driver /Driver:$($inf.FullName) /ForceUnsigned | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Add-Driver $infName failed: $LASTEXITCODE" }
    }

    # Stage agent files.
    $autopilotDir = Join-Path $mountDir 'autopilot'
    New-Item -ItemType Directory -Path $autopilotDir -Force | Out-Null
    Copy-Item "$PSScriptRoot\Invoke-AutopilotWinPE.ps1" -Destination $autopilotDir
    $gwapiPath = Join-Path $autopilotDir 'Get-WindowsAutopilotInfo.ps1'
    $gwapiSource = Join-Path $PSScriptRoot 'vendored/Get-WindowsAutopilotInfo.ps1'
    if (Test-Path -LiteralPath $gwapiSource) {
        Copy-Item $gwapiSource -Destination $gwapiPath
    } else {
        Invoke-WebRequest -Uri 'https://www.powershellgallery.com/api/v2/package/Get-WindowsAutopilotInfo' `
            -OutFile (Join-Path $env:TEMP 'gwapi.nupkg')
        Expand-Archive (Join-Path $env:TEMP 'gwapi.nupkg') -DestinationPath (Join-Path $env:TEMP 'gwapi-extract') -Force
        Get-ChildItem (Join-Path $env:TEMP 'gwapi-extract') -Recurse -Filter 'Get-WindowsAutopilotInfo.ps1' |
            Select-Object -First 1 |
            Copy-Item -Destination $gwapiPath
    }
    Copy-Item "$PSScriptRoot\config.json" -Destination $autopilotDir
    Copy-Item "$PSScriptRoot\startnet.cmd" -Destination (Join-Path $mountDir 'Windows\System32\startnet.cmd') -Force
} finally {
    & dism.exe /Unmount-Image /MountDir:$mountDir /Commit | Out-Null
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}
Copy-Item $bootWim -Destination $wimPath -Force

$makeIso = "$peRoot\MakeWinPEMedia.cmd"
& cmd /c "`"$makeIso`" /ISO `"$workRoot`" `"$isoPath`"" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "MakeWinPEMedia failed: $LASTEXITCODE" }

$wimSha = (Get-FileHash -LiteralPath $wimPath -Algorithm SHA256).Hash
$manifest = [pscustomobject]@{
    arch = $Arch
    build_sha = $sha
    output_wim = $wimPath
    output_iso = $isoPath
    wim_sha256 = $wimSha
    adk_root = $adkRoot
    optional_packages = $optionalPackages
    built_at = (Get-Date).ToString('o')
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
