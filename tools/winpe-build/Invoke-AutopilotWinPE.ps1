# Invoke-AutopilotWinPE.ps1
#
# In-WinPE phase-0 agent. Boots a Proxmox VM into Windows by:
#   register -> capture_hash (M2) -> partition_disk -> apply_wim ->
#   inject_drivers -> validate_boot_drivers -> stage_autopilot_config ->
#   bake_boot_entry -> stage_unattend -> done -> reboot.
#
# Designed for PowerShell 5.1 (WinPE-bundled). Sourced by Pester tests
# during development; running it from startnet.cmd at WinPE boot drives
# the live flow.

function Read-AgentConfig {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "config not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-AgentLog {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('DEBUG','INFO','WARN','ERROR')] [string] $Level,
        [Parameter(Mandatory)] [string] $Message
    )
    $ts = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fffK')
    $line = "$ts [$Level] $Message"
    Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-VMIdentity {
    param(
        [scriptblock] $UuidResolver = { (Get-CimInstance Win32_ComputerSystemProduct).UUID },
        [scriptblock] $MacResolver  = {
            (Get-NetAdapter -Physical |
                Where-Object Status -eq 'Up' |
                Sort-Object ifIndex |
                Select-Object -First 1).MacAddress
        }
    )
    $uuid = & $UuidResolver
    $mac  = & $MacResolver
    if ([string]::IsNullOrWhiteSpace($uuid)) { throw "could not read SMBIOS UUID" }
    if ([string]::IsNullOrWhiteSpace($mac))  { throw "could not read MAC address"  }
    return [pscustomobject]@{
        vm_uuid = $uuid.ToString().ToLowerInvariant()
        mac     = $mac.ToString()
    }
}

function Invoke-OrchestratorRequest {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string] $Method,
        [hashtable] $Body,
        [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [int] $MaxAttempts = 5,
        [int] $RetryDelayMs = 2000,
        [int] $TimeoutSec = 30,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    $payload = $null
    if ($Body) { $payload = $Body | ConvertTo-Json -Depth 10 -Compress }

    $bases = @($BaseUrl)
    if ($FallbackBaseUrl) { $bases += $FallbackBaseUrl }

    $lastErr = $null
    foreach ($base in $bases) {
        $uri = ($base.TrimEnd('/')) + '/' + $Path.TrimStart('/')
        for ($i = 1; $i -le $MaxAttempts; $i++) {
            try {
                return & $RestInvoker $uri $Method $headers $payload 'application/json' $TimeoutSec
            } catch {
                $lastErr = $_
                if ($i -lt $MaxAttempts) { Start-Sleep -Milliseconds $RetryDelayMs }
            }
        }
    }
    throw $lastErr
}

function _ReportStepState {
    param(
        [string] $BaseUrl, [string] $BearerToken, [int] $StepId,
        [string] $State, [string] $ErrorMessage,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker
    )
    $body = @{ state = $State }
    if ($ErrorMessage) { $body.error = $ErrorMessage }
    $reqArgs = @{
        BaseUrl = $BaseUrl
        Path = "/winpe/step/$StepId/result"
        Method = 'POST'
        Body = $body
        BearerToken = $BearerToken
        RestInvoker = $RestInvoker
    }
    if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
    $r = Invoke-OrchestratorRequest @reqArgs
    if ($r.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $r.bearer_token) {
        return $r.bearer_token
    }
    return $BearerToken
}

function Invoke-ActionLoop {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [object[]] $Actions,
        [Parameter(Mandatory)] [hashtable] $Handlers,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $token = $BearerToken
    foreach ($action in $Actions) {
        $kind = $action.kind
        $stepId = [int] $action.step_id
        if (-not $Handlers.ContainsKey($kind)) {
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' `
                -ErrorMessage "no handler for kind: $kind" `
                -FallbackBaseUrl $FallbackBaseUrl `
                -RestInvoker $RestInvoker
            throw "no handler registered for kind: $kind"
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'running' `
            -FallbackBaseUrl $FallbackBaseUrl `
            -RestInvoker $RestInvoker
        try {
            # Handlers receive the CURRENT token (post-running-refresh)
            # rather than capturing the original via closure, so a long
            # apply_wim followed by stage_unattend GETs use a fresh
            # token, not one that may have expired during the apply.
            & $Handlers[$kind] $action.params $token
        } catch {
            $msg = $_.Exception.Message
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' -ErrorMessage $msg `
                -FallbackBaseUrl $FallbackBaseUrl `
                -RestInvoker $RestInvoker
            throw
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'ok' `
            -FallbackBaseUrl $FallbackBaseUrl `
            -RestInvoker $RestInvoker
    }
    return $token
}

$script:DiskpartScriptRecoveryBeforeC = @'
select disk 0
clean
convert gpt
create partition efi size=100
format fs=fat32 quick label="EFI"
assign letter=S
create partition msr size=16
create partition primary size=1024
format fs=ntfs quick label="Recovery"
set id="de94bba4-06d1-4d40-a16a-bfd50179d6ac"
gpt attributes=0x8000000000000001
create partition primary
format fs=ntfs quick label="Windows"
assign letter=V
exit
'@

function Invoke-Action-PartitionDisk {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DiskpartRunner = { param($script)
            $tmp = [System.IO.Path]::GetTempFileName()
            try {
                Set-Content -LiteralPath $tmp -Value $script -Encoding ASCII
                & diskpart.exe /s $tmp
                if ($LASTEXITCODE -ne 0) { throw "diskpart failed: $LASTEXITCODE" }
            } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        }
    )
    switch ($Params.layout) {
        'recovery_before_c' {
            & $DiskpartRunner $script:DiskpartScriptRecoveryBeforeC
        }
        default { throw "partition_disk: unknown layout '$($Params.layout)'" }
    }
}

function _RunDism {
    param([string[]] $DismArgs)
    $stdout = & dism.exe @DismArgs 2>&1 | Out-String
    return @{ ExitCode = $LASTEXITCODE; Stdout = $stdout; Stderr = '' }
}

function _ResolveSourceWim {
    foreach ($drive in @('D','E','F','G','H')) {
        $p = "$($drive):\sources\install.wim"
        if (Test-Path -LiteralPath $p) { return $p }
    }
    throw "could not find sources\install.wim on attached CD-ROMs"
}

function _ResolveIndexByName {
    param([string] $Wim, [string] $Name)
    $out = (& dism.exe /Get-WimInfo /WimFile:$Wim) 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "dism /Get-WimInfo failed: $LASTEXITCODE" }
    # Parse blocks. dism output:
    #   Index : 6
    #   Name : Windows 11 Enterprise
    $current = $null
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*Index\s*:\s*(\d+)\s*$') { $current = [int]$Matches[1] }
        elseif ($line -match '^\s*Name\s*:\s*(.+?)\s*$' -and $Matches[1] -eq $Name) {
            return $current
        }
    }
    throw "no image index matched name: $Name"
}

function Invoke-Action-ApplyWim {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DismRunner = { param($a) _RunDism -DismArgs $a },
        [scriptblock] $SourceWimResolver = { _ResolveSourceWim },
        [scriptblock] $IndexResolver = { param($wim, $name)
            _ResolveIndexByName -Wim $wim -Name $name
        }
    )
    $name = [string] $Params.image_index_metadata_name
    if ([string]::IsNullOrWhiteSpace($name)) { throw "apply_wim: missing image_index_metadata_name" }
    $wim = & $SourceWimResolver
    $index = & $IndexResolver $wim $name
    $dismArgs = @(
        '/Apply-Image',
        "/ImageFile:$wim",
        "/Index:$index",
        '/ApplyDir:V:\\'
    )
    $r = & $DismRunner $dismArgs
    if ($r.ExitCode -ne 0) {
        throw "dism /Apply-Image failed (exit $($r.ExitCode)): $($r.Stdout)"
    }
}

function _ResolveVirtioPath {
    foreach ($drive in @('D','E','F','G','H','I')) {
        $marker = "$($drive):\virtio-win_license.txt"
        if (Test-Path -LiteralPath $marker) { return "$($drive):\" }
        $marker = "$($drive):\NetKVM"
        if (Test-Path -LiteralPath $marker) { return "$($drive):\" }
    }
    throw "could not find VirtIO ISO on any attached CD-ROM (D-I)"
}

function Invoke-Action-InjectDrivers {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DismRunner = { param($a) _RunDism -DismArgs $a },
        [scriptblock] $VirtioPathResolver = { _ResolveVirtioPath }
    )
    $virtioRoot = & $VirtioPathResolver
    $dismArgs = @(
        '/Image:V:\\',
        '/Add-Driver',
        "/Driver:$virtioRoot",
        '/Recurse',
        '/ForceUnsigned'
    )
    $r = & $DismRunner $dismArgs
    if ($r.ExitCode -ne 0) {
        throw "dism /Add-Driver failed (exit $($r.ExitCode)): $($r.Stdout)"
    }
}

function _GetInjectedDriverInfs {
    # /Format:Table truncates "Original File Name" and pads columns
    # unpredictably across DISM versions, so a tail-of-line regex
    # silently returns nothing on real output and validate_boot_drivers
    # incorrectly fails every run. /Format:List emits each driver as a
    # block of "Key : Value" lines; "Original File Name : <path>" is
    # the original INF path (e.g. "E:\NetKVM\w11\amd64\netkvm.inf"),
    # which we tail-split on \ to get the leaf filename.
    $out = (& dism.exe /Image:V:\ /Get-Drivers /Format:List) 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "dism /Get-Drivers failed: $LASTEXITCODE" }
    $infs = @()
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*Original File Name\s*:\s*(.+\\)?(\S+\.inf)\s*$') {
            $infs += $Matches[2].ToLowerInvariant()
        }
    }
    return $infs
}

function Invoke-Action-ValidateBootDrivers {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DriverInfResolver = { _GetInjectedDriverInfs }
    )
    $required = @($Params.required_infs) | ForEach-Object { $_.ToLowerInvariant() }
    $present = @(& $DriverInfResolver) | ForEach-Object { $_.ToLowerInvariant() }
    $missing = $required | Where-Object { $_ -notin $present }
    if ($missing.Count -gt 0) {
        throw "validate_boot_drivers: missing INFs: $($missing -join ', ')"
    }
}

function Invoke-Action-StageAutopilotConfig {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [int] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $guestPath = [string] $Params.guest_path
    if ([string]::IsNullOrWhiteSpace($guestPath)) {
        throw "stage_autopilot_config: missing guest_path"
    }
    $reqArgs = @{
        BaseUrl = $BaseUrl
        Path = "/winpe/autopilot-config/$RunId"
        Method = 'GET'
        BearerToken = $BearerToken
        RestInvoker = $RestInvoker
    }
    if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
    $payload = Invoke-OrchestratorRequest @reqArgs
    $dir = Split-Path -Parent $guestPath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $json = $payload | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $guestPath -Value $json -Encoding UTF8
}
