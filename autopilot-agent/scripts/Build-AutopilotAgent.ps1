[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [string[]]$RuntimeIdentifiers = @("win-x64", "win-arm64"),
    [string]$Configuration = "Release",
    [string]$OutputRoot = (Join-Path $PSScriptRoot "..\artifacts")
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$project = Join-Path $repoRoot "src\AutopilotAgent\AutopilotAgent.csproj"
$installer = Join-Path $repoRoot "installer\AutopilotAgent.Installer.wixproj"

foreach ($rid in $RuntimeIdentifiers) {
    $publishDir = Join-Path $OutputRoot "publish\$rid"
    $msiDir = Join-Path $OutputRoot "msi\$rid"
    New-Item -ItemType Directory -Force -Path $publishDir, $msiDir | Out-Null

    dotnet publish $project `
        -c $Configuration `
        -r $rid `
        -p:Version=$Version `
        -p:AutopilotAgentVersion=$Version `
        -o $publishDir

    dotnet build $installer `
        -c $Configuration `
        -p:AutopilotAgentVersion=$Version `
        -p:RuntimeIdentifier=$rid `
        -p:PublishDir=$publishDir `
        -o $msiDir
}
