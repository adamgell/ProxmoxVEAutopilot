$ErrorActionPreference = 'Stop'
$publicDir = Join-Path $PSScriptRoot 'Public'
if (Test-Path $publicDir) {
    foreach ($file in (Get-ChildItem -Path $publicDir -Filter '*.ps1')) {
        . $file.FullName
    }
}
