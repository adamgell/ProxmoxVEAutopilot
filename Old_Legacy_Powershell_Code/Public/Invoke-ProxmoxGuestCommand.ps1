function Invoke-ProxmoxGuestCommand {
    <#
    .SYNOPSIS
        Executes a command inside a Proxmox VM via the QEMU guest agent

    .DESCRIPTION
        Wraps the exec/poll pattern: runs a command via New-PveNodesQemuAgentExec,
        polls Get-PveNodesQemuAgentExecStatus until completion, and returns stdout/stderr.

    .PARAMETER PveTicket
        Proxmox connection ticket. If not specified, uses $script:pveTicket.

    .PARAMETER Node
        Proxmox node the VM is on.

    .PARAMETER Vmid
        VM ID to execute the command in.

    .PARAMETER Command
        Command to execute as an array (program + arguments).

    .PARAMETER InputData
        Optional data to pass as STDIN to the command.

    .PARAMETER TimeoutSeconds
        Maximum time to wait for command completion. Default: 300 seconds.

    .PARAMETER PollIntervalMs
        Milliseconds between status polls. Default: 2000.
    #>
    [CmdletBinding()]
    param (
        [parameter(Mandatory = $false)]
        $PveTicket,

        [parameter(Mandatory = $true)]
        [string]$Node,

        [parameter(Mandatory = $true)]
        [int]$Vmid,

        [parameter(Mandatory = $true)]
        [array]$Command,

        [parameter(Mandatory = $false)]
        [string]$InputData,

        [parameter(Mandatory = $false)]
        [int]$TimeoutSeconds = 300,

        [parameter(Mandatory = $false)]
        [int]$PollIntervalMs = 2000
    )

    if (-not $PveTicket) {
        $PveTicket = $script:pveTicket
    }

    if (-not $PveTicket) {
        throw "No Proxmox connection. Call Connect-ProxmoxHost first."
    }

    # Execute the command
    $execParams = @{
        PveTicket = $PveTicket
        Node      = $Node
        Vmid      = $Vmid
        Command   = $Command
    }
    if ($InputData) {
        $execParams['InputData'] = $InputData
    }

    Write-Verbose "Executing command in VM $Vmid on $Node`: $($Command -join ' ')"
    $execResult = New-PveNodesQemuAgentExec @execParams

    if (-not $execResult.IsSuccessStatusCode) {
        throw "Guest agent exec failed: $($execResult.ReasonPhrase)"
    }

    $pid_ = $execResult.Response.data.pid
    if (-not $pid_) {
        throw "Guest agent exec returned no PID"
    }

    Write-Verbose "Command started with PID: $pid_"

    # Poll for completion
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $completed = $false
    $statusData = $null

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds $PollIntervalMs

        $statusResult = Get-PveNodesQemuAgentExecStatus -PveTicket $PveTicket -Node $Node -Vmid $Vmid -Pid_ $pid_

        if (-not $statusResult.IsSuccessStatusCode) {
            Write-Warning "Status poll failed: $($statusResult.ReasonPhrase)"
            continue
        }

        $statusData = $statusResult.Response.data
        if ($statusData.exited) {
            $completed = $true
            break
        }
    }

    if (-not $completed) {
        throw "Command timed out after $TimeoutSeconds seconds (PID: $pid_)"
    }

    # Decode output if base64 encoded
    $stdout = $statusData.'out-data'
    $stderr = $statusData.'err-data'

    if ($statusData.'out-truncated') {
        Write-Warning "stdout was truncated"
    }
    if ($statusData.'err-truncated') {
        Write-Warning "stderr was truncated"
    }

    return [PSCustomObject]@{
        ExitCode = $statusData.exitcode
        Stdout   = $stdout
        Stderr   = $stderr
        Pid      = $pid_
    }
}
