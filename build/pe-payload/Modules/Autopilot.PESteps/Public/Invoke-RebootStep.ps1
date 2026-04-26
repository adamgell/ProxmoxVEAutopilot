function Invoke-RebootStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()
    Write-Host 'RebootStep: invoking wpeutil reboot'
    wpeutil reboot
    return [pscustomobject]@{ LogTail = 'wpeutil reboot issued'; Extra = @{} }
}
