function Invoke-RebootStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()
    # Don't reboot here — return a flag so Bootstrap.ps1 can stage files first
    Write-Host 'RebootStep: reboot deferred to bootstrap (staging files first)'
    return [pscustomobject]@{ LogTail = 'reboot deferred'; Extra = @{ deferred = $true } }
}
