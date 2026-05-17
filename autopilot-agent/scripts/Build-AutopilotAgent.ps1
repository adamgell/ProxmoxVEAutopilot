[CmdletBinding()]
param(
    [string]$Version = "0.1.2",
    [string[]]$RuntimeIdentifiers = @("win-x64", "win-arm64"),
    [string]$Configuration = "Release",
    [string]$OutputRoot = (Join-Path $PSScriptRoot "..\artifacts")
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$project = Join-Path $repoRoot "src\AutopilotAgent\AutopilotAgent.csproj"
$installer = Join-Path $repoRoot "installer\AutopilotAgent.Installer.wixproj"

function Ensure-NuGetOrgSource {
    $sourceUrl = "https://api.nuget.org/v3/index.json"
    $sources = (& dotnet nuget list source 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet nuget list source failed with exit code $LASTEXITCODE. $sources"
    }
    if ($sources -match "nuget\.org") {
        & dotnet nuget enable source nuget.org | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "dotnet nuget enable source nuget.org failed with exit code $LASTEXITCODE"
        }
        return
    }
    if ($sources -notmatch [regex]::Escape($sourceUrl)) {
        & dotnet nuget add source $sourceUrl --name nuget.org | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "dotnet nuget add source nuget.org failed with exit code $LASTEXITCODE"
        }
    }
}

Ensure-NuGetOrgSource

foreach ($rid in $RuntimeIdentifiers) {
    $installerPlatform = switch ($rid) {
        "win-x64" { "x64" }
        "win-arm64" { "arm64" }
        default { throw "Unsupported installer runtime identifier: $rid" }
    }
    $publishDir = Join-Path $OutputRoot "publish\$rid"
    $msiDir = Join-Path $OutputRoot "msi\$rid"
    New-Item -ItemType Directory -Force -Path $publishDir, $msiDir | Out-Null

    dotnet publish $project `
        -c $Configuration `
        -r $rid `
        -p:Version=$Version `
        -p:AutopilotAgentVersion=$Version `
        -o $publishDir
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet publish failed for $rid with exit code $LASTEXITCODE"
    }

    # WiX uses project-level obj state for output naming. Clean it between
    # RIDs so win-x64 and win-arm64 MSIs do not collide in incremental builds.
    Remove-Item -Recurse -Force (Join-Path (Split-Path $installer) "obj") -ErrorAction SilentlyContinue

    dotnet build $installer `
        -c $Configuration `
        -p:AutopilotAgentVersion=$Version `
        -p:RuntimeIdentifier=$rid `
        -p:InstallerPlatform=$installerPlatform `
        -p:PublishDir=$publishDir `
        -o $msiDir
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet build failed for $rid installer with exit code $LASTEXITCODE"
    }
}
