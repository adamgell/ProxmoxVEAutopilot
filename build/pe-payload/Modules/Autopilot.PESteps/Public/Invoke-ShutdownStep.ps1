function Invoke-ShutdownStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()
    Write-Host 'ShutdownStep: invoking wpeutil shutdown'
    wpeutil shutdown
    return [pscustomobject]@{ LogTail = 'wpeutil shutdown issued'; Extra = @{} }
}
