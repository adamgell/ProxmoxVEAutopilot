[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$configPath = 'C:\ProgramData\SetupCm\labz1.local.yaml'
$expectedClientName = 'LABZ1-CMCLIENT01'
$actualClientName = 'RING0IVY24-01'
$targetPattern = '(?m)^(?<prefix>testClient:\r?\n(?:[ \t]*\r?\n)*  name: )LABZ1-CMCLIENT01(?<lineEnding>\r?)$'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Private LABZ1 Setup-CM config was not found at $configPath."
}

$content = [System.IO.File]::ReadAllText($configPath)
$matches = [regex]::Matches($content, $targetPattern)
if ($matches.Count -ne 1) {
    throw 'Expected exactly one stale testClient.name entry in the private LABZ1 Setup-CM config.'
}

$updated = [regex]::Replace(
    $content,
    $targetPattern,
    '${prefix}RING0IVY24-01${lineEnding}',
    1)
[System.IO.File]::WriteAllText(
    $configPath,
    $updated,
    [System.Text.UTF8Encoding]::new($false))

$readback = [System.IO.File]::ReadAllText($configPath)
if ([regex]::Matches($readback, $targetPattern).Count -ne 0 -or
    $readback -notmatch '(?m)^testClient:\r?\n(?:[ \t]*\r?\n)*  name: RING0IVY24-01\r?$') {
    throw 'Private LABZ1 Setup-CM config did not read back the exact test client target.'
}

[ordered]@{
    previous_client_name = $expectedClientName
    client_name = $actualClientName
} | ConvertTo-Json -Compress
