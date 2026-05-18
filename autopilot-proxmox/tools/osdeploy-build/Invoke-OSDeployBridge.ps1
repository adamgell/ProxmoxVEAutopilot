# OSDeploy WinPE bridge for ProxmoxVEAutopilot.
#
# The controller records Proxmox VM identity before boot. This bridge matches
# that identity from WinPE, retrieves the run package, applies the baked Server
# image, stages the v2 full-OS client, emits lifecycle events, and reboots.

$ErrorActionPreference = 'Stop'
$script:OSDeployHeartbeatIntervalSeconds = 15
$script:OSDeployPeSteps = @(
    'register',
    'package',
    'locate_image',
    'guard_existing_windows',
    'partition',
    'apply_image',
    'inject_drivers',
    'stage_osd_client',
    'stage_unattend',
    'bcdboot',
    'handoff'
)

function Write-OSDeployConsoleStep {
    param(
        [Parameter(Mandatory)] [string] $Step,
        [Parameter(Mandatory)] [string] $State,
        [string] $Message = ''
    )
    $line = "[OSDeploy PE] [$State] $Step"
    if (-not [string]::IsNullOrWhiteSpace($Message)) {
        $line = "$line - $Message"
    }
    Write-Host $line
}

function Write-OSDeployConsoleProgress {
    param(
        [Parameter(Mandatory)] [string] $Step,
        [string] $Message = ''
    )
    Write-Host "[OSDeploy PE] [heartbeat] $Step - $Message"
}

function Write-OSDeployConsoleError {
    param(
        [Parameter(Mandatory)] [string] $Step,
        [Parameter(Mandatory)] [string] $Message
    )
    Write-Host "[OSDeploy PE] [error] $Step - $Message"
}

function Read-OSDeployConfig {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "OSDeploy config not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Resolve-OSDeployMacAddress {
    $adapter = Get-CimInstance -ClassName Win32_NetworkAdapter |
        Where-Object {
            $_.MACAddress -and
            ($_.PhysicalAdapter -eq $true -or $_.Name -match 'VirtIO|Ethernet|Network')
        } |
        Sort-Object InterfaceIndex, Index |
        Select-Object -First 1
    if ($adapter) { return $adapter.MACAddress }

    $ipAdapter = Get-CimInstance -ClassName Win32_NetworkAdapterConfiguration |
        Where-Object { $_.MACAddress } |
        Sort-Object InterfaceIndex, Index |
        Select-Object -First 1
    if ($ipAdapter) { return $ipAdapter.MACAddress }

    throw 'could not read MAC address'
}

function Get-OSDeployVMIdentity {
    $product = Get-CimInstance Win32_ComputerSystemProduct
    $uuid = [string] $product.UUID
    if ([string]::IsNullOrWhiteSpace($uuid)) {
        throw 'could not read SMBIOS UUID'
    }
    return [pscustomobject]@{
        vm_uuid = $uuid.ToLowerInvariant()
        mac = (Resolve-OSDeployMacAddress)
    }
}

function Invoke-OSDeployRequest {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string] $Method,
        [object] $Body,
        [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [int] $MaxAttempts = 5,
        [int] $RetryDelaySeconds = 3
    )
    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    $payload = $null
    if ($null -ne $Body) { $payload = $Body | ConvertTo-Json -Depth 20 -Compress }
    $bases = @($BaseUrl)
    if ($FallbackBaseUrl) { $bases += $FallbackBaseUrl }

    $lastError = $null
    foreach ($base in $bases) {
        $uri = ($base.TrimEnd('/')) + '/' + $Path.TrimStart('/')
        for ($i = 1; $i -le $MaxAttempts; $i++) {
            try {
                return Invoke-RestMethod -Uri $uri -Method $Method -Headers $headers `
                    -Body $payload -ContentType 'application/json' -TimeoutSec 60
            } catch {
                $lastError = $_
                if ($i -lt $MaxAttempts) { Start-Sleep -Seconds $RetryDelaySeconds }
            }
        }
    }
    throw $lastError
}

function Write-OSDeployEvent {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [string] $Phase,
        [Parameter(Mandatory)] [string] $EventType,
        [string] $Message,
        [string] $FallbackBaseUrl,
        [string] $Severity = 'info',
        [hashtable] $Data = @{}
    )
    try {
        Invoke-OSDeployRequest -BaseUrl $BaseUrl `
            -FallbackBaseUrl $FallbackBaseUrl `
            -Path "/api/osdeploy/v1/runs/$RunId/events" `
            -Method POST `
            -BearerToken $BearerToken `
            -Body @{
                phase = $Phase
                event_type = $EventType
                severity = $Severity
                message = $Message
                data = $Data
            } | Out-Null
    } catch {
        Write-Warning "failed to report OSDeploy event ${EventType}: $($_.Exception.Message)"
    }
}

function Invoke-OSDeployPeStep {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [scriptblock] $ScriptBlock,
        [string] $FallbackBaseUrl,
        [hashtable] $Data = @{}
    )
    Write-OSDeployConsoleStep -Step $Name -State 'starting'
    Write-OSDeployEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
        -RunId $RunId -BearerToken $BearerToken -Phase 'pe' `
        -EventType 'osdeploy_pe_step_starting' `
        -Message "OSDeploy PE step starting: $Name" `
        -Data (@{ step = $Name } + $Data)
    $started = Get-Date
    try {
        $result = & $ScriptBlock
        $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 3)
        Write-OSDeployConsoleStep -Step $Name -State 'ok' -Message "${elapsed}s"
        Write-OSDeployEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
            -RunId $RunId -BearerToken $BearerToken -Phase 'pe' `
            -EventType 'osdeploy_pe_step_ok' `
            -Message "OSDeploy PE step completed: $Name" `
            -Data (@{ step = $Name; elapsed_seconds = $elapsed } + $Data)
        return $result
    } catch {
        $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 3)
        Write-OSDeployConsoleError -Step $Name -Message $_.Exception.Message
        Write-OSDeployEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
            -RunId $RunId -BearerToken $BearerToken -Phase 'pe' `
            -EventType 'osdeploy_pe_step_error' `
            -Severity 'error' `
            -Message $_.Exception.Message `
            -Data (@{ step = $Name; elapsed_seconds = $elapsed } + $Data)
        throw
    }
}

function Join-OSDeployNativeArguments {
    param([Parameter(Mandatory)] [string[]] $Arguments)
    return ($Arguments | ForEach-Object {
        $value = [string] $_
        if ($value -match '[\s"]') {
            '"' + ($value -replace '"', '\"') + '"'
        } else {
            $value
        }
    }) -join ' '
}

function Invoke-OSDeployNativeProcessWithHeartbeat {
    param(
        [Parameter(Mandatory)] [string] $FileName,
        [Parameter(Mandatory)] [string[]] $Arguments,
        [Parameter(Mandatory)] [string] $Step,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl
    )
    $outputRoot = 'X:\autopilot\osdeploy\native-output'
    New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
    $outputId = [guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $outputRoot "$Step-$outputId.out.log"
    $stderrPath = Join-Path $outputRoot "$Step-$outputId.err.log"
    $argumentLine = Join-OSDeployNativeArguments -Arguments $Arguments
    $process = Start-Process `
        -FilePath $FileName `
        -ArgumentList $argumentLine `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -NoNewWindow `
        -PassThru
    try {
        $lastHeartbeat = Get-Date
        while (-not $process.WaitForExit(1000)) {
            if (((Get-Date) - $lastHeartbeat).TotalSeconds -ge $script:OSDeployHeartbeatIntervalSeconds) {
                $lastHeartbeat = Get-Date
                Write-OSDeployConsoleProgress -Step $Step -Message "pid=$($process.Id)"
                Write-OSDeployEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
                    -RunId $RunId -BearerToken $BearerToken -Phase 'pe' `
                    -EventType 'osdeploy_pe_step_heartbeat' `
                    -Message "OSDeploy PE step still running: $Step" `
                    -Data @{ step = $Step; pid = $process.Id }
            }
        }
        $process.Refresh()
        $exitCode = if ($null -ne $process.ExitCode) { [int] $process.ExitCode } else { 0 }
    } finally {
        $process.Dispose()
    }
    $stdout = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue } else { '' }
    $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue } else { '' }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Stdout = $stdout
        Stderr = $stderr
    }
}

function Save-OSDeployRunPackage {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $Root = 'X:\autopilot\osdeploy'
    )
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    $Package | ConvertTo-Json -Depth 20 |
        Set-Content -LiteralPath (Join-Path $Root 'package.json') -Encoding UTF8
    $BearerToken |
        Set-Content -LiteralPath (Join-Path $Root 'bearer.token') -Encoding ASCII
    return $Root
}

function Get-OSDeployObjectProperty {
    param(
        [AllowNull()] [object] $Value,
        [Parameter(Mandatory)] [string] $Name
    )
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        if ($Value.Contains($Name)) { return $Value[$Name] }
        return $null
    }
    if ($Value.PSObject.Properties.Match($Name).Count -gt 0) {
        return $Value.$Name
    }
    return $null
}

function Set-OSDeployObjectProperty {
    param(
        [Parameter(Mandatory)] [object] $InputObject,
        [Parameter(Mandatory)] [string] $Name,
        [object] $Value
    )
    if ($InputObject.PSObject.Properties.Match($Name).Count -gt 0) {
        $InputObject.PSObject.Properties[$Name].Value = $Value
    } else {
        Add-Member -InputObject $InputObject `
            -NotePropertyName $Name `
            -NotePropertyValue $Value `
            -Force
    }
}

function Verify-OSDeployStagedFileHash {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [AllowNull()] [string] $ExpectedSha256,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [string] $SourcePath = ''
    )
    $expected = ([string] $ExpectedSha256).Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($expected)) {
        return
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "staged file SHA256 mismatch expected=$expected actual=$actual path=$Path"
    }
    Write-OSDeployEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
        -RunId $RunId -BearerToken $BearerToken -Phase 'pe' `
        -EventType 'osdeploy_staged_file_verified' `
        -Message 'OSDeploy staged file SHA256 verified' `
        -Data @{
            path = $Path
            source_path = $SourcePath
            sha256 = $actual
        }
}

function Join-OSDeployPath {
    param(
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [string] $RelativePath
    )
    $path = $Root
    foreach ($part in ($RelativePath -split '[\\/]')) {
        if (-not [string]::IsNullOrWhiteSpace($part)) {
            $path = Join-Path $path $part
        }
    }
    return $path
}

function Get-OSDeployOfflineProgramDataPath {
    param([Parameter(Mandatory)] [string] $WindowsRoot)
    $root = [System.IO.Path]::GetPathRoot($WindowsRoot)
    return Join-Path $root 'ProgramData'
}

function Resolve-OSDeployPackageTargetPath {
    param(
        [Parameter(Mandatory)] [string] $TargetPath,
        [Parameter(Mandatory)] [string] $WindowsRoot
    )
    if ($TargetPath -match '^[A-Za-z]:\\Windows\\(.+)$') {
        return Join-OSDeployPath -Root $WindowsRoot -RelativePath $Matches[1]
    }
    if ($TargetPath -match '^[A-Za-z]:\\ProgramData\\(.+)$') {
        return Join-OSDeployPath `
            -Root (Get-OSDeployOfflineProgramDataPath -WindowsRoot $WindowsRoot) `
            -RelativePath $Matches[1]
    }
    throw "unsupported OSDeploy v2 client package target path: $TargetPath"
}

function Save-OSDeployOsdClientPackage {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [string] $FallbackBaseUrl
    )
    if (-not (Get-OSDeployObjectProperty -Value $Package -Name 'payloads')) {
        throw 'OSDeploy package is missing payloads'
    }
    $osdClient = Get-OSDeployObjectProperty -Value $Package.payloads -Name 'osd_client'
    if (-not $osdClient -or -not (Get-OSDeployObjectProperty -Value $osdClient -Name 'url')) {
        throw 'OSDeploy package is missing OSD client payload URL'
    }

    $headers = @{ Authorization = "Bearer $BearerToken" }
    $clientPackage = Invoke-RestMethod `
        -Uri ([string] $osdClient.url) `
        -Headers $headers `
        -TimeoutSec 300

    foreach ($file in @($clientPackage.files)) {
        $targetPath = [string] $file.path
        $destination = Resolve-OSDeployPackageTargetPath `
            -TargetPath $targetPath `
            -WindowsRoot $WindowsRoot
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        [System.IO.File]::WriteAllBytes(
            $destination,
            [Convert]::FromBase64String([string] $file.content_b64)
        )
        Verify-OSDeployStagedFileHash `
            -Path $destination `
            -ExpectedSha256 ([string] (Get-OSDeployObjectProperty -Value $file -Name 'sha256')) `
            -BaseUrl $BaseUrl `
            -RunId $RunId `
            -BearerToken $BearerToken `
            -FallbackBaseUrl $FallbackBaseUrl `
            -SourcePath $targetPath
    }

    $programDataRoot = Get-OSDeployOfflineProgramDataPath -WindowsRoot $WindowsRoot
    $osdRoot = Join-OSDeployPath `
        -Root $programDataRoot `
        -RelativePath 'ProxmoxVEAutopilot\OSD'
    New-Item -ItemType Directory -Path $osdRoot -Force | Out-Null

    $config = $clientPackage.config | ConvertTo-Json -Depth 30 | ConvertFrom-Json
    Set-OSDeployObjectProperty `
        -InputObject $config `
        -Name 'flask_base_url' `
        -Value ([string] $Package.server_base_url)
    if (Get-OSDeployObjectProperty -Value $Package -Name 'server_base_url_fallback') {
        Set-OSDeployObjectProperty `
            -InputObject $config `
            -Name 'flask_base_url_fallback' `
            -Value ([string] $Package.server_base_url_fallback)
    }
    $osdeployAgent = Get-OSDeployObjectProperty -Value $Package -Name 'agent'
    if ($osdeployAgent) {
        foreach ($requiredAgentField in @('bootstrap_token', 'bootstrap_url', 'config_url')) {
            if (-not (Get-OSDeployObjectProperty -Value $osdeployAgent -Name $requiredAgentField)) {
                throw "OSDeploy agent package is missing $requiredAgentField"
            }
        }
        Set-OSDeployObjectProperty `
            -InputObject $config `
            -Name 'osdeploy_agent' `
            -Value $osdeployAgent
        Set-OSDeployObjectProperty `
            -InputObject $config `
            -Name 'agent_heartbeat_url' `
            -Value (([string] $Package.server_base_url).TrimEnd('/') + '/api/agent/v1/heartbeat')
    }
    $payloads = Get-OSDeployObjectProperty -Value $Package -Name 'payloads'
    if ($payloads) {
        $agentMsi = Get-OSDeployObjectProperty -Value $payloads -Name 'autopilotagent_msi'
        $agentPostinstall = Get-OSDeployObjectProperty -Value $payloads -Name 'autopilotagent_postinstall'
        if ($agentMsi) {
            if (-not (Get-OSDeployObjectProperty -Value $agentMsi -Name 'url')) {
                throw 'OSDeploy AutopilotAgent MSI payload is missing url'
            }
            Set-OSDeployObjectProperty `
                -InputObject $config `
                -Name 'autopilotagent_msi' `
                -Value $agentMsi
        }
        if ($agentPostinstall) {
            if (-not (Get-OSDeployObjectProperty -Value $agentPostinstall -Name 'url')) {
                throw 'OSDeploy AutopilotAgent postinstall payload is missing url'
            }
            Set-OSDeployObjectProperty `
                -InputObject $config `
                -Name 'autopilotagent_postinstall' `
                -Value $agentPostinstall
        }
    }
    $config | ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath (Join-Path $osdRoot 'osd-config.json') -Encoding UTF8
    return $osdRoot
}

function New-OSDeployUnattendTextElement {
    param(
        [Parameter(Mandatory)] [System.Xml.XmlDocument] $Xml,
        [Parameter(Mandatory)] [System.Xml.XmlElement] $Parent,
        [Parameter(Mandatory)] [string] $Namespace,
        [Parameter(Mandatory)] [string] $Name,
        [AllowNull()] [object] $Value
    )
    $node = $Xml.CreateElement($Name, $Namespace)
    if ($null -ne $Value) { $node.InnerText = [string] $Value }
    $Parent.AppendChild($node) | Out-Null
    return $node
}

function Set-OSDeployUnattendTextElement {
    param(
        [Parameter(Mandatory)] [System.Xml.XmlDocument] $Xml,
        [Parameter(Mandatory)] [System.Xml.XmlElement] $Parent,
        [Parameter(Mandatory)] [System.Xml.XmlNamespaceManager] $NamespaceManager,
        [Parameter(Mandatory)] [string] $Namespace,
        [Parameter(Mandatory)] [string] $Name,
        [AllowNull()] [object] $Value
    )
    $node = $Parent.SelectSingleNode("u:$Name", $NamespaceManager)
    if (-not $node) {
        $node = $Xml.CreateElement($Name, $Namespace)
        $Parent.AppendChild($node) | Out-Null
    }
    $node.InnerText = [string] $Value
    return $node
}

function New-OSDeployBootstrapPassword {
    $bytes = New-Object byte[] 18
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    $randomText = [Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', ''
    if ($randomText.Length -lt 20) {
        $randomText = ($randomText + ([guid]::NewGuid().ToString('N')))
    }
    return ('Pve!' + $randomText.Substring(0, 20))
}

function Set-OSDeployUnattendPasswordElement {
    param(
        [Parameter(Mandatory)] [System.Xml.XmlDocument] $Xml,
        [Parameter(Mandatory)] [System.Xml.XmlElement] $Parent,
        [Parameter(Mandatory)] [System.Xml.XmlNamespaceManager] $NamespaceManager,
        [Parameter(Mandatory)] [string] $Namespace,
        [Parameter(Mandatory)] [string] $Value
    )
    $passwordNode = $Parent.SelectSingleNode('u:Password', $NamespaceManager)
    if (-not $passwordNode) {
        $passwordNode = $Xml.CreateElement('Password', $Namespace)
        $Parent.AppendChild($passwordNode) | Out-Null
    }
    Set-OSDeployUnattendTextElement -Xml $Xml `
        -Parent $passwordNode `
        -NamespaceManager $NamespaceManager `
        -Namespace $Namespace `
        -Name 'Value' `
        -Value $Value | Out-Null
    Set-OSDeployUnattendTextElement -Xml $Xml `
        -Parent $passwordNode `
        -NamespaceManager $NamespaceManager `
        -Namespace $Namespace `
        -Name 'PlainText' `
        -Value 'true' | Out-Null
}

function ConvertTo-OSDeployWindowsComputerName {
    param([AllowNull()] [string] $Name)
    $normalized = ([string] $Name).Trim() -replace '[^A-Za-z0-9-]', ''
    if ([string]::IsNullOrWhiteSpace($normalized)) { return $null }
    if ($normalized.Length -gt 15) { $normalized = $normalized.Substring(0, 15) }
    if ($normalized -match '^\d+$') {
        $normalized = "PVE-$normalized"
        if ($normalized.Length -gt 15) { $normalized = $normalized.Substring(0, 15) }
    }
    return $normalized
}

function Resolve-OSDeployProductKey {
    param(
        [AllowNull()] [string] $Version,
        [AllowNull()] [string] $Edition,
        [AllowNull()] [string] $ImageName
    )
    $explicitKey = [Environment]::GetEnvironmentVariable('OSDEPLOY_UNATTEND_PRODUCT_KEY')
    if (-not [string]::IsNullOrWhiteSpace($explicitKey)) {
        return $explicitKey.Trim()
    }
    if ([Environment]::GetEnvironmentVariable('OSDEPLOY_ALLOW_DEFAULT_GVLK') -ne '1') {
        return $null
    }
    $product = "$Version $Edition $ImageName"
    if ($product -match '(?i)\bEvaluation\b') {
        return $null
    }
    if (([string] $Version) -match '(?i)Windows Server') {
        if (([string] $Version) -match '2025') {
            if (([string] $Edition) -match '(?i)Standard') {
                return 'TVRH6-WHNXV-R9WG3-9XRFY-MY832'
            }
            return 'D764K-2NDRG-47T6Q-P8T8W-YP6DF'
        }
        if (([string] $Edition) -match '(?i)Standard') {
            return 'VDYBN-27WPP-V4HQT-9VMD4-VMK7H'
        }
        return 'WX4NM-KYWYW-QJJR4-XV3QB-6VM33'
    }
    if (([string] $Version) -match '(?i)Windows (10|11)') {
        if (([string] $Edition) -match '(?i)Enterprise') {
            return 'NPPR9-FWDCX-D2C8J-H872K-2YT43'
        }
        if (([string] $Edition) -match '(?i)Education') {
            return 'NW6C2-QMPVW-D7KKK-3GKT6-VCFB2'
        }
        if (([string] $Edition) -match '(?i)Pro') {
            return 'W269N-WFGWX-YVC9B-4J6C9-T83GX'
        }
    }
    return $null
}

function Remove-OSDeployUnattendElement {
    param(
        [Parameter(Mandatory)] [System.Xml.XmlElement] $Parent,
        [Parameter(Mandatory)] [System.Xml.XmlNamespaceManager] $NamespaceManager,
        [Parameter(Mandatory)] [string] $Name
    )
    $node = $Parent.SelectSingleNode("u:$Name", $NamespaceManager)
    if ($node) {
        $Parent.RemoveChild($node) | Out-Null
    }
}

function Add-OSDeployOfflineUnattend {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $ComputerName,
        [string] $Locale = 'en-US',
        [string] $Version = 'Windows Server 2022',
        [string] $Edition = 'Datacenter',
        [string] $ImageName
    )
    $unattendPath = Join-OSDeployPath -Root $WindowsRoot -RelativePath 'Panther\Unattend.xml'
    $pantherRoot = Split-Path -Parent $unattendPath
    New-Item -ItemType Directory -Path $pantherRoot -Force | Out-Null
    $unattendNs = 'urn:schemas-microsoft-com:unattend'
    $wcmNs = 'http://schemas.microsoft.com/WMIConfig/2002/State'

    $xml = New-Object System.Xml.XmlDocument
    $xml.PreserveWhitespace = $false
    if (Test-Path -LiteralPath $unattendPath) {
        try {
            $xml.Load($unattendPath)
        } catch {
            throw "Existing offline Unattend.xml is not valid XML: $($_.Exception.Message)"
        }
    } else {
        $root = $xml.CreateElement('unattend', $unattendNs)
        $xml.AppendChild($root) | Out-Null
    }
    if (-not $xml.DocumentElement -or $xml.DocumentElement.LocalName -ne 'unattend') {
        throw 'Existing offline Unattend.xml does not have an unattend root element'
    }
    $rootElement = $xml.DocumentElement
    if (-not $rootElement.NamespaceURI) {
        throw 'Existing offline Unattend.xml root does not use the Windows unattend namespace'
    }
    [void] $rootElement.SetAttribute('xmlns:wcm', $wcmNs)

    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('u', $unattendNs)
    $ns.AddNamespace('wcm', $wcmNs)

    $specialize = $rootElement.SelectSingleNode("u:settings[@pass='specialize']", $ns)
    if (-not $specialize) {
        $specialize = $xml.CreateElement('settings', $unattendNs)
        [void] $specialize.SetAttribute('pass', 'specialize')
        $rootElement.AppendChild($specialize) | Out-Null
    }
    $shellSpecialize = $specialize.SelectSingleNode("u:component[@name='Microsoft-Windows-Shell-Setup' and @processorArchitecture='amd64']", $ns)
    if (-not $shellSpecialize) {
        $shellSpecialize = $xml.CreateElement('component', $unattendNs)
        [void] $shellSpecialize.SetAttribute('name', 'Microsoft-Windows-Shell-Setup')
        [void] $shellSpecialize.SetAttribute('processorArchitecture', 'amd64')
        [void] $shellSpecialize.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        [void] $shellSpecialize.SetAttribute('language', 'neutral')
        [void] $shellSpecialize.SetAttribute('versionScope', 'nonSxS')
        $specialize.AppendChild($shellSpecialize) | Out-Null
    }
    $windowsComputerName = ConvertTo-OSDeployWindowsComputerName -Name $ComputerName
    if ($windowsComputerName) {
        Set-OSDeployUnattendTextElement -Xml $xml `
            -Parent $shellSpecialize `
            -NamespaceManager $ns `
            -Namespace $unattendNs `
            -Name 'ComputerName' `
            -Value $windowsComputerName | Out-Null
    }
    $productKey = Resolve-OSDeployProductKey `
        -Version $Version `
        -Edition $Edition `
        -ImageName $ImageName
    if ([string]::IsNullOrWhiteSpace($productKey)) {
        Remove-OSDeployUnattendElement `
            -Parent $shellSpecialize `
            -NamespaceManager $ns `
            -Name 'ProductKey'
    } else {
        Set-OSDeployUnattendTextElement -Xml $xml `
            -Parent $shellSpecialize `
            -NamespaceManager $ns `
            -Namespace $unattendNs `
            -Name 'ProductKey' `
            -Value $productKey | Out-Null
    }

    $sppUx = $specialize.SelectSingleNode("u:component[@name='Microsoft-Windows-Security-SPP-UX' and @processorArchitecture='amd64']", $ns)
    if (-not $sppUx) {
        $sppUx = $xml.CreateElement('component', $unattendNs)
        [void] $sppUx.SetAttribute('name', 'Microsoft-Windows-Security-SPP-UX')
        [void] $sppUx.SetAttribute('processorArchitecture', 'amd64')
        [void] $sppUx.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        [void] $sppUx.SetAttribute('language', 'neutral')
        [void] $sppUx.SetAttribute('versionScope', 'nonSxS')
        $specialize.AppendChild($sppUx) | Out-Null
    }
    Set-OSDeployUnattendTextElement -Xml $xml `
        -Parent $sppUx `
        -NamespaceManager $ns `
        -Namespace $unattendNs `
        -Name 'SkipAutoActivation' `
        -Value 'true' | Out-Null

    $oobeSettings = $rootElement.SelectSingleNode("u:settings[@pass='oobeSystem']", $ns)
    if (-not $oobeSettings) {
        $oobeSettings = $xml.CreateElement('settings', $unattendNs)
        [void] $oobeSettings.SetAttribute('pass', 'oobeSystem')
        $rootElement.AppendChild($oobeSettings) | Out-Null
    }
    if ([string]::IsNullOrWhiteSpace($Locale)) {
        $Locale = 'en-US'
    }
    $internationalOobe = $oobeSettings.SelectSingleNode("u:component[@name='Microsoft-Windows-International-Core' and @processorArchitecture='amd64']", $ns)
    if (-not $internationalOobe) {
        $internationalOobe = $xml.CreateElement('component', $unattendNs)
        [void] $internationalOobe.SetAttribute('name', 'Microsoft-Windows-International-Core')
        [void] $internationalOobe.SetAttribute('processorArchitecture', 'amd64')
        [void] $internationalOobe.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        [void] $internationalOobe.SetAttribute('language', 'neutral')
        [void] $internationalOobe.SetAttribute('versionScope', 'nonSxS')
        $oobeSettings.AppendChild($internationalOobe) | Out-Null
    }
    foreach ($entry in @(
        @{ Name = 'InputLocale'; Value = $Locale },
        @{ Name = 'SystemLocale'; Value = $Locale },
        @{ Name = 'UILanguage'; Value = $Locale },
        @{ Name = 'UserLocale'; Value = $Locale }
    )) {
        Set-OSDeployUnattendTextElement -Xml $xml `
            -Parent $internationalOobe `
            -NamespaceManager $ns `
            -Namespace $unattendNs `
            -Name $entry.Name `
            -Value $entry.Value | Out-Null
    }
    $shellOobe = $oobeSettings.SelectSingleNode("u:component[@name='Microsoft-Windows-Shell-Setup' and @processorArchitecture='amd64']", $ns)
    if (-not $shellOobe) {
        $shellOobe = $xml.CreateElement('component', $unattendNs)
        [void] $shellOobe.SetAttribute('name', 'Microsoft-Windows-Shell-Setup')
        [void] $shellOobe.SetAttribute('processorArchitecture', 'amd64')
        [void] $shellOobe.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        [void] $shellOobe.SetAttribute('language', 'neutral')
        [void] $shellOobe.SetAttribute('versionScope', 'nonSxS')
        $oobeSettings.AppendChild($shellOobe) | Out-Null
    }
    $oobe = $shellOobe.SelectSingleNode('u:OOBE', $ns)
    if (-not $oobe) {
        $oobe = $xml.CreateElement('OOBE', $unattendNs)
        $shellOobe.AppendChild($oobe) | Out-Null
    }
    foreach ($entry in @(
        @{ Name = 'HideEULAPage'; Value = 'true' },
        @{ Name = 'HideLocalAccountScreen'; Value = 'true' },
        @{ Name = 'HideOEMRegistrationScreen'; Value = 'true' },
        @{ Name = 'HideOnlineAccountScreens'; Value = 'true' },
        @{ Name = 'HideWirelessSetupInOOBE'; Value = 'true' },
        @{ Name = 'SkipMachineOOBE'; Value = 'true' },
        @{ Name = 'SkipUserOOBE'; Value = 'true' },
        @{ Name = 'ProtectYourPC'; Value = '3' }
    )) {
        Set-OSDeployUnattendTextElement -Xml $xml `
            -Parent $oobe `
            -NamespaceManager $ns `
            -Namespace $unattendNs `
            -Name $entry.Name `
            -Value $entry.Value | Out-Null
    }

    $bootstrapUser = 'PVEAutopilot'
    $bootstrapPassword = New-OSDeployBootstrapPassword
    $userAccounts = $shellOobe.SelectSingleNode('u:UserAccounts', $ns)
    if (-not $userAccounts) {
        $userAccounts = $xml.CreateElement('UserAccounts', $unattendNs)
        $shellOobe.AppendChild($userAccounts) | Out-Null
    }
    $localAccounts = $userAccounts.SelectSingleNode('u:LocalAccounts', $ns)
    if (-not $localAccounts) {
        $localAccounts = $xml.CreateElement('LocalAccounts', $unattendNs)
        $userAccounts.AppendChild($localAccounts) | Out-Null
    }
    $localAccount = $localAccounts.SelectSingleNode("u:LocalAccount[u:Name='$bootstrapUser']", $ns)
    if (-not $localAccount) {
        $localAccount = $xml.CreateElement('LocalAccount', $unattendNs)
        [void] $localAccount.SetAttribute('action', $wcmNs, 'add')
        $localAccounts.AppendChild($localAccount) | Out-Null
    }
    Set-OSDeployUnattendTextElement -Xml $xml -Parent $localAccount -NamespaceManager $ns -Namespace $unattendNs -Name 'Name' -Value $bootstrapUser | Out-Null
    Set-OSDeployUnattendTextElement -Xml $xml -Parent $localAccount -NamespaceManager $ns -Namespace $unattendNs -Name 'Group' -Value 'Administrators' | Out-Null
    Set-OSDeployUnattendPasswordElement -Xml $xml -Parent $localAccount -NamespaceManager $ns -Namespace $unattendNs -Value $bootstrapPassword

    $autoLogon = $shellOobe.SelectSingleNode('u:AutoLogon', $ns)
    if (-not $autoLogon) {
        $autoLogon = $xml.CreateElement('AutoLogon', $unattendNs)
        $shellOobe.AppendChild($autoLogon) | Out-Null
    }
    Set-OSDeployUnattendTextElement -Xml $xml -Parent $autoLogon -NamespaceManager $ns -Namespace $unattendNs -Name 'Enabled' -Value 'true' | Out-Null
    Set-OSDeployUnattendTextElement -Xml $xml -Parent $autoLogon -NamespaceManager $ns -Namespace $unattendNs -Name 'Username' -Value $bootstrapUser | Out-Null
    Set-OSDeployUnattendPasswordElement -Xml $xml -Parent $autoLogon -NamespaceManager $ns -Namespace $unattendNs -Value $bootstrapPassword
    Set-OSDeployUnattendTextElement -Xml $xml -Parent $autoLogon -NamespaceManager $ns -Namespace $unattendNs -Name 'LogonCount' -Value '1' | Out-Null

    $xml.Save($unattendPath)
    return $unattendPath
}

function Find-OSDeployInstallImage {
    $candidates = @()
    foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
        if ($drive.Root -like 'X:\*') { continue }
        foreach ($relative in @(
            'sources\install.wim',
            'sources\install.esd',
            'install.wim',
            'install.esd'
        )) {
            $candidates += (Join-Path $drive.Root $relative)
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw 'could not locate install.wim or install.esd on OSDeploy media'
}

function Get-OSDeployFirmwareType {
    try {
        $value = (Get-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control' -Name PEFirmwareType -ErrorAction Stop).PEFirmwareType
        if ([int] $value -eq 1) { return 'BIOS' }
        if ([int] $value -eq 2) { return 'UEFI' }
    } catch {
    }
    return 'UEFI'
}

function Test-OSDeployExistingWindowsInstall {
    param(
        [string[]] $ExcludedRoots = @('X:\')
    )
    foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
        $root = [string] $drive.Root
        if ($ExcludedRoots -contains $root) { continue }
        $kernel = Join-Path $root 'Windows\System32\ntoskrnl.exe'
        if (Test-Path -LiteralPath $kernel -PathType Leaf) {
            return [pscustomobject]@{
                Found = $true
                Root = $root
                KernelPath = $kernel
            }
        }
    }
    return [pscustomobject]@{
        Found = $false
        Root = $null
        KernelPath = $null
    }
}

function Initialize-OSDeploySystemDisk {
    param(
        [int] $DiskNumber = 0,
        [ValidateSet('BIOS','UEFI')] [string] $FirmwareType = 'UEFI'
    )
    if ($FirmwareType -eq 'BIOS') {
        $diskpartScript = @"
select disk $DiskNumber
clean
convert mbr
create partition primary
format quick fs=ntfs label="Windows"
active
assign letter=W
"@
    } else {
        $diskpartScript = @"
select disk $DiskNumber
clean
convert gpt
create partition efi size=100
format quick fs=fat32 label="System"
assign letter=S
create partition msr size=16
create partition primary
format quick fs=ntfs label="Windows"
assign letter=W
"@
    }
    $scriptPath = 'X:\autopilot\osdeploy\diskpart-osdeploy.txt'
    New-Item -ItemType Directory -Path (Split-Path -Parent $scriptPath) -Force | Out-Null
    Set-Content -LiteralPath $scriptPath -Value $diskpartScript -Encoding ASCII
    & diskpart.exe /s $scriptPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "diskpart failed with exit code $LASTEXITCODE"
    }
    return [pscustomobject]@{
        WindowsDrive = 'W:'
        WindowsRoot = 'W:\Windows'
        SystemDrive = $(if ($FirmwareType -eq 'BIOS') { 'W:' } else { 'S:' })
        FirmwareType = $FirmwareType
    }
}

function Invoke-OSDeployImageApply {
    param(
        [Parameter(Mandatory)] [string] $InstallImage,
        [int] $ImageIndex = 1,
        [string] $WindowsDrive = 'W:',
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl
    )
    $applyDir = $WindowsDrive.TrimEnd('\') + '\'
    # Equivalent native command: dism.exe /Apply-Image /ImageFile:$InstallImage /Index:$ImageIndex /ApplyDir:$applyDir
    $result = Invoke-OSDeployNativeProcessWithHeartbeat `
        -FileName 'dism.exe' `
        -Arguments @('/Apply-Image', "/ImageFile:$InstallImage", "/Index:$ImageIndex", "/ApplyDir:$applyDir") `
        -Step 'apply_image' `
        -BaseUrl $BaseUrl `
        -RunId $RunId `
        -BearerToken $BearerToken `
        -FallbackBaseUrl $FallbackBaseUrl
    if ($result.ExitCode -ne 0) {
        throw "DISM image apply failed with exit code $($result.ExitCode): $($result.Stderr) $($result.Stdout)"
    }
}

function Find-OSDeployVirtIODriverRoot {
    foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
        if ($drive.Root -like 'X:\*') { continue }
        foreach ($relative in @('vioscsi', 'viostor', 'NetKVM', 'Balloon', 'qemufwcfg', 'vioser')) {
            $candidate = Join-Path $drive.Root $relative
            if (Test-Path -LiteralPath $candidate -PathType Container) {
                return $drive.Root
            }
        }
    }
    return $null
}

function Resolve-OSDeployVirtIOInf {
    param(
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [string] $InfName
    )
    $candidates = @(Get-ChildItem -Path $Root -Recurse -Filter $InfName -ErrorAction SilentlyContinue)
    if (-not $candidates) { return $null }
    foreach ($pattern in @('\2k22\amd64\', '\w11\amd64\', '\amd64\')) {
        $match = $candidates |
            Where-Object { $_.FullName -match [regex]::Escape($pattern) } |
            Sort-Object FullName |
            Select-Object -First 1
        if ($match) { return $match }
    }
    return $candidates | Sort-Object FullName | Select-Object -First 1
}

function Add-OSDeployVirtIODrivers {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl
    )
    $driverRoot = Find-OSDeployVirtIODriverRoot
    if (-not $driverRoot) {
        throw 'VirtIO driver media not found'
    }
    $imageRoot = $WindowsRoot
    if ((Split-Path -Leaf $imageRoot) -ieq 'Windows') {
        $imageRoot = Split-Path -Parent $imageRoot
    }

    $driverPaths = @()
    foreach ($infName in @('vioscsi.inf', 'viostor.inf', 'netkvm.inf', 'balloon.inf', 'qemufwcfg.inf', 'vioser.inf')) {
        $candidate = Resolve-OSDeployVirtIOInf -Root $driverRoot -InfName $infName
        if ($candidate) {
            $driverPaths += $candidate.FullName
        }
    }
    if (-not $driverPaths) {
        throw 'VirtIO driver media did not contain expected driver INF files'
    }

    foreach ($driverPath in $driverPaths) {
        # Equivalent native command: dism.exe /Image:$imageRoot /Add-Driver /Driver:$driverPath /ForceUnsigned
        $result = Invoke-OSDeployNativeProcessWithHeartbeat `
            -FileName 'dism.exe' `
            -Arguments @("/Image:$imageRoot", '/Add-Driver', "/Driver:$driverPath", '/ForceUnsigned') `
            -Step 'inject_drivers' `
            -BaseUrl $BaseUrl `
            -RunId $RunId `
            -BearerToken $BearerToken `
            -FallbackBaseUrl $FallbackBaseUrl
        if ($result.ExitCode -ne 0) {
            throw "DISM driver injection failed for $driverPath with exit code $($result.ExitCode): $($result.Stderr) $($result.Stdout)"
        }
    }
    return [pscustomobject]@{
        Applied = $true
        DriverRoot = $driverRoot
        ImageRoot = $imageRoot
        DriverPaths = $driverPaths
        Message = 'VirtIO drivers applied'
    }
}

function Invoke-OSDeployBootFiles {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $SystemDrive = 'S:',
        [ValidateSet('BIOS','UEFI')] [string] $FirmwareType = 'UEFI',
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl
    )
    $bootMode = $(if ($FirmwareType -eq 'BIOS') { 'BIOS' } else { 'UEFI' })
    $result = Invoke-OSDeployNativeProcessWithHeartbeat `
        -FileName 'bcdboot.exe' `
        -Arguments @($WindowsRoot, '/s', $SystemDrive, '/f', $bootMode) `
        -Step 'bcdboot' `
        -BaseUrl $BaseUrl `
        -RunId $RunId `
        -BearerToken $BearerToken `
        -FallbackBaseUrl $FallbackBaseUrl
    if ($result.ExitCode -ne 0) {
        throw "bcdboot failed with exit code $($result.ExitCode): $($result.Stderr) $($result.Stdout)"
    }
}

function Stop-OSDeployWinPEForDiskBoot {
    & wpeutil.exe shutdown
    if ($LASTEXITCODE -eq 0) { return }

    Write-Warning "wpeutil shutdown failed with exit code $LASTEXITCODE; falling back to Stop-Computer."
    Stop-Computer -Force
}

function Invoke-OSDeployBridge {
    param([string] $ConfigPath = (Join-Path $PSScriptRoot 'config.json'))

    $config = Read-OSDeployConfig -Path $ConfigPath
    $baseUrl = [string] $config.flask_base_url
    $fallbackUrl = [string] $config.fallback_base_url
    $identity = Get-OSDeployVMIdentity
    $register = Invoke-OSDeployRequest -BaseUrl $baseUrl `
        -FallbackBaseUrl $fallbackUrl `
        -Path '/api/osdeploy/v1/pe/register' `
        -Method POST `
        -Body @{
            vm_uuid = $identity.vm_uuid
            mac = $identity.mac
            architecture = $config.architecture
            build_sha = $config.build_sha
            client_version = '0.1.0'
        }
    $token = [string] $register.bearer_token
    $runId = [string] $register.run_id
    Write-OSDeployConsoleStep -Step 'register' -State 'ok' -Message "run_id=$runId"

    $stepArgs = @{
        BaseUrl = $baseUrl
        FallbackBaseUrl = $fallbackUrl
        RunId = $runId
        BearerToken = $token
    }

    $package = Invoke-OSDeployPeStep @stepArgs -Name 'package' -ScriptBlock {
        Invoke-OSDeployRequest -BaseUrl $baseUrl `
            -FallbackBaseUrl $fallbackUrl `
            -Path "/api/osdeploy/v1/pe/package/$runId" `
            -Method GET `
            -BearerToken $token
    }

    $stageRoot = Invoke-OSDeployPeStep @stepArgs -Name 'stage_package' -ScriptBlock {
        Save-OSDeployRunPackage -Package $package -BearerToken $token
    }
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_pe_package_staged' `
        -Message 'OSDeploy run package staged in WinPE' `
        -Data @{ staged_root = $stageRoot }

    $imageSelection = Invoke-OSDeployPeStep @stepArgs -Name 'locate_image' -ScriptBlock {
        $installImage = Find-OSDeployInstallImage
        $artifact = Get-OSDeployObjectProperty -Value $package -Name 'artifact'
        $imageIndex = [int] (Get-OSDeployObjectProperty -Value $artifact -Name 'apply_image_index')
        if ($imageIndex -lt 1) {
            $imageIndex = [int] (Get-OSDeployObjectProperty -Value $artifact -Name 'output_image_index')
        }
        if ($imageIndex -lt 1) {
            $imageIndex = [int] (Get-OSDeployObjectProperty -Value $artifact -Name 'image_index')
        }
        if ($imageIndex -lt 1) { $imageIndex = 1 }
        [pscustomobject]@{
            InstallImage = $installImage
            ImageIndex = $imageIndex
        }
    }
    $installImage = $imageSelection.InstallImage
    $imageIndex = [int] $imageSelection.ImageIndex
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_install_image_found' `
        -Message 'OSDeploy install image located' `
        -Data @{ install_image = $installImage; image_index = $imageIndex }

    Invoke-OSDeployPeStep @stepArgs -Name 'guard_existing_windows' -ScriptBlock {
        $existingWindows = Test-OSDeployExistingWindowsInstall
        if ($existingWindows.Found) {
            Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
                -RunId $runId -BearerToken $token -Phase 'pe' `
                -EventType 'osdeploy_existing_windows_guard_blocked' `
                -Severity 'error' `
                -Message 'Existing Windows installation detected; refusing to clean disk' `
                -Data @{
                    root = $existingWindows.Root
                    kernel_path = $existingWindows.KernelPath
                }
            throw "existing Windows installation detected at $($existingWindows.Root); refusing to clean disk"
        }
    } | Out-Null

    $firmwareType = Get-OSDeployFirmwareType
    $disk = Invoke-OSDeployPeStep @stepArgs -Name 'partition' -ScriptBlock {
        Initialize-OSDeploySystemDisk -FirmwareType $firmwareType
    }
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_disk_partitioned' `
        -Message 'OSDeploy target disk partitioned' `
        -Data @{
            windows_drive = $disk.WindowsDrive
            system_drive = $disk.SystemDrive
            firmware_type = $disk.FirmwareType
        }

    Invoke-OSDeployPeStep @stepArgs -Name 'apply_image' -ScriptBlock {
        Invoke-OSDeployImageApply `
            -InstallImage $installImage `
            -ImageIndex $imageIndex `
            -WindowsDrive $disk.WindowsDrive `
            -BaseUrl $baseUrl `
            -RunId $runId `
            -BearerToken $token `
            -FallbackBaseUrl $fallbackUrl
    } | Out-Null
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_image_applied' `
        -Message 'OSDeploy Server image applied' `
        -Data @{ windows_root = $disk.WindowsRoot; image_index = $imageIndex }

    $driverResult = Invoke-OSDeployPeStep @stepArgs -Name 'inject_drivers' -ScriptBlock {
        Add-OSDeployVirtIODrivers `
            -WindowsRoot $disk.WindowsRoot `
            -BaseUrl $baseUrl `
            -RunId $runId `
            -BearerToken $token `
            -FallbackBaseUrl $fallbackUrl
    }
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_drivers_applied' `
        -Message $driverResult.Message `
        -Data @{
            applied = $driverResult.Applied
            driver_root = $driverResult.DriverRoot
            driver_paths = @($driverResult.DriverPaths)
        }

    $osdRoot = Invoke-OSDeployPeStep @stepArgs -Name 'stage_osd_client' -ScriptBlock {
        Save-OSDeployOsdClientPackage `
            -Package $package `
            -WindowsRoot $disk.WindowsRoot `
            -BearerToken $token `
            -BaseUrl $baseUrl `
            -RunId $runId `
            -FallbackBaseUrl $fallbackUrl
    }
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_setupcomplete_staged' `
        -Message 'OSDeploy v2 SetupComplete.cmd and full-OS client staged' `
        -Data @{ osd_root = $osdRoot; setupcomplete = (Join-Path $disk.WindowsRoot 'Setup\Scripts\SetupComplete.cmd') }

    $packageIdentity = Get-OSDeployObjectProperty -Value $package -Name 'identity'
    $packageServerSettings = Get-OSDeployObjectProperty -Value $package -Name 'server_settings'
    $packageArtifact = Get-OSDeployObjectProperty -Value $package -Name 'artifact'
    $unattendPath = Invoke-OSDeployPeStep @stepArgs -Name 'stage_unattend' -ScriptBlock {
        Add-OSDeployOfflineUnattend `
            -WindowsRoot $disk.WindowsRoot `
            -ComputerName (Get-OSDeployObjectProperty -Value $packageIdentity -Name 'computer_name') `
            -Locale (Get-OSDeployObjectProperty -Value $packageServerSettings -Name 'os_language') `
            -Version (Get-OSDeployObjectProperty -Value $packageServerSettings -Name 'os_version') `
            -Edition (Get-OSDeployObjectProperty -Value $packageServerSettings -Name 'os_edition') `
            -ImageName (Get-OSDeployObjectProperty -Value $packageArtifact -Name 'image_name')
    }
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_unattend_staged' `
        -Message 'OSDeploy offline unattend staged for first boot' `
        -Data @{ unattend = $unattendPath }

    Invoke-OSDeployPeStep @stepArgs -Name 'bcdboot' -ScriptBlock {
        Invoke-OSDeployBootFiles `
            -WindowsRoot $disk.WindowsRoot `
            -SystemDrive $disk.SystemDrive `
            -FirmwareType $disk.FirmwareType `
            -BaseUrl $baseUrl `
            -RunId $runId `
            -BearerToken $token `
            -FallbackBaseUrl $fallbackUrl
    } | Out-Null
    Write-OSDeployEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
        -RunId $runId -BearerToken $token -Phase 'pe' `
        -EventType 'osdeploy_boot_files_staged' `
        -Message 'OSDeploy boot files staged; stopping WinPE for disk boot handoff' `
        -Data @{ windows_root = $disk.WindowsRoot; system_drive = $disk.SystemDrive }

    Invoke-OSDeployPeStep @stepArgs -Name 'handoff' -ScriptBlock {
        Stop-OSDeployWinPEForDiskBoot
    } | Out-Null
}

if ($env:OSDEPLOY_BRIDGE_LIBRARY_ONLY -ne '1') {
    Invoke-OSDeployBridge
}
