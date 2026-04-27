function Invoke-SetRegistryStep {
    <#
    .SYNOPSIS
        Load a hive from the offline target volume, write keys, unload.
        Always unloads in finally — orphaned hive handles are a real
        operator headache.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [ValidateSet('SYSTEM','SOFTWARE','DEFAULT')] [string] $Hive,
        [Parameter(Mandatory)] [string] $Target,
        [Parameter(Mandatory)] [object[]] $Keys
    )

    $hivePath = switch ($Hive) {
        'SYSTEM'   { "$Target\Windows\System32\config\SYSTEM" }
        'SOFTWARE' { "$Target\Windows\System32\config\SOFTWARE" }
        'DEFAULT'  { "$Target\Users\Default\NTUSER.DAT" }
    }

    $stagingName = "PEStaging_$Hive"
    reg.exe load "HKLM\$stagingName" $hivePath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Invoke-SetRegistryStep: reg load $hivePath failed ($LASTEXITCODE)"
    }

    try {
        foreach ($k in $Keys) {
            $regPath = "HKLM:\$stagingName\$($k.path)"
            if (-not (Test-Path $regPath)) {
                New-Item -Path $regPath -Force | Out-Null
            }
            $type = switch ($k.type) {
                'REG_SZ'        { 'String' }
                'REG_EXPAND_SZ' { 'ExpandString' }
                'REG_DWORD'     { 'DWord' }
                'REG_QWORD'     { 'QWord' }
                'REG_MULTI_SZ'  { 'MultiString' }
                'REG_BINARY'    { 'Binary' }
                default         { 'String' }
            }
            New-ItemProperty -Path $regPath -Name $k.name -Value $k.value -PropertyType $type -Force | Out-Null
        }
    } finally {
        [GC]::Collect()
        reg.exe unload "HKLM\$stagingName" | Out-Null
    }

    return [pscustomobject]@{
        LogTail = "set $($Keys.Count) keys in $Hive of $Target"
        Extra   = @{ hive = $Hive; target = $Target; key_count = $Keys.Count }
    }
}
