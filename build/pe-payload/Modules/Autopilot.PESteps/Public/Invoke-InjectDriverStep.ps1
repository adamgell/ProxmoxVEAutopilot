function Invoke-InjectDriverStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [int] $Size,
        [Parameter(Mandatory)] [string] $Target
    )
    $tmpZip = "X:\Windows\Temp\driver-$Sha256.zip"
    $tmpDir = "X:\Windows\Temp\driver-$Sha256"
    Get-PeContent -OrchestratorUrl $OrchestratorUrl -Sha256 $Sha256 -OutPath $tmpZip
    if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
    Expand-Archive -LiteralPath $tmpZip -DestinationPath $tmpDir -Force
    Add-WindowsDriver -Path "$Target\Windows" -Driver $tmpDir -Recurse -ForceUnsigned -ErrorAction Stop | Out-Null
    Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
    Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    return [pscustomobject]@{ LogTail = "injected driver $Sha256 → $Target\Windows"; Extra = @{ target = $Target } }
}
