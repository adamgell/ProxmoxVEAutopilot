[CmdletBinding()]
param(
    [string]$ArtifactsRoot = (Join-Path $PSScriptRoot "..\artifacts"),
    [string]$SignToolPath = $script:SignToolPath,
    [string]$ArtifactSigningDlibPath = $script:ArtifactSigningDlibPath,
    [string]$MetadataPath = $script:ArtifactSigningMetadataPath,
    [string]$DotNetRootForSigning,
    [string]$TimestampUrl = $(if ($script:TimestampUrl) { $script:TimestampUrl } else { "http://timestamp.acs.microsoft.com/" })
)

$ErrorActionPreference = "Stop"

foreach ($required in @(
    @{ Name = "SignTool"; Path = $SignToolPath },
    @{ Name = "Artifact Signing dlib"; Path = $ArtifactSigningDlibPath },
    @{ Name = "metadata.json"; Path = $MetadataPath }
)) {
    if (-not $required.Path -or -not (Test-Path $required.Path)) {
        throw "$($required.Name) was not found: $($required.Path)"
    }
}

if ($DotNetRootForSigning) {
    if (-not (Test-Path $DotNetRootForSigning)) {
        throw "Signing .NET root was not found: $DotNetRootForSigning"
    }
    $env:DOTNET_ROOT = (Resolve-Path $DotNetRootForSigning).Path
    $env:PATH = "$env:DOTNET_ROOT;$env:PATH"
}

$targets = Get-ChildItem $ArtifactsRoot -Recurse -File |
    Where-Object {
        $_.Name -eq "AutopilotAgent.exe" -or
        ($_.Name -match "^AutopilotAgent-.+\.msi$")
    }

if (-not $targets) {
    throw "No AutopilotAgent.exe or AutopilotAgent MSI files found under $ArtifactsRoot."
}

foreach ($target in $targets) {
    & $SignToolPath sign /v /debug /fd SHA256 `
        /tr $TimestampUrl /td SHA256 `
        /dlib $ArtifactSigningDlibPath `
        /dmdf $MetadataPath `
        $target.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Signing failed for $($target.FullName) with exit $LASTEXITCODE."
    }

    & $SignToolPath verify /pa /v $target.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "signtool.exe verify failed for $($target.FullName) with exit $LASTEXITCODE."
    }
}

Write-Host "Signed and verified $($targets.Count) AutopilotAgent artifact(s)."
