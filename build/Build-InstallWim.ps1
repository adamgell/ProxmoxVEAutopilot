#Requires -Version 7
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Build install.wim: extract from stock Windows ISO, DISM-inject virtio drivers, export.

.DESCRIPTION
    Reads a build-config JSON from -ConfigJson (a path) or - (stdin).
    Required fields: windowsIsoPath, virtioIsoPath, edition, architecture, drivers, outputDir.
    Optional: lockPath (default C:\BuildRoot\work\.build.lock).

    Produces in outputDir:
        install-<edition-slug>-<arch>-<sha>.wim
        install-<edition-slug>-<arch>-<sha>.json   (sidecar)
        install-<edition-slug>-<arch>-<sha>.log    (cmtrace)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ConfigJson
)

$ErrorActionPreference = 'Stop'

# ---- Load module ----
Import-Module (Join-Path $PSScriptRoot 'Modules\Autopilot.Build\Autopilot.Build.psd1') -Force

# ---- Parse config ----
$rawJson = if ($ConfigJson -eq '-') { [Console]::In.ReadToEnd() } else { Get-Content -LiteralPath $ConfigJson -Raw }
$config = $rawJson | ConvertFrom-Json

foreach ($key in @('windowsIsoPath','virtioIsoPath','edition','architecture','drivers','outputDir')) {
    if (-not $config.PSObject.Properties.Match($key)) {
        throw "Build config missing required field: $key"
    }
}

$lockPath = if ($config.PSObject.Properties.Match('lockPath')) { $config.lockPath } else { 'C:\BuildRoot\work\.build.lock' }
$workDir  = Split-Path -Parent $lockPath
if (-not (Test-Path $workDir)) { New-Item -ItemType Directory -Path $workDir -Force | Out-Null }

# Edition slug: lowercased with non-alnum replaced by '-'
$editionSlug = ($config.edition -replace '[^A-Za-z0-9]+', '-').Trim('-').ToLowerInvariant()
$arch        = $config.architecture
$tempName    = "install-$editionSlug-$arch-staging.wim"
$tempPath    = Join-Path $workDir $tempName
$mountPath   = Join-Path $workDir "mount-install-$arch"
if (Test-Path $mountPath) { Remove-Item $mountPath -Recurse -Force }
New-Item -ItemType Directory -Path $mountPath -Force | Out-Null

# ---- Acquire lock ----
$lock = New-BuildLock -Path $lockPath -Owner 'Build-InstallWim'
try {
    # ---- Set up logging ----
    $logTempPath = Join-Path $workDir "install-$editionSlug-$arch-staging.log"
    if (Test-Path $logTempPath) { Remove-Item $logTempPath -Force }
    Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Build start. edition=$($config.edition) arch=$arch"

    try {
        # ---- Mount ISOs ----
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Mounting Windows ISO: $($config.windowsIsoPath)"
        $winMount = Mount-DiskImage -ImagePath $config.windowsIsoPath -PassThru
        $winLetter = ($winMount | Get-Volume).DriveLetter
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Windows ISO mounted at ${winLetter}:"

        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Mounting virtio ISO: $($config.virtioIsoPath)"
        $virtioMount = Mount-DiskImage -ImagePath $config.virtioIsoPath -PassThru
        $virtioLetter = ($virtioMount | Get-Volume).DriveLetter
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "virtio ISO mounted at ${virtioLetter}:"

        # ---- Locate install.wim and edition index ----
        $srcWim = "${winLetter}:\sources\install.wim"
        if (-not (Test-Path $srcWim)) { throw "install.wim not found at $srcWim" }

        Copy-Item -Path $srcWim -Destination $tempPath -Force
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Copied $srcWim → $tempPath"

        $images = Get-WindowsImage -ImagePath $tempPath
        $match = $images | Where-Object { $_.ImageName -eq $config.edition }
        if (-not $match) {
            $available = ($images | ForEach-Object { $_.ImageName }) -join ', '
            throw "Edition '$($config.edition)' not found in install.wim. Available: $available"
        }
        $editionIndex = $match.ImageIndex
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Selected edition '$($config.edition)' at index $editionIndex"

        # ---- Mount the WIM ----
        Mount-WindowsImage -Path $mountPath -ImagePath $tempPath -Index $editionIndex | Out-Null
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Mounted install.wim at $mountPath"

        # ---- Inject drivers ----
        foreach ($driver in $config.drivers) {
            $driverPath = "${virtioLetter}:\$driver\w11\$arch"
            if (-not (Test-Path $driverPath)) {
                Write-CmTraceLog -Path $logTempPath -Severity Warning -Component 'Build-InstallWim' -Message "Driver path missing, skipping: $driverPath"
                continue
            }
            Add-WindowsDriver -Path $mountPath -Driver $driverPath -Recurse -ForceUnsigned | Out-Null
            Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Injected driver: $driver from $driverPath"
        }

        # ---- Dismount + commit ----
        Dismount-WindowsImage -Path $mountPath -Save | Out-Null
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Dismounted (committed)"

        # ---- Export to compact ----
        $exportTempPath = Join-Path $workDir "install-$editionSlug-$arch-export.wim"
        if (Test-Path $exportTempPath) { Remove-Item $exportTempPath -Force }
        Export-WindowsImage -SourceImagePath $tempPath -SourceIndex $editionIndex -DestinationImagePath $exportTempPath | Out-Null
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Exported to $exportTempPath"

        # ---- Hash + final filenames ----
        $sha  = Get-FileSha256 -Path $exportTempPath
        $size = (Get-Item $exportTempPath).Length
        $finalWim = Join-Path $config.outputDir "install-$editionSlug-$arch-$sha.wim"
        $finalJson= Join-Path $config.outputDir "install-$editionSlug-$arch-$sha.json"
        $finalLog = Join-Path $config.outputDir "install-$editionSlug-$arch-$sha.log"
        if (-not (Test-Path $config.outputDir)) { New-Item -ItemType Directory -Path $config.outputDir -Force | Out-Null }
        Move-Item -Path $exportTempPath -Destination $finalWim -Force

        # ---- Sidecar ----
        $sidecar = @{
            kind             = 'install-wim'
            sha256           = $sha
            size             = $size
            edition          = $config.edition
            architecture     = $arch
            sourceWindowsIso = @{
                path   = $config.windowsIsoPath
                sha256 = (Get-FileSha256 -Path $config.windowsIsoPath)
            }
            sourceVirtioIso  = @{
                path   = $config.virtioIsoPath
                sha256 = (Get-FileSha256 -Path $config.virtioIsoPath)
            }
            driversInjected  = $config.drivers
            buildHost        = $env:COMPUTERNAME
            buildTimestamp   = (Get-Date).ToUniversalTime().ToString('o')
            builderScript    = 'Build-InstallWim.ps1'
        }
        Write-ArtifactSidecar -Path $finalJson -Properties $sidecar
        Write-CmTraceLog -Path $logTempPath -Severity Info -Component 'Build-InstallWim' -Message "Wrote sidecar $finalJson"

        # ---- Move log to final ----
        Move-Item -Path $logTempPath -Destination $finalLog -Force

        Write-Host "BUILD OK"
        Write-Host "WIM:     $finalWim"
        Write-Host "Sidecar: $finalJson"
        Write-Host "Log:     $finalLog"
        Write-Host "sha256:  $sha"
        Write-Host "size:    $size"
    } finally {
        # ---- Cleanup mounted WIM if still mounted ----
        try { Dismount-WindowsImage -Path $mountPath -Discard -ErrorAction SilentlyContinue | Out-Null } catch {}
        if (Test-Path $tempPath) { Remove-Item $tempPath -Force -ErrorAction SilentlyContinue }
        if (Test-Path $mountPath) { Remove-Item $mountPath -Recurse -Force -ErrorAction SilentlyContinue }

        # ---- Cleanup ISOs ----
        try { Dismount-DiskImage -ImagePath $config.windowsIsoPath -ErrorAction SilentlyContinue | Out-Null } catch {}
        try { Dismount-DiskImage -ImagePath $config.virtioIsoPath  -ErrorAction SilentlyContinue | Out-Null } catch {}
    }
} finally {
    $lock.Release()
}
