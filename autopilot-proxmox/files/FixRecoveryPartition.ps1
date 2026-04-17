# Relocate the WinRE recovery partition to the end of the disk.
#
# Cloned VMs inherit the template's partition layout. When Proxmox expands
# scsi0 after clone, the new free space lands AFTER the recovery partition,
# stranding it in the middle and blocking future C: expansion.
#
# Runs via SetupComplete.cmd as SYSTEM during first boot, before user sign-in.
# Idempotent: no-op if recovery is already at the end of the disk.

$ErrorActionPreference = 'Stop'
$logDir = "$env:WINDIR\Setup\Scripts"
$log    = Join-Path $logDir 'FixRecoveryPartition.log'

Start-Transcript -Path $log -Append | Out-Null

try {
    $RECOVERY_GPT_TYPE = '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}'
    $TOLERANCE_BYTES   = 1MB

    $disk = Get-Disk -Number 0
    if ($disk.PartitionStyle -ne 'GPT') {
        Write-Host "Disk 0 is not GPT ($($disk.PartitionStyle)); skipping."
        return
    }

    $partitions = Get-Partition -DiskNumber 0 | Sort-Object Offset
    $recovery   = $partitions | Where-Object { $_.GptType -eq $RECOVERY_GPT_TYPE } | Select-Object -Last 1
    $windows    = $partitions | Where-Object { $_.DriveLetter -eq 'C' }

    if (-not $recovery) { Write-Host "No recovery partition on disk 0; skipping."; return }
    if (-not $windows)  { throw "Cannot locate Windows (C:) partition on disk 0." }

    $recoveryEnd = [int64]$recovery.Offset + [int64]$recovery.Size
    $tail        = [int64]$disk.Size - $recoveryEnd

    if ($tail -lt $TOLERANCE_BYTES) {
        Write-Host "Recovery already at end of disk (tail=$tail bytes); nothing to do."
        return
    }

    Write-Host "Recovery is not at end of disk. tail=$tail bytes. Rebuilding."

    # Preserve the existing recovery size so we don't shrink it.
    $recoverySize = [int64]$recovery.Size

    # reagentc /disable copies winre.wim to C:\Windows\System32\Recovery\ so
    # we can delete the recovery partition without losing the image.
    Write-Host "reagentc /disable"
    $null = & reagentc.exe /disable
    if ($LASTEXITCODE -ne 0) { throw "reagentc /disable failed with exit $LASTEXITCODE" }

    Write-Host "Deleting old recovery partition (#$($recovery.PartitionNumber))"
    Remove-Partition -DiskNumber 0 -PartitionNumber $recovery.PartitionNumber -Confirm:$false

    # Extend C: to the max, then shrink back just enough to leave room for
    # the new recovery partition at the end.
    $supported = Get-PartitionSupportedSize -DriveLetter 'C'
    $targetC   = [int64]$supported.SizeMax - $recoverySize
    if ($targetC -le $windows.Size) {
        throw "Refusing to shrink C: (current=$($windows.Size), target=$targetC)."
    }
    Write-Host "Resizing C: to $targetC bytes"
    Resize-Partition -DriveLetter 'C' -Size $targetC

    Write-Host "Creating new recovery partition at end of disk"
    $newRecovery = New-Partition -DiskNumber 0 -UseMaximumSize -GptType $RECOVERY_GPT_TYPE
    Format-Volume -Partition $newRecovery -FileSystem NTFS -NewFileSystemLabel 'Recovery' -Confirm:$false | Out-Null

    # Set required GPT attributes: hidden (0x4000...) + required-partition
    # (0x0000...0001) + no-drive-letter (0x8000...). 0x8000000000000001 matches
    # what Windows Setup normally assigns to the WinRE partition.
    $diskpartScript = @"
select disk 0
select partition $($newRecovery.PartitionNumber)
gpt attributes=0x8000000000000001
exit
"@
    $dpFile = New-TemporaryFile
    Set-Content -Path $dpFile -Value $diskpartScript -Encoding ASCII
    $null = & diskpart.exe /s $dpFile
    Remove-Item $dpFile -Force

    Write-Host "reagentc /enable (repopulates winre.wim into new partition)"
    $null = & reagentc.exe /enable
    if ($LASTEXITCODE -ne 0) { throw "reagentc /enable failed with exit $LASTEXITCODE" }

    & reagentc.exe /info | Write-Host
    Write-Host "Recovery partition successfully relocated to end of disk."
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    Write-Host $_.ScriptStackTrace
    # Non-zero exit so SetupComplete.cmd log flags it, but don't block OOBE.
    exit 1
}
finally {
    Stop-Transcript | Out-Null
}
