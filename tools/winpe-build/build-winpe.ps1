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
$dandISetEnv = "$adkRoot\Deployment Tools\DandISetEnv.bat"
$oscdimgRoot = "$adkRoot\Deployment Tools\$Arch\Oscdimg"
$peRoot = "$adkRoot\Windows Preinstallation Environment"
$copyPe = "$peRoot\copype.cmd"
if (-not (Test-Path -LiteralPath $dandISetEnv)) {
    throw "ADK Deployment Tools not installed (looked for $dandISetEnv)"
}
if (-not (Test-Path -LiteralPath $copyPe)) {
    throw "ADK + WinPE add-on not installed (looked for $copyPe)"
}
$oscdimg = "$oscdimgRoot\oscdimg.exe"
$efiNoPrompt = "$oscdimgRoot\efisys_noprompt.bin"
if (-not (Test-Path -LiteralPath $oscdimg)) {
    throw "ADK oscdimg.exe not installed (looked for $oscdimg)"
}
if (-not (Test-Path -LiteralPath $efiNoPrompt)) {
    throw "ADK UEFI no-prompt boot image not installed (looked for $efiNoPrompt)"
}

function Invoke-Cmd {
    param(
        [Parameter(Mandatory)]
        [string] $Label,

        [Parameter(Mandatory)]
        [string] $CommandLine
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
        if ($hasNative) {
            $PSNativeCommandUseErrorActionPreference = $oldNative
        }
    }
    if ($output) { $output | Write-Host }
    if ($exitCode -ne 0) { throw "$Label failed: $exitCode" }
}

function Resolve-VirtioInf {
    param(
        [Parameter(Mandatory)]
        [string] $Root,

        [Parameter(Mandatory)]
        [string] $InfName,

        [Parameter(Mandatory)]
        [string] $Arch
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

$workRoot = Join-Path $env:TEMP "winpe-build-$Arch-$(New-Guid)"
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

    # Bake-in WinPE-time drivers. vioscsi is REQUIRED so WinPE can see
    # the virtio-scsi-pci disk (otherwise diskpart's `select disk 0`
    # will fail). NetKVM is REQUIRED so the agent can phone home.
    # vioserial is included so any phase-0 fallback to QGA still works.
    # Other drivers (Balloon, vioinput, viogpudo) are needed only by the
    # OS post-apply and are injected into V:\ via dism /add-driver in
    # phase 0 from the still-attached VirtIO ISO -- not baked into WinPE.
    $virtioRoot = $null
    $virtioInfNames = @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')
    foreach ($candidate in @('D:\virtio','F:\BuildRoot\inputs\virtio','F:\BuildRoot\inputs\virtio-win')) {
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
        throw "WinPE build needs a VirtIO driver source at D:\virtio, F:\BuildRoot\inputs\virtio, or F:\BuildRoot\inputs\virtio-win"
    }
    foreach ($infName in $virtioInfNames) {
        $inf = Resolve-VirtioInf -Root $virtioRoot -InfName $infName -Arch $Arch
        if (-not $inf) {
            throw "WinPE build cannot find $infName under $virtioRoot for $Arch"
        }
        Write-Host "Adding VirtIO driver $infName from $($inf.FullName)"
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
        $gwapiArchive = Join-Path $env:TEMP 'gwapi.zip'
        Invoke-WebRequest -Uri 'https://www.powershellgallery.com/api/v2/package/Get-WindowsAutopilotInfo' `
            -OutFile $gwapiArchive
        Expand-Archive $gwapiArchive -DestinationPath (Join-Path $env:TEMP 'gwapi-extract') -Force
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
