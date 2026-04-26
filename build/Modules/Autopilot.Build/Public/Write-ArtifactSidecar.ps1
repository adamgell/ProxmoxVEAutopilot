function Write-ArtifactSidecar {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [hashtable] $Properties
    )
    $required = @('kind', 'sha256', 'size')
    foreach ($key in $required) {
        if (-not $Properties.ContainsKey($key)) {
            throw "Sidecar property '$key' is required."
        }
    }
    $json = $Properties | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $Path -Value $json -Encoding utf8
}
