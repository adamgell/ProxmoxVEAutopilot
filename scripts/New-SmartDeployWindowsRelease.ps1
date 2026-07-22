#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter()]
    [string] $IsoPath,

    [Parameter()]
    [string] $EditionName = 'Windows 11 Enterprise',

    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string] $ReleaseName,

    [Parameter()]
    [string] $SmartDeployRoot = 'E:\SmartDeploy',

    [Parameter()]
    [string] $RemoteInstallRoot = 'E:\RemoteInstall',

    [Parameter()]
    [string] $AnswerFilePath,

    [Parameter()]
    [ValidateSet('x64', 'arm64')]
    [string] $Architecture = 'x64',

    [Parameter()]
    [string] $WdsBootImageName,

    [Parameter()]
    [string] $SmartDeployShareUncRoot,

    [Parameter()]
    [string] $SmartDeployCapturedImagePath,

    [Parameter()]
    [string] $OrganizationalUnit,

    [Parameter()]
    [switch] $PlanOnly,

    [Parameter()]
    [switch] $UpdateAnswerFileOnly,

    [Parameter()]
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Join-SdPath {
    param(
        [Parameter(Mandatory)]
        [string] $Base,

        [Parameter(Mandatory)]
        [string[]] $Child
    )

    $current = $Base
    foreach ($part in $Child) {
        $current = Join-Path -Path $current -ChildPath $part
    }

    $current
}

function New-SmartDeployReleasePaths {
    param(
        [Parameter(Mandatory)]
        [string] $Root,

        [Parameter(Mandatory)]
        [string] $Name
    )

    $images = Join-SdPath -Base $Root -Child @('Images')
    $answerFiles = Join-SdPath -Base $Root -Child @('Answer Files')
    $platformPacks = Join-SdPath -Base $Root -Child @('Platform Packs')
    $bootMedia = Join-SdPath -Base $Root -Child @('Boot Media')
    $scratch = Join-SdPath -Base $Root -Child @('Scratch')
    $targetWim = Join-SdPath -Base $images -Child @("$Name-install.wim")

    [pscustomobject]@{
        root = $Root
        images = $images
        answer_files = $answerFiles
        platform_packs = $platformPacks
        boot_media = $bootMedia
        scratch = $scratch
        target_wim = $targetWim
        metadata_path = [System.IO.Path]::ChangeExtension($targetWim, '.metadata.json')
    }
}

function Ensure-SmartDeployReleaseDirectories {
    param(
        [Parameter(Mandatory)]
        [pscustomobject] $Paths
    )

    foreach ($path in @(
            $Paths.root,
            $Paths.images,
            $Paths.answer_files,
            $Paths.platform_packs,
            $Paths.boot_media,
            $Paths.scratch
        )) {
        if (-not (Test-Path -LiteralPath $path)) {
            if ($PSCmdlet.ShouldProcess($path, 'Create SmartDeploy release directory')) {
                New-Item -ItemType Directory -Path $path -Force | Out-Null
            }
        }
    }
}

function Mount-ReleaseIso {
    param(
        [Parameter(Mandatory)]
        [string] $IsoPath
    )

    $mountedByScript = $false
    $diskImage = Get-DiskImage -ImagePath $IsoPath -ErrorAction SilentlyContinue
    if ($null -eq $diskImage -or -not $diskImage.Attached) {
        $diskImage = Mount-DiskImage -ImagePath $IsoPath -PassThru
        $mountedByScript = $true
    }

    $volume = $diskImage | Get-Volume | Where-Object DriveLetter | Select-Object -First 1
    if ($null -eq $volume) {
        throw "Mounted ISO '$IsoPath' does not have a drive letter."
    }

    [pscustomobject]@{
        disk_image = $diskImage
        mounted_by_script = $mountedByScript
        drive_root = "$($volume.DriveLetter):\"
    }
}

function ConvertTo-ReleaseImageInfo {
    param(
        [Parameter(Mandatory)]
        [object[]] $Images
    )

    @($Images | ForEach-Object {
            [pscustomobject]@{
                ImageIndex = $_.ImageIndex
                ImageName = $_.ImageName
                ImageDescription = $_.ImageDescription
            }
        })
}

function Find-AnswerXmlNodes {
    param(
        [Parameter(Mandatory)]
        [xml] $Xml,

        [Parameter(Mandatory)]
        [string[]] $FieldNames
    )

    $wanted = @{}
    foreach ($fieldName in $FieldNames) {
        $wanted[$fieldName.ToLowerInvariant()] = $true
    }

    @($Xml.SelectNodes('//*') | Where-Object {
            $localName = $_.LocalName.ToLowerInvariant()
            if ($wanted.ContainsKey($localName)) {
                return $true
            }

            foreach ($attribute in @($_.Attributes)) {
                $attributeName = $attribute.Name.ToLowerInvariant()
                $attributeValue = [string] $attribute.Value
                if (($attributeName -eq 'name' -or $attributeName -eq 'key' -or $attributeName -eq 'id') -and
                    $wanted.ContainsKey($attributeValue.ToLowerInvariant())) {
                    return $true
                }
            }

            return $false
        })
}

function Set-AnswerXmlField {
    param(
        [Parameter(Mandatory)]
        [xml] $Xml,

        [Parameter(Mandatory)]
        [string[]] $FieldNames,

        [Parameter(Mandatory)]
        [string] $Value,

        [Parameter(Mandatory)]
        [string] $Description
    )

    $nodes = @(Find-AnswerXmlNodes -Xml $Xml -FieldNames $FieldNames)
    if ($nodes.Count -eq 0) {
        throw "Could not find SmartDeploy answer field '$Description'. Looked for: $($FieldNames -join ', ')."
    }

    foreach ($node in $nodes) {
        $valueAttribute = $node.Attributes['value']
        if ($null -ne $valueAttribute) {
            $valueAttribute.Value = $Value
        }
        else {
            $node.InnerText = $Value
        }
    }

    $nodes.Count
}

function Update-SmartDeployAnswerFile {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter()]
        [string] $ImageFile,

        [Parameter()]
        [string] $OuDn
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Answer file '$Path' does not exist."
    }

    [xml]$answerXml = Get-Content -LiteralPath $Path
    $updates = @()

    if (-not [string]::IsNullOrWhiteSpace($ImageFile)) {
        $count = Set-AnswerXmlField `
            -Xml $answerXml `
            -FieldNames @('image_file', 'imagefile', 'image_path', 'imagepath') `
            -Value $ImageFile `
            -Description 'image_file'
        $updates += [pscustomobject]@{ field = 'image_file'; value = $ImageFile; nodes_updated = $count }
    }

    if (-not [string]::IsNullOrWhiteSpace($OuDn)) {
        $count = Set-AnswerXmlField `
            -Xml $answerXml `
            -FieldNames @('organizational_unit', 'organizationalunit', 'ou', 'machine_object_ou') `
            -Value $OuDn `
            -Description 'organizational_unit'
        $updates += [pscustomobject]@{ field = 'organizational_unit'; value = $OuDn; nodes_updated = $count }
    }

    if ($updates.Count -gt 0) {
        if ($PSCmdlet.ShouldProcess($Path, 'Update SmartDeploy answer XML')) {
            $answerXml.Save($Path)
        }
    }

    @($updates)
}

$paths = New-SmartDeployReleasePaths -Root $SmartDeployRoot -Name $ReleaseName
if ([string]::IsNullOrWhiteSpace($WdsBootImageName)) {
    $WdsBootImageName = "SmartDeploy WinPE $Architecture - $ReleaseName"
}

$targetWim = $paths.target_wim
$metadataPath = $paths.metadata_path
$targetImageForAnswer = $null
if (-not [string]::IsNullOrWhiteSpace($SmartDeployCapturedImagePath)) {
    $targetImageForAnswer = $SmartDeployCapturedImagePath
    if (-not [string]::IsNullOrWhiteSpace($SmartDeployShareUncRoot)) {
        $rootPrefix = $SmartDeployRoot.TrimEnd('\')
        if ($SmartDeployCapturedImagePath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            $relativePath = $SmartDeployCapturedImagePath.Substring($rootPrefix.Length).TrimStart('\')
            $targetImageForAnswer = Join-SdPath -Base $SmartDeployShareUncRoot -Child ($relativePath -split '\\')
        }
    }
}

if ($UpdateAnswerFileOnly) {
    if ([string]::IsNullOrWhiteSpace($AnswerFilePath)) {
        throw '-AnswerFilePath is required with -UpdateAnswerFileOnly.'
    }

    $answerUpdates = Update-SmartDeployAnswerFile `
        -Path $AnswerFilePath `
        -ImageFile $targetImageForAnswer `
        -OuDn $OrganizationalUnit

    [pscustomobject]@{
        release_name = $ReleaseName
        architecture = $Architecture
        target_wim = $targetWim
        raw_iso_install_wim = $targetWim
        smartdeploy_captured_image_path = $SmartDeployCapturedImagePath
        answer_file = $AnswerFilePath
        target_image_for_answer = $targetImageForAnswer
        answer_updates = $answerUpdates
        raw_iso_wim_answer_file_compatible = $false
        requires_smartdeploy_capture_wizard_image = $true
        boot_media_creation = 'manual_smartdeploy_media_wizard'
        manual_smartdeploy_media_wizard = $true
    } | ConvertTo-Json -Depth 8
    return
}

if ([string]::IsNullOrWhiteSpace($IsoPath)) {
    throw '-IsoPath is required unless -UpdateAnswerFileOnly is used.'
}

if (-not (Test-Path -LiteralPath $IsoPath)) {
    throw "ISO '$IsoPath' does not exist."
}

$mountedByScript = $false
$sourceWim = $null

try {
    $mountedIso = Mount-ReleaseIso -IsoPath $IsoPath
    $mountedByScript = $mountedIso.mounted_by_script
    $sourceWim = Join-Path -Path $mountedIso.drive_root -ChildPath 'sources\install.wim'
    if (-not (Test-Path -LiteralPath $sourceWim)) {
        throw "ISO '$IsoPath' does not contain sources\install.wim."
    }

    $images = @(Get-WindowsImage -ImagePath $sourceWim)
    $releaseImages = ConvertTo-ReleaseImageInfo -Images $images
    $selectedImage = $releaseImages | Where-Object ImageName -eq $EditionName | Select-Object -First 1
    if ($null -eq $selectedImage) {
        $available = ($releaseImages | ForEach-Object ImageName) -join ', '
        throw "Edition '$EditionName' was not found in '$sourceWim'. Available images: $available."
    }

    $plan = [pscustomobject]@{
        release_name = $ReleaseName
        architecture = $Architecture
        iso_path = $IsoPath
        source_wim = $sourceWim
        selected_edition = $selectedImage
        smartdeploy_root = $SmartDeployRoot
        remoteinstall_root = $RemoteInstallRoot
        target_wim = $targetWim
        raw_iso_install_wim = $targetWim
        metadata_path = $metadataPath
        answer_file = $AnswerFilePath
        target_image_for_answer = $targetImageForAnswer
        smartdeploy_captured_image_path = $SmartDeployCapturedImagePath
        raw_iso_wim_answer_file_compatible = $false
        requires_smartdeploy_capture_wizard_image = $true
        smartdeploy_wim_customdata_required = $true
        organizational_unit = $OrganizationalUnit
        wds_boot_image_name = $WdsBootImageName
        expected_boot_wim = Join-SdPath -Base $paths.boot_media -Child @("$ReleaseName-boot.wim")
        boot_media_creation = 'manual_smartdeploy_media_wizard'
        manual_smartdeploy_media_wizard = $true
        images = $releaseImages
    }

    if ($PlanOnly) {
        $plan | ConvertTo-Json -Depth 8
        return
    }

    Ensure-SmartDeployReleaseDirectories -Paths $paths

    $shouldCopy = $true
    if (Test-Path -LiteralPath $targetWim) {
        if (-not $Force) {
            $sourceLength = (Get-Item -LiteralPath $sourceWim).Length
            $targetLength = (Get-Item -LiteralPath $targetWim).Length
            if ($sourceLength -ne $targetLength) {
                throw "Target WIM '$targetWim' already exists with a different length. Re-run with -Force to overwrite it."
            }

            $shouldCopy = $false
        }
    }

    $copyStarted = Get-Date
    if ($shouldCopy) {
        if ($PSCmdlet.ShouldProcess($targetWim, "Copy full install.wim from $IsoPath")) {
            Copy-Item -LiteralPath $sourceWim -Destination $targetWim -Force:$Force
        }
    }
    $copySeconds = [Math]::Round(((Get-Date) - $copyStarted).TotalSeconds, 1)

    $sourceHash = Get-FileHash -Algorithm SHA256 -LiteralPath $sourceWim
    $targetHash = Get-FileHash -Algorithm SHA256 -LiteralPath $targetWim
    $hashesMatch = $sourceHash.Hash -eq $targetHash.Hash
    if (-not $hashesMatch) {
        throw "Hash mismatch after staging '$targetWim'. Source=$($sourceHash.Hash) Target=$($targetHash.Hash)."
    }

    $answerUpdates = @()
    if (-not [string]::IsNullOrWhiteSpace($AnswerFilePath)) {
        $answerUpdates = Update-SmartDeployAnswerFile `
            -Path $AnswerFilePath `
            -ImageFile $targetImageForAnswer `
            -OuDn $OrganizationalUnit
    }

    $metadata = [ordered]@{
        release_name = $ReleaseName
        architecture = $Architecture
        source_iso = $IsoPath
        source_iso_length = (Get-Item -LiteralPath $IsoPath).Length
        source_wim = $sourceWim
        source_wim_length = (Get-Item -LiteralPath $sourceWim).Length
        target_wim = $targetWim
        target_wim_length = (Get-Item -LiteralPath $targetWim).Length
        raw_iso_install_wim = $targetWim
        raw_iso_install_wim_length = (Get-Item -LiteralPath $targetWim).Length
        edition_name = $EditionName
        image_index = $selectedImage.ImageIndex
        image_name = $selectedImage.ImageName
        smartdeploy_captured_image_path = $SmartDeployCapturedImagePath
        target_image_for_answer = $targetImageForAnswer
        raw_iso_wim_answer_file_compatible = $false
        requires_smartdeploy_capture_wizard_image = $true
        smartdeploy_wim_customdata_required = $true
        answer_file = $AnswerFilePath
        answer_updates = $answerUpdates
        source_sha256 = $sourceHash.Hash
        target_sha256 = $targetHash.Hash
        hashes_match = $hashesMatch
        copied = $shouldCopy
        copied_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        copy_seconds = $copySeconds
        smartdeploy_root = $SmartDeployRoot
        remoteinstall_root = $RemoteInstallRoot
        wds_boot_image_name = $WdsBootImageName
        expected_boot_wim = $plan.expected_boot_wim
        boot_media_creation = 'manual_smartdeploy_media_wizard'
        manual_smartdeploy_media_wizard = $true
        images = $releaseImages
    }

    if ($PSCmdlet.ShouldProcess($metadataPath, 'Write SmartDeploy release metadata')) {
        $metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    }

    [pscustomobject]$metadata | ConvertTo-Json -Depth 8
}
finally {
    if ($mountedByScript) {
        Dismount-DiskImage -ImagePath $IsoPath | Out-Null
    }
}
