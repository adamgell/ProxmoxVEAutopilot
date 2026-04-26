$ErrorActionPreference = 'Stop'

# Import PETransport so Get-PeContent (and other transport cmdlets) are
# available in this module's scope and can be mocked by Pester.
$transportPsd1 = Join-Path $PSScriptRoot '..' 'Autopilot.PETransport' 'Autopilot.PETransport.psd1'
if (Test-Path $transportPsd1) {
    Import-Module $transportPsd1 -Force
}

$publicDir = Join-Path $PSScriptRoot 'Public'
if (Test-Path $publicDir) {
    Get-ChildItem -Path $publicDir -Filter '*.ps1' | ForEach-Object {
        . $_.FullName
        Export-ModuleMember -Function $_.BaseName
    }
}
