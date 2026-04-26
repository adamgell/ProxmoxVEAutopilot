function Invoke-ScheduleTaskStep {
    <#
    .SYNOPSIS
        Write a Task Scheduler XML to <target>\Windows\System32\Tasks\<name>.
        TaskCache registry entries are NOT written here — Windows creates them
        on first boot when the Task Scheduler service starts and indexes the
        Tasks folder. This is more reliable than predicting the GUID structure
        Windows uses internally.
    #>
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param(
        [Parameter(Mandatory)] [string] $Target,
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $TaskXml
    )
    $tasksDir = "$Target\Windows\System32\Tasks"
    if (-not (Test-Path $tasksDir)) {
        New-Item -ItemType Directory -Path $tasksDir -Force | Out-Null
    }
    $taskFile = "$tasksDir\$Name"
    Set-Content -LiteralPath $taskFile -Value $TaskXml
    return [pscustomobject]@{
        LogTail = "wrote task xml -> $taskFile"
        Extra   = @{ task = $Name; target = $Target }
    }
}
