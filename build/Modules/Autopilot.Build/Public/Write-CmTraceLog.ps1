function Write-CmTraceLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Message,
        [Parameter(Mandatory)] [ValidateSet('Info', 'Warning', 'Error')] [string] $Severity,
        [Parameter(Mandatory)] [string] $Component
    )

    $type = switch ($Severity) {
        'Info'    { 1 }
        'Warning' { 2 }
        'Error'   { 3 }
    }

    $now = Get-Date
    $time = $now.ToString('HH:mm:ss.fff')
    $tzMinutes = [int][System.TimeZoneInfo]::Local.GetUtcOffset($now).TotalMinutes
    $tzSign = if ($tzMinutes -ge 0) { '+' } else { '-' }
    $tzAbs = [Math]::Abs($tzMinutes)
    $timeWithTz = "$time$tzSign$('{0:D3}' -f $tzAbs)"
    $date = $now.ToString('MM-dd-yyyy')

    $line = "<![LOG[$Message]LOG]!><time=`"$timeWithTz`" date=`"$date`" component=`"$Component`" context=`"`" type=`"$type`" thread=`"$PID`" file=`"`">"

    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Add-Content -Path $Path -Value $line -Encoding utf8
}
