function Invoke-StageFilesStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target
    )
    $tmp = "X:\Windows\Temp\stage-$Sha256.zip"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmp
    Expand-Archive -LiteralPath $tmp -DestinationPath $Target -Force
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "staged zip $Sha256 → $Target"; Extra = @{ target = $Target } }
}
