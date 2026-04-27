function Invoke-LogStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param([Parameter(Mandatory)] [string] $Message)
    Write-Host "LogStep: $Message"
    return [pscustomobject]@{ LogTail = $Message; Extra = @{} }
}
