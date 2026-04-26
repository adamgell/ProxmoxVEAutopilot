$ErrorActionPreference = 'Stop'
$publicDir = Join-Path $PSScriptRoot 'Public'
if (Test-Path $publicDir) {
    Get-ChildItem -Path $publicDir -Filter '*.ps1' | ForEach-Object {
        . $_.FullName
        Export-ModuleMember -Function $_.BaseName
    }
}
