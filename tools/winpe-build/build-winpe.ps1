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

# Real build path is implemented in Tasks E2-E5 below.
throw "build-winpe.ps1: real build path not yet implemented; use -DryRun"
