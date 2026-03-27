function New-ProxmoxCloneDevice {
    <#
    .SYNOPSIS
        Clones a Proxmox VM or template and applies APHVTools-specific configuration

    .DESCRIPTION
        Uses the cv4pve clone API to create a new VM from an existing Proxmox source
        VM/template, then applies APHVTools naming, compute, networking, and SMBIOS
        settings before optionally starting the clone.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param (
        [parameter(Mandatory = $false)]
        $PveTicket,

        [parameter(Mandatory = $true)]
        [string]$Node,

        [parameter(Mandatory = $true)]
        [int]$TemplateVmid,

        [parameter(Mandatory = $true)]
        [int]$NewVmid,

        [parameter(Mandatory = $true)]
        [ValidatePattern('^[a-zA-Z0-9-]+$')]
        [string]$VMName,

        [parameter(Mandatory = $false)]
        [int]$Cores = 2,

        [parameter(Mandatory = $false)]
        [int]$MemoryMB = 4096,

        [parameter(Mandatory = $false)]
        [string]$Storage,

        [parameter(Mandatory = $false)]
        [int]$DiskSizeGB,

        [parameter(Mandatory = $false)]
        [string]$Bridge,

        [parameter(Mandatory = $false)]
        [int]$VlanTag,

        [parameter(Mandatory = $false)]
        [string]$VirtIOIsoPath,

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

    if (-not $PSCmdlet.ShouldProcess("$VMName (VMID: $NewVmid) from template $TemplateVmid on $Node", "Clone Proxmox VM")) {
        return
    }

    $vmIdentity = New-ProxmoxVmIdentity -Vmid $NewVmid
    $Smbios1 = Merge-ProxmoxSmbios1 `
        -Smbios1 $Smbios1 `
        -Manufacturer $Manufacturer `
        -Product $Product `
        -Family $Family `
        -SerialNumber $SerialNumber `
        -SKU $SKU `
        -Uuid $vmIdentity.Uuid

    $cloneParams = @{
        PveTicket = $PveTicket
        Node      = $Node
        Vmid      = $TemplateVmid
        Newid     = $NewVmid
        Name      = $VMName
        Full      = $true
    }

    if ($Storage) {
        $cloneParams['Storage'] = $Storage
    }

    Write-Host "  Cloning template VM '$TemplateVmid' to '$VMName' (VMID: $NewVmid).. " -ForegroundColor Cyan -NoNewline
    try {
        $cloneResult = New-PveNodesQemuClone @cloneParams
        if (-not $cloneResult.IsSuccessStatusCode) {
            throw "VM clone failed ($($cloneResult.StatusCode)): $($cloneResult.ReasonPhrase)"
        }

        $upid = $cloneResult.Response.data
        if ($upid) {
            $taskDone = Wait-PveTaskIsFinish -PveTicket $PveTicket -Upid $upid -Wait 1000 -Timeout 120000
            if (-not $taskDone) {
                Write-Warning "VM clone task did not complete within timeout. Check Proxmox task log for VMID $NewVmid."
            }
        }

        Write-Host $script:tick -ForegroundColor Green
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        throw "Failed to clone template VM '$TemplateVmid' to '$VMName' (VMID: $NewVmid): $_"
    }

    $currentConfigResult = Invoke-PveRestApi -PveTicket $PveTicket -Resource "/nodes/$Node/qemu/$NewVmid/config" -Method Get
    if (-not $currentConfigResult.IsSuccessStatusCode) {
        throw "Failed to read cloned VM config for VMID ${NewVmid}: $($currentConfigResult.ReasonPhrase)"
    }

    $currentScsi0 = $currentConfigResult.Response.data.scsi0
    if (-not $currentScsi0) {
        throw "Cloned VM '$VMName' (VMID: $NewVmid) does not have a scsi0 disk to set a serial number on."
    }

    $configParams = @{
        name    = $VMName
        cores   = $Cores
        memory  = $MemoryMB
        cpu     = 'host'
        balloon = 0
        agent   = 'enabled=1,type=virtio'
        scsi0   = (Set-ProxmoxDiskSerial -DiskConfig $currentScsi0 -Serial $vmIdentity.DiskSerial)
    }

    if ($Bridge) {
        $netConfig = "virtio,bridge=$Bridge,firewall=0"
        if ($VlanTag) {
            $netConfig += ",tag=$VlanTag"
        }
        $configParams['net0'] = $netConfig
    }

    if ($Smbios1) {
        $configParams['smbios1'] = $Smbios1
    }

    if ($VirtIOIsoPath) {
        $configParams['ide3'] = "$VirtIOIsoPath,media=cdrom"
    }

    Write-Host "  Updating cloned VM configuration.. " -ForegroundColor Cyan -NoNewline
    try {
        $configResult = $null
        $maxConfigAttempts = 6

        for ($configAttempt = 1; $configAttempt -le $maxConfigAttempts; $configAttempt++) {
            $configResult = Invoke-PveRestApi -PveTicket $PveTicket `
                -Resource "/nodes/$Node/qemu/$NewVmid/config" `
                -Method Set `
                -Parameters $configParams

            if ($configResult.IsSuccessStatusCode) {
                break
            }

            $lockTimeout = $configResult.StatusCode -eq 500 -and $configResult.ReasonPhrase -match "can't lock file '.+lock-$NewVmid\.conf' - got timeout"
            if (-not $lockTimeout -or $configAttempt -eq $maxConfigAttempts) {
                throw "VM config update failed ($($configResult.StatusCode)): $($configResult.ReasonPhrase)"
            }

            Write-Warning "Config for VMID $NewVmid is still locked after clone. Retrying in 5 seconds (attempt $($configAttempt + 1) of $maxConfigAttempts)."
            Start-Sleep -Seconds 5
        }

        $configTask = $configResult.Response.data
        if ($configTask) {
            $taskDone = Wait-PveTaskIsFinish -PveTicket $PveTicket -Upid $configTask -Wait 1000 -Timeout 120000
            if (-not $taskDone) {
                Write-Warning "VM config task did not complete within timeout. Check Proxmox task log for VMID $NewVmid."
            }
        }

        Write-Host $script:tick -ForegroundColor Green
    }
    catch {
        Write-Host "X" -ForegroundColor Red
        throw "Failed to update cloned VM '$VMName' (VMID: $NewVmid): $_"
    }

    if ($DiskSizeGB) {
        try {
            $templateConfig = Invoke-PveRestApi -PveTicket $PveTicket -Resource "/nodes/$Node/qemu/$TemplateVmid/config" -Method Get
            $templateDiskSizeGb = $null

            if ($templateConfig.IsSuccessStatusCode -and $templateConfig.Response.data.scsi0 -match 'size=(\d+(?:\.\d+)?)G') {
                $templateDiskSizeGb = [int][Math]::Ceiling([double]$Matches[1])
            }

            if ($templateDiskSizeGb -and $DiskSizeGB -gt $templateDiskSizeGb) {
                Write-Host "  Resizing cloned disk from ${templateDiskSizeGb}GB to ${DiskSizeGB}GB.. " -ForegroundColor Cyan -NoNewline
                $resizeResult = Set-PveNodesQemuResize -PveTicket $PveTicket -Node $Node -Vmid $NewVmid -Disk 'scsi0' -Size "${DiskSizeGB}G"
                if (-not $resizeResult.IsSuccessStatusCode) {
                    throw "Disk resize failed ($($resizeResult.StatusCode)): $($resizeResult.ReasonPhrase)"
                }

                $resizeTask = $resizeResult.Response.data
                if ($resizeTask) {
                    $taskDone = Wait-PveTaskIsFinish -PveTicket $PveTicket -Upid $resizeTask -Wait 1000 -Timeout 120000
                    if (-not $taskDone) {
                        Write-Warning "Disk resize task did not complete within timeout for VMID $NewVmid."
                    }
                }

                Write-Host $script:tick -ForegroundColor Green
            }
            elseif ($templateDiskSizeGb -and $DiskSizeGB -lt $templateDiskSizeGb) {
                Write-Warning "Requested disk size ${DiskSizeGB}GB is smaller than template disk size ${templateDiskSizeGb}GB. Keeping template disk size."
            }
        }
        catch {
            Write-Warning "Failed to evaluate or resize disk for cloned VM '$VMName' (VMID: $NewVmid): $_"
        }
    }

    if ($ChassisType) {
        Write-Warning "Skipping chassis type $ChassisType for VMID $NewVmid because this Proxmox host does not support smbios2."
    }

    if ($StartAfterCreate) {
        Write-Host "  Starting VM '$VMName'.. " -ForegroundColor Cyan -NoNewline
        try {
            $startResult = New-PveNodesQemuStatusStart -PveTicket $PveTicket -Node $Node -Vmid $NewVmid
            if (-not $startResult.IsSuccessStatusCode) {
                throw "VM start failed: $($startResult.ReasonPhrase)"
            }
            Write-Host $script:tick -ForegroundColor Green
        }
        catch {
            Write-Host "X" -ForegroundColor Red
            throw "Failed to start VM '$VMName' (VMID: $NewVmid): $_"
        }
    }

    return [PSCustomObject]@{
        Vmid         = $NewVmid
        Name         = $VMName
        Node         = $Node
        Status       = if ($StartAfterCreate) { 'Started' } else { 'Cloned' }
        SerialNumber = $SerialNumber
        Uuid         = $vmIdentity.Uuid
        DiskSerial   = $vmIdentity.DiskSerial
        OemProfile   = if ($Manufacturer) { "$Manufacturer $Product" } else { $null }
        SourceVmid   = $TemplateVmid
    }
}
