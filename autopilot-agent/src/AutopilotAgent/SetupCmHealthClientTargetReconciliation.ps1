[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$configPath = 'C:\ProgramData\SetupCm\labz1.local.yaml'
$expectedClientName = 'LABZ1-CMCLIENT01'
$actualClientName = 'RING0IVY24-01'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Private LABZ1 Setup-CM config was not found at $configPath."
}

$lines = [System.IO.File]::ReadAllLines($configPath)
$testClientIndexes = @(
    for ($index = 0; $index -lt $lines.Length; $index++) {
        if ($lines[$index].TrimStart([char]0xFEFF).Trim() -eq 'testClient:') {
            $index
        }
    }
)
if ($testClientIndexes.Count -ne 1) {
    throw 'Expected exactly one testClient block in the private LABZ1 Setup-CM config.'
}

$testClientIndex = $testClientIndexes[0]
$candidateIndexes = @(
    for (
        $index = $testClientIndex + 1;
        $index -le [Math]::Min($lines.Length - 1, $testClientIndex + 3);
        $index++
    ) {
        if ($lines[$index].Trim() -eq "name: $expectedClientName") {
            $index
        }
    }
)
if ($candidateIndexes.Count -ne 1) {
    throw 'Expected exactly one stale testClient.name entry in the next three testClient block lines.'
}
if (@($lines | Where-Object { $_.Trim() -eq "name: $expectedClientName" }).Count -ne 1) {
    throw 'The stale client name is not unique in the private LABZ1 Setup-CM config.'
}

$candidateIndex = $candidateIndexes[0]
$updatedLine = [regex]::Replace(
    $lines[$candidateIndex],
    '^(?<indent>\s*)name:\s*LABZ1-CMCLIENT01\s*$',
    '${indent}name: RING0IVY24-01',
    1)
if ($updatedLine -eq $lines[$candidateIndex]) {
    throw 'The exact stale test client name could not be replaced.'
}
$lines[$candidateIndex] = $updatedLine
[System.IO.File]::WriteAllLines(
    $configPath,
    $lines,
    [System.Text.UTF8Encoding]::new($false))

$readback = [System.IO.File]::ReadAllLines($configPath)
if ($readback.Length -le $candidateIndex -or
    $readback[$candidateIndex].Trim() -ne "name: $actualClientName") {
    throw 'Private LABZ1 Setup-CM config did not read back the exact test client target.'
}

[ordered]@{
    previous_client_name = $expectedClientName
    client_name = $actualClientName
} | ConvertTo-Json -Compress
