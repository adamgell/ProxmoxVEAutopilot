#Requires -Version 7
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Build winpe-autopilot-<arch>-<sha>.{wim,iso}: ADK winpe.wim + packages + drivers + pwsh7 + .NET 8 + payload + (optional) OpenSSH.

.DESCRIPTION
    Reads a build-config JSON from -ConfigJson (a path) or - (stdin).
    Required fields: adkRoot, architecture, virtioIsoPath, drivers, pwsh7Zip,
                     dotnetRuntimeZip, payloadDir, orchestratorUrl, outputDir.
    Optional fields: lockPath, opensshZip, opensshAuthorizedKey.

    If `opensshZip` is set, the build also injects Win32-OpenSSH into the PE WIM
    at `\Program Files\OpenSSH\`, registers the `sshd` service for auto-start
    (LocalSystem, port 22), bakes ephemeral host keys, and stages the value of
    `opensshAuthorizedKey` into `\ProgramData\ssh\administrators_authorized_keys`.
    Once the resulting PE boots, you can SSH to it as Administrator using the
    matching private key for live debugging of the bootstrap.

    Produces in outputDir:
        winpe-autopilot-<arch>-<sha>.wim
        winpe-autopilot-<arch>-<sha>.iso
        winpe-autopilot-<arch>-<sha>.json
        winpe-autopilot-<arch>-<sha>.log
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ConfigJson
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Modules\Autopilot.Build\Autopilot.Build.psd1') -Force

# ---- Parse config ----
$rawJson = if ($ConfigJson -eq '-') { [Console]::In.ReadToEnd() } else { Get-Content -LiteralPath $ConfigJson -Raw }
$config = $rawJson | ConvertFrom-Json
foreach ($key in @('adkRoot','architecture','virtioIsoPath','drivers','pwsh7Zip','dotnetRuntimeZip','payloadDir','orchestratorUrl','outputDir')) {
    if (-not $config.PSObject.Properties.Match($key)) { throw "Build config missing required field: $key" }
}

$lockPath = if ($config.PSObject.Properties.Match('lockPath')) { $config.lockPath } else { 'C:\BuildRoot\work\.build.lock' }
$workDir  = Split-Path -Parent $lockPath
if (-not (Test-Path $workDir)) { New-Item -ItemType Directory -Path $workDir -Force | Out-Null }

$arch       = $config.architecture
$peStaging  = Join-Path $workDir "winpe-$arch-staging.wim"
$peMount    = Join-Path $workDir "mount-pe-$arch"
$mediaDir   = Join-Path $workDir "media-pe-$arch"
if (Test-Path $peMount)  { Remove-Item $peMount -Recurse -Force }
if (Test-Path $mediaDir) { Remove-Item $mediaDir -Recurse -Force }
New-Item -ItemType Directory -Path $peMount  -Force | Out-Null
New-Item -ItemType Directory -Path $mediaDir -Force | Out-Null

$lock = New-BuildLock -Path $lockPath -Owner 'Build-PeWim'
try {
    $logTempPath = Join-Path $workDir "winpe-autopilot-$arch-staging.log"
    if (Test-Path $logTempPath) { Remove-Item $logTempPath -Force }
    function Log([string]$Severity, [string]$Message) {
        Write-CmTraceLog -Path $logTempPath -Severity $Severity -Component 'Build-PeWim' -Message $Message
    }
    Log 'Info' "Build start. arch=$arch orchestratorUrl=$($config.orchestratorUrl)"

    # ---- Phase 1: Copy + mount ADK winpe.wim ----
    $adkWinPe = Join-Path $config.adkRoot "Assessment and Deployment Kit\Windows Preinstallation Environment\$arch\en-us\winpe.wim"
    if (-not (Test-Path $adkWinPe)) { throw "ADK winpe.wim not found: $adkWinPe" }
    Copy-Item -Path $adkWinPe -Destination $peStaging -Force
    Log 'Info' "Copied ADK winpe.wim → $peStaging"
    Mount-WindowsImage -Path $peMount -ImagePath $peStaging -Index 1 | Out-Null
    Log 'Info' "Mounted at $peMount"

    try {
        # ---- Phase 2: Add ADK packages ----
        $ocsRoot = Join-Path $config.adkRoot "Assessment and Deployment Kit\Windows Preinstallation Environment\$arch\WinPE_OCs"
        $packagesToAdd = @(
            'WinPE-WMI', 'WinPE-NetFX', 'WinPE-PowerShell',
            'WinPE-StorageWMI', 'WinPE-EnhancedStorage', 'WinPE-DismCmdlets',
            'WinPE-SecureStartup', 'WinPE-SecureBootCmdlets'
        )
        foreach ($pkg in $packagesToAdd) {
            $base = Join-Path $ocsRoot "$pkg.cab"
            $loc  = Join-Path $ocsRoot "en-us\${pkg}_en-us.cab"
            if (-not (Test-Path $base)) { throw "ADK package not found: $base" }
            Add-WindowsPackage -Path $peMount -PackagePath $base | Out-Null
            if (Test-Path $loc) { Add-WindowsPackage -Path $peMount -PackagePath $loc | Out-Null }
            Log 'Info' "Added package: $pkg"
        }

        # ---- Phase 3: Inject virtio drivers ----
        $virtioMount = Mount-DiskImage -ImagePath $config.virtioIsoPath -PassThru
        $virtioLetter = ($virtioMount | Get-Volume).DriveLetter
        try {
            foreach ($driver in $config.drivers) {
                $driverPath = "${virtioLetter}:\$driver\w11\$arch"
                if (-not (Test-Path $driverPath)) {
                    Log 'Warning' "Driver path missing, skipping: $driverPath"
                    continue
                }
                Add-WindowsDriver -Path $peMount -Driver $driverPath -Recurse -ForceUnsigned | Out-Null
                Log 'Info' "Injected driver: $driver"
            }
        } finally {
            try { Dismount-DiskImage -ImagePath $config.virtioIsoPath -ErrorAction SilentlyContinue | Out-Null } catch {}
        }

        # ---- Phase 4: Drop in .NET 8 runtime + pwsh 7 ----
        $dotnetTarget = Join-Path $peMount 'Program Files\dotnet'
        if (-not (Test-Path $dotnetTarget)) { New-Item -ItemType Directory -Path $dotnetTarget -Force | Out-Null }
        Expand-Archive -Path $config.dotnetRuntimeZip -DestinationPath $dotnetTarget -Force
        Log 'Info' "Extracted .NET 8 runtime → $dotnetTarget"

        $pwshTarget = Join-Path $peMount 'Program Files\PowerShell\7'
        if (-not (Test-Path $pwshTarget)) { New-Item -ItemType Directory -Path $pwshTarget -Force | Out-Null }
        Expand-Archive -Path $config.pwsh7Zip -DestinationPath $pwshTarget -Force
        Log 'Info' "Extracted pwsh 7 → $pwshTarget"

        # Discover dotnet version (used in registry below)
        $dotnetVersion = Get-ChildItem -Path (Join-Path $dotnetTarget 'shared\Microsoft.NETCore.App') -Directory |
                         Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty Name
        if (-not $dotnetVersion) { throw "Could not discover .NET 8 version from extracted runtime." }
        Log 'Info' ".NET runtime version discovered: $dotnetVersion"

        # ---- Phase 4b: OpenSSH server (optional; for in-PE remote debugging) ----
        $includeSsh = $false
        if ($config.PSObject.Properties.Match('opensshZip')) {
            if (-not $config.PSObject.Properties.Match('opensshAuthorizedKey')) {
                throw "opensshZip provided but opensshAuthorizedKey missing — refusing to ship a PE with SSH server but no authorized key."
            }
            if (-not (Test-Path $config.opensshZip)) {
                throw "opensshZip path not found: $($config.opensshZip)"
            }
            $includeSsh = $true

            $sshTarget = Join-Path $peMount 'Program Files\OpenSSH'
            New-Item -ItemType Directory -Path $sshTarget -Force | Out-Null

            # Microsoft's Win32-OpenSSH zips put binaries in a single subdir like 'OpenSSH-Win64'.
            # Extract to a temp dir and flatten that single subdir into $sshTarget.
            $sshExtractTmp = Join-Path $workDir "ssh-extract-$arch"
            if (Test-Path $sshExtractTmp) { Remove-Item $sshExtractTmp -Recurse -Force }
            New-Item -ItemType Directory -Path $sshExtractTmp -Force | Out-Null
            Expand-Archive -Path $config.opensshZip -DestinationPath $sshExtractTmp -Force
            $sshExtractedRoots = @(Get-ChildItem $sshExtractTmp)
            if ($sshExtractedRoots.Count -eq 1 -and $sshExtractedRoots[0].PSIsContainer) {
                Copy-Item -Path (Join-Path $sshExtractedRoots[0].FullName '*') -Destination $sshTarget -Recurse -Force
            } else {
                Copy-Item -Path (Join-Path $sshExtractTmp '*') -Destination $sshTarget -Recurse -Force
            }
            Remove-Item $sshExtractTmp -Recurse -Force
            Log 'Info' "Extracted OpenSSH → $sshTarget"

            $sshKeygen = Join-Path $sshTarget 'ssh-keygen.exe'
            if (-not (Test-Path $sshKeygen)) { throw "ssh-keygen.exe not found at $sshKeygen — verify opensshZip is a valid Win32-OpenSSH release zip." }

            # Generate ephemeral host keys (each WIM gets its own; PE is single-use anyway).
            $hostKeyTmp = Join-Path $workDir "ssh-hostkeys-$arch"
            if (Test-Path $hostKeyTmp) { Remove-Item $hostKeyTmp -Recurse -Force }
            New-Item -ItemType Directory -Path $hostKeyTmp -Force | Out-Null
            foreach ($keyType in @('rsa','ecdsa','ed25519')) {
                $keyPath = Join-Path $hostKeyTmp "ssh_host_${keyType}_key"
                # Empty passphrase: '' is passed as a single empty arg to ssh-keygen.
                & $sshKeygen -q -t $keyType -f $keyPath -N ''
                if (-not (Test-Path $keyPath)) { throw "ssh-keygen failed to produce $keyPath" }
            }

            # Stage host keys + sshd_config + administrators_authorized_keys
            # at \ProgramData\ssh\ (= X:\ProgramData\ssh\ at PE runtime).
            $sshDataDir = Join-Path $peMount 'ProgramData\ssh'
            New-Item -ItemType Directory -Path $sshDataDir -Force | Out-Null
            Copy-Item -Path "$hostKeyTmp\*" -Destination $sshDataDir -Force
            Remove-Item $hostKeyTmp -Recurse -Force

            # Minimal sshd_config: pubkey-only, sftp enabled (so `scp` works).
            $sshdConfig = @"
HostKey __PROGRAMDATA__/ssh/ssh_host_rsa_key
HostKey __PROGRAMDATA__/ssh/ssh_host_ecdsa_key
HostKey __PROGRAMDATA__/ssh/ssh_host_ed25519_key
PubkeyAuthentication yes
PasswordAuthentication no
PermitRootLogin prohibit-password
Subsystem sftp sftp-server.exe
"@
            Set-Content -LiteralPath (Join-Path $sshDataDir 'sshd_config') -Value $sshdConfig -Encoding utf8

            # Administrators-group authorized keys (PE login lands as SYSTEM/Administrators).
            Set-Content -LiteralPath (Join-Path $sshDataDir 'administrators_authorized_keys') -Value $config.opensshAuthorizedKey -Encoding utf8
            Log 'Info' 'Staged sshd_config, host keys, and administrators_authorized_keys'
        }

        # ---- Phase 5: Strip PS 5.1 binaries (DeployR pattern) ----
        $ps51Path = Join-Path $peMount 'Windows\System32\WindowsPowerShell\v1.0'
        if (Test-Path $ps51Path) {
            cmd.exe /c "takeown /f `"$ps51Path\*.*`" >nul 2>&1"
            cmd.exe /c "icacls `"$ps51Path\*.*`" /grant everyone:f >nul 2>&1"
            Get-ChildItem -Path "$ps51Path\*.*" -File | Remove-Item -Force
            Log 'Info' "Stripped PS 5.1 binaries (kept v1.0\Modules)"
        }

        # ---- Phase 6: Offline registry edits ----
        $sysHive = Join-Path $peMount 'Windows\System32\config\SYSTEM'
        $swHive  = Join-Path $peMount 'Windows\System32\config\SOFTWARE'
        cmd.exe /c "reg.exe load HKLM\PESystem `"$sysHive`" >nul"   ; if ($LASTEXITCODE -ne 0) { throw 'reg load PESystem failed' }
        cmd.exe /c "reg.exe load HKLM\PESoftware `"$swHive`" >nul"  ; if ($LASTEXITCODE -ne 0) { throw 'reg load PESoftware failed' }
        try {
            $shortArch = if ($arch -eq 'amd64') { 'x64' } else { $arch }
            $sharedHostKey = "HKLM:\PESoftware\dotnet\Setup\InstalledVersions\$shortArch\sharedhost"
            New-Item -Path $sharedHostKey -Force | Out-Null
            New-ItemProperty -Path $sharedHostKey -Name Path -Value 'X:\Program Files\dotnet\' -PropertyType String -Force | Out-Null
            New-ItemProperty -Path $sharedHostKey -Name Version -Value $dotnetVersion -PropertyType String -Force | Out-Null

            $envKey = 'HKLM:\PESystem\ControlSet001\Control\Session Manager\Environment'
            $existingPath = (Get-Item -Path $envKey).GetValue('Path', $null, 'DoNotExpandEnvironmentNames')
            $newPath = "$existingPath;X:\Program Files\dotnet\;X:\Program Files\PowerShell\7"
            Set-ItemProperty -Path $envKey -Name Path -Value $newPath -Type ExpandString | Out-Null

            $existingPsmp = (Get-Item -Path $envKey).GetValue('PSModulePath', '', 'DoNotExpandEnvironmentNames')
            $newPsmp = "$existingPsmp;%ProgramFiles%\PowerShell\;%ProgramFiles%\PowerShell\7\;%SystemRoot%\system32\config\systemprofile\Documents\PowerShell\Modules\"
            New-ItemProperty -Path $envKey -Name PSModulePath -PropertyType ExpandString -Value $newPsmp -Force | Out-Null

            New-ItemProperty -Path $envKey -Name APPDATA      -PropertyType ExpandString -Value '%SystemRoot%\System32\Config\SystemProfile\AppData\Roaming' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name HOMEDRIVE    -PropertyType ExpandString -Value '%SystemDrive%' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name HOMEPATH     -PropertyType ExpandString -Value '%SystemRoot%\System32\Config\SystemProfile' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name LOCALAPPDATA -PropertyType ExpandString -Value '%SystemRoot%\System32\Config\SystemProfile\AppData\Local' -Force | Out-Null
            New-ItemProperty -Path $envKey -Name POWERSHELL_UPDATECHECK -PropertyType String -Value 'LTS' -Force | Out-Null

            $tcpKey = 'HKLM:\PESystem\ControlSet001\Services\Tcpip\Parameters'
            New-ItemProperty -Path $tcpKey -Name TcpTimedWaitDelay -PropertyType DWord -Value 30 -Force | Out-Null
            New-ItemProperty -Path $tcpKey -Name MaxUserPort       -PropertyType DWord -Value 65534 -Force | Out-Null

            if ($includeSsh) {
                # Auto-start sshd. Type=16 (SERVICE_WIN32_OWN_PROCESS), Start=2 (SERVICE_AUTO_START),
                # ErrorControl=1 (SERVICE_ERROR_NORMAL). LocalSystem so PE-as-SYSTEM logins work.
                $sshdSvc = 'HKLM:\PESystem\ControlSet001\Services\sshd'
                New-Item -Path $sshdSvc -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name Type         -PropertyType DWord        -Value 16 -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name Start        -PropertyType DWord        -Value 2  -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name ErrorControl -PropertyType DWord        -Value 1  -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name ImagePath    -PropertyType ExpandString -Value '"X:\Program Files\OpenSSH\sshd.exe"' -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name ObjectName   -PropertyType String       -Value 'LocalSystem' -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name DisplayName  -PropertyType String       -Value 'OpenSSH Server' -Force | Out-Null
                New-ItemProperty -Path $sshdSvc -Name Description  -PropertyType String       -Value 'PE-side OpenSSH for live debugging.' -Force | Out-Null
                Log 'Info' 'Registered sshd service (auto-start) in offline SYSTEM hive'
            }

            Log 'Info' "Applied offline registry edits"
        } finally {
            [GC]::Collect()
            cmd.exe /c "reg.exe unload HKLM\PESoftware >nul"  | Out-Null
            cmd.exe /c "reg.exe unload HKLM\PESystem >nul"    | Out-Null
        }

        # ---- Phase 7: Stage payload ----
        $payloadTarget = Join-Path $peMount 'autopilot'
        if (-not (Test-Path $payloadTarget)) { New-Item -ItemType Directory -Path $payloadTarget -Force | Out-Null }
        Copy-Item -Path (Join-Path $config.payloadDir '*') -Destination $payloadTarget -Recurse -Force

        # Render Bootstrap.json (substitute orchestratorUrl)
        $bootstrapJsonPath = Join-Path $payloadTarget 'Bootstrap.json'
        $rendered = (Get-Content -LiteralPath $bootstrapJsonPath -Raw) -replace '__ORCHESTRATOR_URL__', $config.orchestratorUrl
        Set-Content -LiteralPath $bootstrapJsonPath -Value $rendered -Encoding utf8

        # Render unattend.xml (substitute architecture)
        $unattendTpl = Join-Path $payloadTarget 'unattend.xml.template'
        $unattendOut = Join-Path $peMount 'unattend.xml'
        $unattendRendered = (Get-Content -LiteralPath $unattendTpl -Raw) -replace '__ARCHITECTURE__', $arch
        Set-Content -LiteralPath $unattendOut -Value $unattendRendered -Encoding utf8
        Remove-Item -Path $unattendTpl -Force  # template stays out of the WIM

        # winpeshl.ini → System32
        Copy-Item -Path (Join-Path $payloadTarget 'winpeshl.ini') -Destination (Join-Path $peMount 'Windows\System32\winpeshl.ini') -Force
        Remove-Item -Path (Join-Path $payloadTarget 'winpeshl.ini') -Force  # not needed in payload tree post-build

        Log 'Info' "Staged payload at X:\autopilot and rendered unattend.xml"

        # ---- Phase 8: Dismount + commit ----
        Dismount-WindowsImage -Path $peMount -Save | Out-Null
        Log 'Info' "Dismounted (committed)"

        # ---- Phase 9: Export to compact ----
        $exportTempPath = Join-Path $workDir "winpe-autopilot-$arch-export.wim"
        if (Test-Path $exportTempPath) { Remove-Item $exportTempPath -Force }
        Export-WindowsImage -SourceImagePath $peStaging -SourceIndex 1 -DestinationImagePath $exportTempPath | Out-Null
        Log 'Info' "Exported to $exportTempPath"

        # ---- Phase 10: Hash + final WIM ----
        $sha   = Get-FileSha256 -Path $exportTempPath
        $size  = (Get-Item $exportTempPath).Length
        $finalWim  = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.wim"
        $finalIso  = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.iso"
        $finalJson = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.json"
        $finalLog  = Join-Path $config.outputDir "winpe-autopilot-$arch-$sha.log"
        if (-not (Test-Path $config.outputDir)) { New-Item -ItemType Directory -Path $config.outputDir -Force | Out-Null }
        Move-Item -Path $exportTempPath -Destination $finalWim -Force

        # ---- Phase 11: Build ISO with efisys_noprompt.bin ----
        $oscdimgDir = Join-Path $config.adkRoot "Assessment and Deployment Kit\Deployment Tools\$arch\Oscdimg"
        $oscdimg    = Join-Path $oscdimgDir 'oscdimg.exe'
        $efiNoPrompt = Join-Path $oscdimgDir 'efisys_noprompt.bin'
        if (-not (Test-Path $oscdimg))    { throw "oscdimg not found: $oscdimg" }
        if (-not (Test-Path $efiNoPrompt)){ throw "efisys_noprompt.bin not found: $efiNoPrompt" }

        # Assemble media tree: copy ADK media template (boot files), drop our WIM at \Sources\boot.wim
        $adkMedia = Join-Path $config.adkRoot "Assessment and Deployment Kit\Windows Preinstallation Environment\$arch\Media"
        if (-not (Test-Path $adkMedia)) { throw "ADK Media template not found: $adkMedia" }
        Copy-Item -Path (Join-Path $adkMedia '*') -Destination $mediaDir -Recurse -Force
        $sourcesDir = Join-Path $mediaDir 'Sources'
        if (-not (Test-Path $sourcesDir)) { New-Item -ItemType Directory -Path $sourcesDir -Force | Out-Null }
        Copy-Item -Path $finalWim -Destination (Join-Path $sourcesDir 'boot.wim') -Force

        # ARM64 is UEFI-only; -bootdata:1#pEF... = single (UEFI) boot sector with efisys_noprompt
        $oscdimgArgs = @(
            '-m','-o','-u2','-udfver102',
            "-bootdata:1#pEF,e,b$efiNoPrompt",
            $mediaDir,
            $finalIso
        )
        & $oscdimg @oscdimgArgs
        if ($LASTEXITCODE -ne 0) { throw "oscdimg failed with exit code $LASTEXITCODE" }
        Log 'Info' "ISO built: $finalIso"

        # ---- Phase 12: Sidecar + log ----
        $sidecar = @{
            kind             = 'pe-wim'
            sha256           = $sha
            size             = $size
            architecture     = $arch
            adkRoot          = $config.adkRoot
            orchestratorUrl  = $config.orchestratorUrl
            sourceVirtioIso  = @{
                path   = $config.virtioIsoPath
                sha256 = (Get-FileSha256 -Path $config.virtioIsoPath)
            }
            driversInjected   = $config.drivers
            packagesAdded     = $packagesToAdd
            dotnetVersion     = $dotnetVersion
            pwsh7ZipSha       = (Get-FileSha256 -Path $config.pwsh7Zip)
            payloadDirSnapshot= (Get-ChildItem -Path $config.payloadDir -Recurse -File | ForEach-Object { $_.FullName.Substring($config.payloadDir.Length).TrimStart('\','/') })
            buildHost         = $env:COMPUTERNAME
            buildTimestamp    = (Get-Date).ToUniversalTime().ToString('o')
            builderScript     = 'Build-PeWim.ps1'
            isoPath           = $finalIso
            isoSha256         = (Get-FileSha256 -Path $finalIso)
            isoSize           = (Get-Item $finalIso).Length
            opensshIncluded   = $includeSsh
            opensshZipSha     = if ($includeSsh) { (Get-FileSha256 -Path $config.opensshZip) } else { $null }
        }
        Write-ArtifactSidecar -Path $finalJson -Properties $sidecar
        Log 'Info' "Wrote sidecar $finalJson"
        Move-Item -Path $logTempPath -Destination $finalLog -Force

        Write-Host "BUILD OK"
        Write-Host "WIM:     $finalWim"
        Write-Host "ISO:     $finalIso"
        Write-Host "Sidecar: $finalJson"
        Write-Host "Log:     $finalLog"
        Write-Host "sha256:  $sha"
        Write-Host "size:    $size"
    } finally {
        try { Dismount-WindowsImage -Path $peMount -Discard -ErrorAction SilentlyContinue | Out-Null } catch {}
        if (Test-Path $peStaging) { Remove-Item $peStaging -Force -ErrorAction SilentlyContinue }
        if (Test-Path $peMount)   { Remove-Item $peMount   -Recurse -Force -ErrorAction SilentlyContinue }
        if (Test-Path $mediaDir)  { Remove-Item $mediaDir  -Recurse -Force -ErrorAction SilentlyContinue }
    }
} finally {
    $lock.Release()
}
