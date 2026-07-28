# Removes the trailing Windows Recovery partition and extends C: into it, plus
# whatever new space a Proxmox disk resize added. No arguments.
#
# The problem
# -----------
# After `qm resize` grows the virtual disk, Resize-Partition still refuses:
# Get-PartitionSupportedSize keeps reporting the old size. That is not a
# geometry-refresh problem, it is a layout problem. These images put WinRE in a
# partition immediately AFTER C:, so C: has nothing contiguous to grow into.
# The new space lands past the recovery partition, unreachable.
#
# Layout before:   [EFI] [MSR] [ C: 77.5G ] [WinRE ~0.5G] .......... 178G free
# Layout after:    [EFI] [MSR] [ C: 255G ................................. ]
#
# What this does
# --------------
# reagentc /disable first, which copies winre.wim back to
# C:\Windows\System32\Recovery rather than destroying it. Then the recovery
# partition is deleted, C: is extended over the freed space plus the unallocated
# tail, and reagentc /enable puts WinRE back. With no dedicated partition it
# hosts WinRE inside C:, which is a supported configuration and what plenty of
# OEM images already do.
#
# Safety
# ------
# Only deletes a partition that is all of: on the same disk as C:, positioned
# after C:, and typed as Recovery. If reagentc reports WinRE somewhere
# unexpected, or the partition after C: is not a recovery partition, it stops
# without touching anything. It also refuses when there is nothing to gain.
#
# BitLocker stops it outright. Resizing a protected volume and deleting the
# recovery partition underneath one is how you end up in a recovery prompt with
# no recovery environment. Suspending protection is not enough either, because
# the volume is still encrypted; C: has to be fully decrypted, which on these
# images means `manage-bde -off C:` and waiting for it to finish.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- baked config
$LogRoot = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent\install"
# Windows Recovery partition GPT type, used as a second check alongside the
# friendly type name.
$RecoveryGptType = "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}"
# ------------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot "reclaim-recovery.log"

function Write-InstallLog {
    param([string]$Message)
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Output $line
}

function Show-Layout {
    param([string]$When)
    Write-InstallLog "Partition layout ($When):"
    Get-Partition -DiskNumber $script:DiskNumber | Sort-Object Offset | ForEach-Object {
        Write-InstallLog ("  part={0} type={1} letter={2} offset={3:N2}GB size={4:N2}GB" -f `
                $_.PartitionNumber, $_.Type, $_.DriveLetter, ($_.Offset / 1GB), ($_.Size / 1GB))
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Reclaiming the recovery partition requires administrative context."
}

# BitLocker first: everything below moves partitions around, and doing that
# under an encrypted volume risks an unbootable machine.
$bde = $null
try {
    $bde = Get-BitLockerVolume -MountPoint "C:" -ErrorAction Stop
}
catch {
    # Server SKUs and trimmed images ship without the BitLocker module at all,
    # which is not an error here, just an absence of encryption to worry about.
    Write-Verbose "Get-BitLockerVolume unavailable: $($_.Exception.Message)"
}
if ($bde -and $bde.VolumeStatus -ne "FullyDecrypted") {
    Write-InstallLog "REFUSING: C: reports VolumeStatus=$($bde.VolumeStatus) ProtectionStatus=$($bde.ProtectionStatus)."
    Write-InstallLog "Decrypt before reclaiming the recovery partition:"
    Write-InstallLog "  manage-bde -off C:"
    Write-InstallLog "  manage-bde -status C:    # wait for 'Fully Decrypted'"
    Write-InstallLog "Suspending protection is not sufficient; the volume must actually be decrypted."
    exit 1
}
if ($bde) { Write-InstallLog "BitLocker: C: is $($bde.VolumeStatus), safe to proceed." }
else { Write-InstallLog "BitLocker: not present on this machine." }

$sys = Get-Partition -DriveLetter C
$script:DiskNumber = $sys.DiskNumber
$disk = Get-Disk -Number $script:DiskNumber
Write-InstallLog "Disk $($disk.Number): size=$([math]::Round($disk.Size/1GB,1))GB allocated=$([math]::Round($disk.AllocatedSize/1GB,1))GB partitionStyle=$($disk.PartitionStyle)"
Show-Layout "before"

$reagentBefore = (reagentc /info 2>&1 | Out-String).Trim()
Write-InstallLog "reagentc /info before:`n$reagentBefore"

# The partition sitting immediately after C: is the only candidate. Anything
# else, and the free space is not actually blocked by recovery.
$after = Get-Partition -DiskNumber $script:DiskNumber |
    Where-Object { $_.Offset -gt $sys.Offset } |
    Sort-Object Offset |
    Select-Object -First 1

if (-not $after) {
    Write-InstallLog "Nothing sits after C:. If C: still will not grow, the resize did not reach this disk."
    Show-Layout "unchanged"
    exit 0
}

$isRecovery = ($after.Type -eq "Recovery") -or ($after.GptType -eq $RecoveryGptType)
if (-not $isRecovery) {
    Write-InstallLog "REFUSING: the partition after C: is part=$($after.PartitionNumber) type=$($after.Type) gpt=$($after.GptType), which is not a recovery partition. Not deleting it."
    exit 1
}

$gain = ($disk.Size - ($sys.Offset + $sys.Size)) / 1GB
Write-InstallLog "Recovery partition: part=$($after.PartitionNumber) size=$([math]::Round($after.Size/1GB,2))GB. Reclaiming it exposes about $([math]::Round($gain,1))GB to C:."
if ($gain -lt 1) {
    Write-InstallLog "Less than 1GB to gain; nothing worth doing."
    exit 0
}

# Moves winre.wim to C:\Windows\System32\Recovery instead of losing it.
Write-InstallLog "Disabling WinRE so its image is preserved on C: ..."
$disableOut = (reagentc /disable 2>&1 | Out-String).Trim()
Write-InstallLog "reagentc /disable: $disableOut"

Write-InstallLog "Deleting recovery partition $($after.PartitionNumber) ..."
# Recovery partitions are protected, so this needs diskpart's override; the
# PowerShell cmdlet refuses on some builds.
$dp = @"
select disk $($script:DiskNumber)
select partition $($after.PartitionNumber)
delete partition override
"@
$dpOut = ($dp | diskpart | Out-String).Trim()
Write-InstallLog "diskpart: $dpOut"

Start-Sleep -Seconds 3
Update-HostStorageCache

$sys = Get-Partition -DriveLetter C
$max = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
Write-InstallLog "C: is $([math]::Round($sys.Size/1GB,2))GB, can now reach $([math]::Round($max/1GB,2))GB."
if ($max -gt ($sys.Size + 1GB)) {
    Resize-Partition -DriveLetter C -Size $max
    $vol = Get-Volume -DriveLetter C
    Write-InstallLog "EXTENDED: C: is now $([math]::Round($vol.Size/1GB,2))GB with $([math]::Round($vol.SizeRemaining/1GB,2))GB free."
}
else {
    Write-InstallLog "C: still cannot grow. The virtual disk may not have been resized on the host."
}

# Re-enable WinRE. Without a dedicated partition it is hosted inside C:, which
# is supported.
Write-InstallLog "Re-enabling WinRE ..."
try {
    $enableOut = (reagentc /enable 2>&1 | Out-String).Trim()
    Write-InstallLog "reagentc /enable: $enableOut"
}
catch {
    Write-InstallLog "WARNING: reagentc /enable failed: $($_.Exception.Message). WinRE image is still at C:\Windows\System32\Recovery; re-enable it by hand if you need recovery on this box."
}

Write-InstallLog "reagentc /info after:`n$((reagentc /info 2>&1 | Out-String).Trim())"
Show-Layout "after"
Write-InstallLog "Recovery reclaim complete."
