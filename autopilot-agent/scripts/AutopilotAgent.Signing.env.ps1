[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Endpoint,

    [Parameter(Mandatory = $true)]
    [string]$CodeSigningAccountName,

    [Parameter(Mandatory = $true)]
    [string]$CertificateProfileName,

    [string]$ArtifactsRoot = (Join-Path $PSScriptRoot "..\artifacts"),
    [string]$SignToolPath,
    [string]$ArtifactSigningDlibPath,
    [string]$TimestampUrl = "http://timestamp.acs.microsoft.com/"
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredCommand {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "$Name is required but was not found in PATH."
    }
    $cmd.Source
}

function Resolve-RequiredPath {
    param([string]$Path, [string]$Name)
    if (-not $Path -or -not (Test-Path $Path)) {
        throw "$Name was not found: $Path"
    }
    (Resolve-Path $Path).Path
}

$script:AzureCliPath = Resolve-RequiredCommand az
$script:DotNetPath = Resolve-RequiredCommand dotnet
$script:WixPath = Resolve-RequiredCommand wix
az account show | Out-Null

$dotnetInfo = dotnet --info
if ($dotnetInfo -notmatch "Microsoft\.NETCore\.App 8\.") {
    throw ".NET 8 runtime is required for AutopilotAgent validation."
}

if (-not $SignToolPath) {
    $sdkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    $SignToolPath = Get-ChildItem $sdkRoot -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $ArtifactSigningDlibPath) {
    $searchRoots = @(
        "$env:USERPROFILE",
        "$env:ProgramFiles",
        "${env:ProgramFiles(x86)}"
    ) | Where-Object { $_ -and (Test-Path $_) }
    $ArtifactSigningDlibPath = $searchRoots |
        ForEach-Object { Get-ChildItem $_ -Recurse -Filter Azure.CodeSigning.Dlib.dll -ErrorAction SilentlyContinue } |
        Where-Object { $_.FullName -match "\\x64\\Azure\.CodeSigning\.Dlib\.dll$" } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

$script:SignToolPath = Resolve-RequiredPath $SignToolPath "SignTool path"
$script:ArtifactSigningDlibPath = Resolve-RequiredPath $ArtifactSigningDlibPath "Artifact Signing dlib path"
$script:ArtifactsRoot = (New-Item -ItemType Directory -Force -Path $ArtifactsRoot).FullName
$script:SigningRoot = (New-Item -ItemType Directory -Force -Path (Join-Path $ArtifactsRoot "signing")).FullName
$script:SignedRoot = (New-Item -ItemType Directory -Force -Path (Join-Path $ArtifactsRoot "signed")).FullName
$script:TimestampUrl = $TimestampUrl
$script:ArtifactSigningEndpoint = $Endpoint
$script:ArtifactSigningAccountName = $CodeSigningAccountName
$script:ArtifactSigningCertificateProfileName = $CertificateProfileName
$script:ArtifactSigningMetadataPath = Join-Path $SigningRoot "metadata.json"

$metadata = [ordered]@{
    Endpoint               = $Endpoint
    CodeSigningAccountName = $CodeSigningAccountName
    CertificateProfileName = $CertificateProfileName
    CorrelationId          = [guid]::NewGuid().ToString()
    ExcludeCredentials     = @(
        "EnvironmentCredential",
        "ManagedIdentityCredential",
        "WorkloadIdentityCredential",
        "SharedTokenCacheCredential",
        "VisualStudioCredential",
        "VisualStudioCodeCredential",
        "AzurePowerShellCredential",
        "AzureDeveloperCliCredential",
        "InteractiveBrowserCredential"
    )
}
$metadata | ConvertTo-Json -Depth 4 | Set-Content -Path $ArtifactSigningMetadataPath -Encoding UTF8

Write-Host "AutopilotAgent signing environment ready."
Write-Host "Metadata: $ArtifactSigningMetadataPath"
