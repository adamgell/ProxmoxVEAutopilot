function New-ProxmoxDevice {
    <#
    .SYNOPSIS
        Creates a Proxmox QEMU VM with UEFI, Q35, VirtIO, vTPM, guest agent, and optional SMBIOS

    .DESCRIPTION
        Creates a Windows-ready VM on Proxmox using cv4pve cmdlets. Configures OVMF/UEFI
        firmware, Q35 machine type, VirtIO storage and network, EFI disk, vTPM, and
        enables the QEMU guest agent. Optionally sets SMBIOS type 1 and type 2 fields
        to emulate real OEM hardware for Autopilot testing.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.

    .PARAMETER Node
        Proxmox node to create the VM on.

    .PARAMETER Vmid
        VM ID to assign. Use Get-NextProxmoxVmid to get an available ID.

    .PARAMETER VMName
        Name for the VM.

    .PARAMETER Cores
        Number of CPU cores.

    .PARAMETER MemoryMB
        Memory in MiB.

    .PARAMETER Storage
        Storage target for VM disks.

    .PARAMETER DiskSizeGB
        OS disk size in GB.

    .PARAMETER IsoPath
        Path to Windows ISO on Proxmox storage (e.g. "local:iso/Win11_24H2.iso").

    .PARAMETER VirtIOIsoPath
        Path to VirtIO drivers ISO (e.g. "local:iso/virtio-win.iso").

    .PARAMETER Bridge
        Network bridge name.

    .PARAMETER VlanTag
        Optional VLAN tag for the network adapter.

    .PARAMETER Manufacturer
        SMBIOS type 1 manufacturer field (e.g. "Lenovo").

    .PARAMETER Product
        SMBIOS type 1 product field (e.g. "ThinkPad T14 Gen 4").

    .PARAMETER Family
        SMBIOS type 1 family field (e.g. "ThinkPad").

    .PARAMETER SerialNumber
        SMBIOS type 1 serial number field.

    .PARAMETER SKU
        SMBIOS type 1 SKU field (e.g. "21HES06600").

    .PARAMETER Smbios1
        Raw SMBIOS type 1 string for advanced users. If provided, overrides individual fields.

    .PARAMETER ChassisType
        SMBIOS type 2 chassis type (e.g. 3=Desktop, 10=Notebook).

    .PARAMETER StartAfterCreate
        Start the VM immediately after creation.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [parameter(Mandatory = $false)]
        $PveTicket,

        [parameter(Mandatory = $true)]
        [string]$Node,

        [parameter(Mandatory = $true)]
        [int]$Vmid,

        [parameter(Mandatory = $true)]
        [ValidatePattern('^[a-zA-Z0-9-]+$')]
        [string]$VMName,

        [parameter(Mandatory = $false)]
        [int]$Cores = 2,

        [parameter(Mandatory = $false)]
        [int]$MemoryMB = 4096,

        [parameter(Mandatory = $true)]
        [string]$Storage,

        [parameter(Mandatory = $false)]
        [int]$DiskSizeGB = 64,

        [parameter(Mandatory = $true)]
        [string]$IsoPath,

        [parameter(Mandatory = $false)]
        [string]$VirtIOIsoPath,

        [parameter(Mandatory = $true)]
        [string]$Bridge,

        [parameter(Mandatory = $false)]
        [int]$VlanTag,

        [parameter(Mandatory = $false)]
        [string]$Manufacturer,

        [parameter(Mandatory = $false)]
        [string]$Product,

        [parameter(Mandatory = $false)]
        [string]$Family,

        [parameter(Mandatory = $false)]
        [string]$SerialNumber,

        [parameter(Mandatory = $false)]
        [string]$SKU,

        [parameter(Mandatory = $false)]
        [string]$Smbios1,

        [parameter(Mandatory = $false)]
        [int]$ChassisType,

        [parameter(Mandatory = $false)]
        [switch]$StartAfterCreate
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    if (-not $PSCmdlet.ShouldProcess("$VMName (VMID: $Vmid) on $Node", "Create Proxmox VM")) {
        return
    }

    $vmIdentity = New-ProxmoxVmIdentity -Vmid $Vmid
    $Smbios1 = Merge-ProxmoxSmbios1 `
        -Smbios1 $Smbios1 `
        -Manufacturer $Manufacturer `
        -Product $Product `
        -Family $Family `
        -SerialNumber $SerialNumber `
        -SKU $SKU `
        -Uuid $vmIdentity.Uuid

    $osDiskConfig = Set-ProxmoxDiskSerial `
        -DiskConfig "$Storage`:$DiskSizeGB,iothread=1,ssd=1,discard=on" `
        -Serial $vmIdentity.DiskSerial

    # Build network string
    $netConfig = "virtio,bridge=$Bridge,firewall=0"
    if ($VlanTag) {
        $netConfig += ",tag=$VlanTag"
    }

    # Build the VM creation parameters
    $createParams = @{
        PveTicket  = $PveTicket
        Node       = $Node
        Vmid       = $Vmid
        Name       = $VMName
        Bios       = 'ovmf'
        Machine    = 'q35'
        Ostype     = 'win11'
        Cpu        = 'host'
        Cores      = $Cores
        Memory     = $MemoryMB
        Balloon    = 0
        Agent      = 'enabled=1,type=virtio'
        Tablet     = $true
        Scsihw     = 'virtio-scsi-single'
        Efidisk0   = "$Storage`:0,efitype=4m,pre-enrolled-keys=1"
        Tpmstate0  = "$Storage`:1,version=v2.0"
        ScsiN      = @{ 0 = $osDiskConfig }
        NetN       = @{ 0 = $netConfig }
        IdeN       = @{ 2 = "$IsoPath,media=cdrom" }
        Boot       = 'order=ide2;scsi0'
    }

    # Attach VirtIO drivers ISO as ide3 if specified
    if ($VirtIOIsoPath) {
        $createParams['IdeN'][3] = "$VirtIOIsoPath,media=cdrom"
    }

    # Add SMBIOS1 if we have it
    if ($Smbios1) {
        $createParams['Smbios1'] = $Smbios1
    }

    # Dump every param with type for debugging
    Write-Host "  --- createParams dump ---" -ForegroundColor DarkGray
    foreach ($k in $createParams.Keys | Sort-Object) {
        $v = $createParams[$k]
        $t = if ($null -eq $v) { 'null' } else { $v.GetType().Name }
        if ($v -is [hashtable]) {
            $sub = ($v.GetEnumerator() | ForEach-Object { "  $($_.Key)[$($_.Value.GetType().Name)]=$($_.Value)" }) -join "`n"
            Write-Host "  $k [$t]:`n$sub" -ForegroundColor DarkGray
        } elseif ($k -eq 'PveTicket') {
            Write-Host "  $k [$t] Host=$($v.HostName):$($v.Port)" -ForegroundColor DarkGray
        } else {
            Write-Host "  $k [$t] = $v" -ForegroundColor DarkGray
        }
    }
    Write-Host "  --- end dump ---" -ForegroundColor DarkGray

    Write-Host "  Creating VM '$VMName' (VMID: $Vmid) on node '$Node'.. " -ForegroundColor Cyan -NoNewline
    try {
        $createResult = New-PveNodesQemu @createParams

        if (-not $createResult.IsSuccessStatusCode) {
            throw "VM creation failed ($($createResult.StatusCode)): $($createResult.ReasonPhrase)"
        }

        # Wait for the creation task to finish
        $upid = $createResult.Response.data
        if ($upid) {
            $taskDone = Wait-PveTaskIsFinish -PveTicket $PveTicket -Upid $upid -Wait 1000 -Timeout 120000
            if (-not $taskDone) {
                Write-Warning "VM creation task did not complete within timeout. Check Proxmox task log for VMID $Vmid."
            }
        }

        Write-Host $script:tick -ForegroundColor Green
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        throw "Failed to create VM '$VMName' (VMID: $Vmid): $_"
    }

    # Proxmox 9 rejects smbios2 as an unknown setting, so skip chassis-type writes
    # rather than leaving the VM config in a broken state.
    if ($ChassisType) {
        Write-Warning "Skipping chassis type $ChassisType for VMID $Vmid because this Proxmox host does not support smbios2."
    }

    # Start the VM if requested
    if ($StartAfterCreate) {
        Write-Host "  Starting VM '$VMName'.. " -ForegroundColor Cyan -NoNewline
        try {
            $startResult = New-PveNodesQemuStatusStart -PveTicket $PveTicket -Node $Node -Vmid $Vmid
            if (-not $startResult.IsSuccessStatusCode) {
                throw "VM start failed: $($startResult.ReasonPhrase)"
            }
            Write-Host $script:tick -ForegroundColor Green
        }
        catch {
            Write-Host "X" -ForegroundColor Red
            throw "Failed to start VM '$VMName' (VMID: $Vmid): $_"
        }
    }

    # Return a result object
    return [PSCustomObject]@{
        Vmid         = $Vmid
        Name         = $VMName
        Node         = $Node
        Status       = if ($StartAfterCreate) { 'Started' } else { 'Created' }
        SerialNumber = $SerialNumber
        Uuid         = $vmIdentity.Uuid
        DiskSerial   = $vmIdentity.DiskSerial
        OemProfile   = if ($Manufacturer) { "$Manufacturer $Product" } else { $null }
    }
}
