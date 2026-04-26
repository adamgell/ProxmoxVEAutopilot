function Invoke-PartitionStep {
    <#
    .SYNOPSIS
        Clear disk 0 and create a GPT layout for UEFI boot:
          ESP    : 260 MB, FAT32, mounted at S:
          MSR    : 16 MB, no filesystem
          Windows: rest of disk, NTFS, mounted at W:

        Returns Extra={esp, windows} so subsequent steps know where to write.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Layout
    )

    if ($Layout -ne 'uefi-standard') {
        throw "Invoke-PartitionStep: unsupported layout '$Layout' (only 'uefi-standard' is implemented)"
    }

    $disk = Get-Disk -Number 0
    Clear-Disk -Number 0 -RemoveData -RemoveOEM -Confirm:$false -ErrorAction SilentlyContinue
    Initialize-Disk -Number 0 -PartitionStyle GPT

    # ESP: 260 MB, FAT32
    $esp = New-Partition -DiskNumber 0 -Size 260MB -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    Format-Volume -Partition $esp -FileSystem FAT32 -NewFileSystemLabel 'EFI' -Confirm:$false | Out-Null

    Set-Partition -DiskNumber 0 -PartitionNumber $esp.PartitionNumber -NewDriveLetter 'S'
    $esp = Get-Partition -DiskNumber 0 -PartitionNumber $esp.PartitionNumber

    # MSR: 16 MB
    $null = New-Partition -DiskNumber 0 -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}'

    # Windows: rest of disk, NTFS, mounted at W:
    $windows = New-Partition -DiskNumber 0 -UseMaximumSize -GptType '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}' -DriveLetter 'W'
    Format-Volume -Partition $windows -FileSystem NTFS -NewFileSystemLabel 'Windows' -Confirm:$false | Out-Null

    $espLetter = "$($esp.DriveLetter):"
    $winLetter = "W:"
    Write-Host "PartitionStep: ESP=$espLetter Windows=$winLetter"
    return [pscustomobject]@{
        LogTail = "GPT partitioned: ESP=$espLetter, Windows=$winLetter"
        Extra   = @{ esp = $espLetter; windows = $winLetter }
    }
}
