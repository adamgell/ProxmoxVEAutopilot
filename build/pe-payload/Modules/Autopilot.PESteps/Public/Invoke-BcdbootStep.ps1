function Invoke-BcdbootStep {
    <#
    .SYNOPSIS
        Make the target volume bootable. Equivalent to:
            bcdboot W:\Windows /s S: /f UEFI
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Windows,
        [Parameter(Mandatory)] [string] $Esp
    )
    # Build the Windows path manually with a backslash: Join-Path throws on macOS
    # for Windows-style drive letters, and [System.IO.Path]::Combine uses the
    # platform separator (forward-slash on macOS).  Explicit string concatenation
    # is the only cross-platform approach that always produces 'W:\Windows'.
    $winPath = "${Windows}\Windows"
    Write-Host "BcdbootStep: bcdboot $winPath /s $Esp /f UEFI"
    bcdboot $winPath /s $Esp /f UEFI | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "BcdbootStep: bcdboot exited with code $LASTEXITCODE"
    }
    return [pscustomobject]@{ LogTail = "bcdboot $winPath /s $Esp /f UEFI ok"; Extra = @{ windows = $Windows; esp = $Esp } }
}
