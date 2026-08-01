[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })]
    [string]$SetupCmRepository,

    [Parameter(Mandatory)]
    [string]$DestinationRoot
)

$ErrorActionPreference = 'Stop'

$repository = (Resolve-Path -LiteralPath $SetupCmRepository).Path
foreach ($relativePath in @(
    'Invoke-SetupCm.ps1',
    'src/SetupCm/SetupCm.psd1',
    'src/SetupCm/SetupCm.psm1'
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $repository $relativePath) -PathType Leaf)) {
        throw "Setup-CM repository is missing required runtime file: $relativePath"
    }
}

$dirtyRuntimePaths = @(
    & git -C $repository status --porcelain --untracked-files=no |
        ForEach-Object { $_.Substring(3).Trim() } |
        Where-Object { $_ -match '^(Invoke-SetupCm\.ps1|src/)' }
)
if ($dirtyRuntimePaths.Count -gt 0) {
    throw "Setup-CM runtime source has uncommitted changes: $($dirtyRuntimePaths -join ', ')"
}

$commit = (& git -C $repository rev-parse HEAD).Trim()
if ($commit -notmatch '^[A-Fa-f0-9]{40}$') {
    throw 'Setup-CM source commit could not be resolved.'
}

New-Item -ItemType Directory -Path $DestinationRoot -Force | Out-Null
$archivePath = Join-Path $DestinationRoot 'setup-cm.zip'
$temporaryArchive = "$archivePath.partial"
$manifestPath = Join-Path $DestinationRoot 'setup-cm.manifest.json'
$temporaryManifest = "$manifestPath.partial"
Remove-Item -LiteralPath $temporaryArchive, $temporaryManifest -Force -ErrorAction SilentlyContinue

Push-Location $repository
try {
    Compress-Archive -Path 'Invoke-SetupCm.ps1', 'src' -DestinationPath $temporaryArchive -Force
}
finally {
    Pop-Location
}

$sha256 = (Get-FileHash -LiteralPath $temporaryArchive -Algorithm SHA256).Hash
$manifest = [ordered]@{
    schema_version = 1
    filename = [IO.Path]::GetFileName($archivePath)
    sha256 = $sha256
    source_commit = $commit
    published_at_utc = (Get-Date).ToUniversalTime().ToString('o')
}
$manifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $temporaryManifest -Encoding utf8NoBOM
Move-Item -LiteralPath $temporaryArchive -Destination $archivePath -Force
Move-Item -LiteralPath $temporaryManifest -Destination $manifestPath -Force

[pscustomobject]@{
    archive_path = $archivePath
    manifest_path = $manifestPath
    sha256 = $sha256
    source_commit = $commit
} | ConvertTo-Json -Compress
