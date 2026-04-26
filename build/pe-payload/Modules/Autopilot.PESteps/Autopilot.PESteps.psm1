$ErrorActionPreference = 'Stop'

$transportPsd1 = Join-Path $PSScriptRoot '..' 'Autopilot.PETransport' 'Autopilot.PETransport.psd1'
if (Test-Path $transportPsd1) {
    Import-Module $transportPsd1 -Force
}

$publicDir = Join-Path $PSScriptRoot 'Public'
if (Test-Path $publicDir) {
    foreach ($file in (Get-ChildItem -Path $publicDir -Filter '*.ps1')) {
        . $file.FullName
    }
}
