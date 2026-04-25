function New-BuildLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Owner
    )

    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $existing = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $alive = $false
        try {
            $proc = Get-Process -Id $existing.pid -ErrorAction Stop
            $alive = ($null -ne $proc)
        } catch {
            $alive = $false
        }
        if ($alive) {
            throw "Build lock held by PID $($existing.pid) (owner=$($existing.owner)) since $($existing.acquiredAt). Path=$Path"
        }
        Remove-Item -LiteralPath $Path -Force
    }

    $payload = @{
        owner      = $Owner
        pid        = $PID
        acquiredAt = (Get-Date).ToString('o')
    } | ConvertTo-Json
    Set-Content -LiteralPath $Path -Value $payload -Encoding utf8

    $lockPath = $Path
    $obj = [pscustomobject]@{ Path = $lockPath }
    $obj | Add-Member -MemberType ScriptMethod -Name Release -Value {
        if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force }
    }.GetNewClosure()
    return $obj
}
