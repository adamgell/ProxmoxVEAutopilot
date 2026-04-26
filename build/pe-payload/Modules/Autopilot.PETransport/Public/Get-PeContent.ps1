function Get-PeContent {
    <#
    .SYNOPSIS
        Fetch /winpe/content/<sha256> to OutPath, then verify the file's
        sha256 matches the expected value. On mismatch, deletes the file
        and throws.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $OrchestratorUrl,
        [Parameter(Mandatory)] [string] $Sha256,
        [Parameter(Mandatory)] [string] $OutPath
    )

    $url = "$($OrchestratorUrl.TrimEnd('/'))/winpe/content/$Sha256"
    $outDir = Split-Path -Parent $OutPath
    if ($outDir -and -not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    Invoke-WebRequest -Uri $url -OutFile $OutPath -UseBasicParsing -ErrorAction Stop

    $actual = (Get-FileHash -LiteralPath $OutPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -LiteralPath $OutPath -Force -ErrorAction SilentlyContinue
        throw "Get-PeContent: sha256 mismatch. expected=$Sha256 actual=$actual url=$url"
    }
}
