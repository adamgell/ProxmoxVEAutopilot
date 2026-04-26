function Invoke-ApplyWimStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target,
        [int] $Index = 1
    )
    $tmp = "X:\Windows\Temp\install-$Sha256.wim"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmp
    Expand-WindowsImage -ImagePath $tmp -Index $Index -ApplyPath $Target -ErrorAction Stop
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "applied wim $Sha256 → $Target (index $Index)"; Extra = @{ target = $Target } }
}
