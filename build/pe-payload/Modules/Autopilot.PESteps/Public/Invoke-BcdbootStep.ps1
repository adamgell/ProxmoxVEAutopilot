function Invoke-BcdbootStep {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Windows,
        [Parameter(Mandatory)] [string] $Esp
    )
    $winPath = "${Windows}\Windows"
    Write-Host "BcdbootStep: bcdboot $winPath /s $Esp /f UEFI"
    bcdboot $winPath /s $Esp /f UEFI | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "BcdbootStep: bcdboot exited with code $LASTEXITCODE"
    }

    # QEMU AAVMF (ARM64 UEFI) doesn't always honour NVRAM boot entries;
    # ensure the EFI fallback path exists so the firmware can discover it.
    $fallbackDir = "${Esp}\EFI\BOOT"
    if (-not (Test-Path $fallbackDir)) { New-Item -ItemType Directory -Path $fallbackDir -Force | Out-Null }
    $src = "${Esp}\EFI\Microsoft\Boot\bootmgfw.efi"
    $dst = "${fallbackDir}\BOOTAA64.EFI"
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination $dst -Force
        Write-Host "BcdbootStep: copied fallback $dst"
    } else {
        Write-Host "BcdbootStep: WARNING - $src not found, fallback not created"
    }

    return [pscustomobject]@{ LogTail = "bcdboot $winPath /s $Esp /f UEFI ok + fallback"; Extra = @{ windows = $Windows; esp = $Esp } }
}
