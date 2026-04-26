function Invoke-WriteUnattendStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target
    )
    $parent = Split-Path -Parent $Target
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $Target
    return [pscustomobject]@{ LogTail = "wrote unattend.xml ($Sha256) → $Target"; Extra = @{ target = $Target } }
}
