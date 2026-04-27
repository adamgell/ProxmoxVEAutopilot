function Invoke-ApplyWimStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [long] $Size,
        [Parameter(Mandatory)] [string] $Target,
        [int] $Index = 1
    )
    # Download to the target partition, not X: (RAM disk, ~512MB — too small for WIMs)
    $tmp = "${Target}\install-$Sha256.wim"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmp
    Expand-WindowsImage -ImagePath $tmp -Index $Index -ApplyPath $Target -ErrorAction Stop
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "applied wim $Sha256 → $Target (index $Index)"; Extra = @{ target = $Target } }
}
