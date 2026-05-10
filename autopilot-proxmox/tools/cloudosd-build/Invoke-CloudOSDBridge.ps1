# CloudOSD WinPE bridge for ProxmoxVEAutopilot.
#
# This script is baked into the static CloudOSD PE ISO. It registers the VM
# identity with ProxmoxVEAutopilot, receives a run-scoped package, generates an
# OSDCloud workflow, runs OSDCloud in CLI mode, stages first-boot payloads, and
# chains SetupComplete without overwriting OSDCloud's own content.

$ErrorActionPreference = 'Stop'

function Read-CloudOSDConfig {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "CloudOSD config not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Resolve-CloudOSDMacAddress {
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

function Get-CloudOSDVMIdentity {
    $system = Get-CimInstance Win32_ComputerSystem
    $product = Get-CimInstance Win32_ComputerSystemProduct
    $uuid = [string] $product.UUID
    if ([string]::IsNullOrWhiteSpace($uuid)) {
        throw 'could not read SMBIOS UUID'
    }
    return [pscustomobject]@{
        vm_uuid = $uuid.ToLowerInvariant()
        mac = (Resolve-CloudOSDMacAddress)
        manufacturer = [string] $system.Manufacturer
        model = [string] $system.Model
    }
}

function Invoke-CloudOSDRequest {
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

function Write-CloudOSDEvent {
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
        Invoke-CloudOSDRequest -BaseUrl $BaseUrl `
            -FallbackBaseUrl $FallbackBaseUrl `
            -Path "/api/cloudosd/runs/$RunId/events" `
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
        Write-Warning "failed to report CloudOSD event ${EventType}: $($_.Exception.Message)"
    }
}

function ConvertTo-CloudOSDHashtable {
    param([AllowNull()] [object] $Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $out = @{}
        foreach ($key in $Value.Keys) { $out[$key] = ConvertTo-CloudOSDHashtable $Value[$key] }
        return $out
    }
    if ($Value -is [pscustomobject]) {
        $out = @{}
        foreach ($prop in $Value.PSObject.Properties) {
            $out[$prop.Name] = ConvertTo-CloudOSDHashtable $prop.Value
        }
        return $out
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        $items = @()
        foreach ($item in $Value) { $items += ConvertTo-CloudOSDHashtable $item }
        return $items
    }
    return $Value
}

function Get-CloudOSDWorkflowTask {
    param(
        [Parameter(Mandatory)] [string] $ModuleRoot,
        [Parameter(Mandatory)] [object] $Task
    )
    if ($Task.PSObject.Properties.Match('steps').Count -gt 0 -and $Task.steps) {
        return Set-CloudOSDWorkflowTaskUnattendedDefaults -Task $Task
    }
    $taskName = [string] $Task.name
    if ([string]::IsNullOrWhiteSpace($taskName)) { $taskName = 'osdcloud-nofirmware' }
    $defaultTask = Join-Path $ModuleRoot "workflow\default\tasks\$taskName.json"
    if (-not (Test-Path -LiteralPath $defaultTask)) {
        throw "OSDCloud default workflow task not found: $defaultTask"
    }
    $workflowTask = Get-Content -LiteralPath $defaultTask -Raw | ConvertFrom-Json
    return Set-CloudOSDWorkflowTaskUnattendedDefaults -Task $workflowTask
}

function Set-CloudOSDWorkflowTaskUnattendedDefaults {
    param([Parameter(Mandatory)] [object] $Task)

    if ($Task.PSObject.Properties.Match('steps').Count -eq 0 -or -not $Task.steps) {
        return $Task
    }

    $skipDriverCommands = @(
        'step-Export-WindowsDriver-OemWinPE',
        'step-Add-WindowsDriver-OemWinOS',
        'step-Add-WindowsDriver-OemWinRE',
        'step-Save-WindowsDriver-Firmware',
        'step-Add-WindowsDriver-Firmware',
        'step-Save-WindowsDriver-DriverPack',
        'step-Add-WindowsDriver-DriverPack',
        'step-Save-WindowsDriver-MSUpdate',
        'step-Add-WindowsDriver-MSUpdate',
        'step-Add-WindowsDriver-Disk',
        'step-Add-WindowsDriver-Net',
        'step-Add-WindowsDriver-Scsi',
        'step-powershell-updatemodule'
    )
    foreach ($step in @($Task.steps)) {
        $command = [string] $step.command
        if ($command -ieq 'step-preinstall-cleartargetdisk') {
            if ($step.PSObject.Properties.Match('parameters').Count -eq 0 -or $null -eq $step.parameters) {
                $step | Add-Member -MemberType NoteProperty -Name parameters -Value ([pscustomobject]@{}) -Force
            }
            if ($step.parameters -is [hashtable]) {
                $step.parameters['Confirm'] = $false
            } else {
                $step.parameters | Add-Member -MemberType NoteProperty -Name Confirm -Value $false -Force
            }
        }
        if ($skipDriverCommands -icontains $command) {
            $step | Add-Member -MemberType NoteProperty -Name skip -Value $true -Force
        }
    }

    return $Task
}

function New-CloudOSDWorkflow {
    param(
        [Parameter(Mandatory)] [string] $ModuleRoot,
        [Parameter(Mandatory)] [object] $Package,
        [string] $Architecture = 'amd64'
    )
    $workflowName = [string] $Package.workflow_name
    if ([string]::IsNullOrWhiteSpace($workflowName)) {
        throw 'CloudOSD package is missing workflow_name'
    }
    $workflowPath = Join-Path $ModuleRoot "workflow\$workflowName"
    $tasksPath = Join-Path $workflowPath 'tasks'
    New-Item -ItemType Directory -Path $tasksPath -Force | Out-Null

    $osPath = Join-Path $workflowPath "os-$Architecture.json"
    $userPath = Join-Path $workflowPath "user-$Architecture.json"
    $taskName = [string] $Package.task.name
    if ([string]::IsNullOrWhiteSpace($taskName)) { $taskName = 'osdcloud-nofirmware' }
    $taskPath = Join-Path $tasksPath "$taskName.json"

    ConvertTo-CloudOSDHashtable $Package.os_settings |
        ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath $osPath -Encoding UTF8
    ConvertTo-CloudOSDHashtable $Package.user_settings |
        ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath $userPath -Encoding UTF8
    Get-CloudOSDWorkflowTask -ModuleRoot $ModuleRoot -Task $Package.task |
        ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath $taskPath -Encoding UTF8

    return $workflowPath
}

function Resolve-OSDCloudModuleRoot {
    param([string] $ExpectedVersion)
    $module = Get-Module -ListAvailable OSDCloud |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if (-not $module) { throw 'OSDCloud module is not available in WinPE' }
    if ($ExpectedVersion -and $module.Version.ToString() -ne $ExpectedVersion) {
        throw "OSDCloud module version mismatch. Expected $ExpectedVersion, found $($module.Version)"
    }
    return $module.ModuleBase
}

function Import-CloudOSDModule {
    param([string] $ExpectedVersion)
    $module = Get-Module -ListAvailable OSDCloud |
        Sort-Object Version -Descending |
        Select-Object -First 1
    if (-not $module) { throw 'OSDCloud module is not available in WinPE' }
    if ($ExpectedVersion -and $module.Version.ToString() -ne $ExpectedVersion) {
        throw "OSDCloud module version mismatch. Expected $ExpectedVersion, found $($module.Version)"
    }
    Import-Module -Name $module.Path -Force -ErrorAction Stop
    if (-not (Get-Command Deploy-OSDCloud -ErrorAction SilentlyContinue)) {
        throw 'Deploy-OSDCloud command is not available after importing OSDCloud'
    }
    return $module.ModuleBase
}

function Get-CloudOSDWindowsRoot {
    foreach ($drive in @('C','D','E','F','G','H','V','W')) {
        $candidate = "$($drive):\Windows"
        if (Test-Path -LiteralPath (Join-Path $candidate 'System32\Config\SOFTWARE')) {
            return $candidate
        }
    }
    throw 'could not locate offline Windows volume'
}

function Get-OfflineProgramDataPath {
    param([Parameter(Mandatory)] [string] $WindowsRoot)
    $root = [System.IO.Path]::GetPathRoot($WindowsRoot)
    if ($root -eq '/' -or $root -eq '\') {
        return Join-Path (Split-Path -Parent $WindowsRoot) 'ProgramData'
    }
    return Join-Path $root 'ProgramData'
}

function Save-CloudOSDPayload {
    param(
        [Parameter(Mandatory)] [object] $Payload,
        [Parameter(Mandatory)] [string] $Destination,
        [string] $BearerToken,
        [scriptblock] $Downloader = {
            param($Url,$OutFile,$Headers)
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile -Headers $Headers -TimeoutSec 300
        }
    )
    if ($Payload.PSObject.Properties.Match('url').Count -eq 0 -or -not $Payload.url) {
        throw "payload is missing url for $Destination"
    }
    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    & $Downloader ([string] $Payload.url) $Destination $headers
    if ($Payload.PSObject.Properties.Match('sha256').Count -gt 0 -and $Payload.sha256) {
        $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne ([string] $Payload.sha256).ToLowerInvariant()) {
            throw "SHA256 mismatch for $Destination"
        }
    }
    if ($Payload.PSObject.Properties.Match('local_path').Count -eq 0) {
        Add-Member -InputObject $Payload -NotePropertyName local_path -NotePropertyValue $Destination -Force
    } else {
        $Payload.local_path = $Destination
    }
}

function Save-CloudOSDRunPackage {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string] $BridgeRoot,
        [string] $BearerToken,
        [scriptblock] $Downloader
    )
    $stageRoot = Join-Path (Get-OfflineProgramDataPath -WindowsRoot $WindowsRoot) 'ProxmoxVEAutopilot\CloudOSD'
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

    $firstBootSource = Join-Path $BridgeRoot 'PVEAutopilot-FirstBoot.ps1'
    Copy-Item -LiteralPath $firstBootSource -Destination (Join-Path $stageRoot 'PVEAutopilot-FirstBoot.ps1') -Force
    if ($Package.payloads.autopilotagent_msi) {
        $saveArgs = @{
            Payload = $Package.payloads.autopilotagent_msi
            Destination = (Join-Path $stageRoot 'AutopilotAgent.msi')
            BearerToken = $BearerToken
        }
        if ($Downloader) { $saveArgs.Downloader = $Downloader }
        Save-CloudOSDPayload @saveArgs
    }
    if ($Package.payloads.autopilotagent_postinstall) {
        $saveArgs = @{
            Payload = $Package.payloads.autopilotagent_postinstall
            Destination = (Join-Path $stageRoot 'autopilotagent-postinstall.ps1')
            BearerToken = $BearerToken
        }
        if ($Downloader) { $saveArgs.Downloader = $Downloader }
        Save-CloudOSDPayload @saveArgs
    }
    if ($Package.payloads.first_boot_script) {
        $Package.payloads.first_boot_script |
            Add-Member -NotePropertyName local_path `
                -NotePropertyValue (Join-Path $stageRoot 'PVEAutopilot-FirstBoot.ps1') `
                -Force
    }

    $runJson = Join-Path $stageRoot 'cloudosd-run.json'
    $Package | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $runJson -Encoding UTF8
    return $stageRoot
}

function Add-PVEAutopilotSetupCompleteChain {
    param([Parameter(Mandatory)] [string] $WindowsRoot)
    $scriptsRoot = Join-Path $WindowsRoot 'Setup\Scripts'
    New-Item -ItemType Directory -Path $scriptsRoot -Force | Out-Null

    $setupScript = Join-Path $scriptsRoot 'PVEAutopilot-SetupComplete.cmd'
    $setupScriptContent = @'
@echo off
set TASK_NAME=PVEAutopilot-CloudOSD-FirstBoot
set SCRIPT=C:\ProgramData\ProxmoxVEAutopilot\CloudOSD\PVEAutopilot-FirstBoot.ps1
schtasks /Create /F /TN "%TASK_NAME%" /SC MINUTE /MO 1 /RU SYSTEM /RL HIGHEST /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%SCRIPT%\""
schtasks /Run /TN "%TASK_NAME%"
exit /b 0
'@
    Set-Content -LiteralPath $setupScript -Value $setupScriptContent -Encoding ASCII

    $setupComplete = Join-Path $scriptsRoot 'SetupComplete.cmd'
    if (-not (Test-Path -LiteralPath $setupComplete)) {
        '@echo off' | Set-Content -LiteralPath $setupComplete -Encoding ASCII
    }
    $content = Get-Content -LiteralPath $setupComplete -Raw
    if ($content -notmatch 'PVEAUTOPILOT-CLOUDOSD-SENTINEL') {
        $append = @'

rem PVEAUTOPILOT-CLOUDOSD-SENTINEL
call "%SystemRoot%\Setup\Scripts\PVEAutopilot-SetupComplete.cmd"
'@
        Add-Content -LiteralPath $setupComplete -Value $append -Encoding ASCII
    }
}

function ConvertTo-PVEAutopilotWindowsComputerName {
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

function Add-PVEAutopilotSpecializeUnattend {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $ComputerName
    )

    $pantherRoot = Join-Path $WindowsRoot 'Panther'
    New-Item -ItemType Directory -Path $pantherRoot -Force | Out-Null
    $unattendPath = Join-Path $pantherRoot 'Unattend.xml'
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
    $rootElement.SetAttribute('xmlns:wcm', $wcmNs)

    $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $ns.AddNamespace('u', $unattendNs)
    $ns.AddNamespace('wcm', $wcmNs)

    $settings = $rootElement.SelectSingleNode("u:settings[@pass='specialize']", $ns)
    if (-not $settings) {
        $settings = $xml.CreateElement('settings', $unattendNs)
        $settings.SetAttribute('pass', 'specialize')
        $rootElement.AppendChild($settings) | Out-Null
    }

    $component = $settings.SelectSingleNode("u:component[@name='Microsoft-Windows-Deployment' and @processorArchitecture='amd64']", $ns)
    if (-not $component) {
        $component = $xml.CreateElement('component', $unattendNs)
        $component.SetAttribute('name', 'Microsoft-Windows-Deployment')
        $component.SetAttribute('processorArchitecture', 'amd64')
        $component.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        $component.SetAttribute('language', 'neutral')
        $component.SetAttribute('versionScope', 'nonSxS')
        $settings.AppendChild($component) | Out-Null
    }

    $runSync = $component.SelectSingleNode('u:RunSynchronous', $ns)
    if (-not $runSync) {
        $runSync = $xml.CreateElement('RunSynchronous', $unattendNs)
        $component.AppendChild($runSync) | Out-Null
    }

    $sentinelPath = 'C:\Windows\Setup\Scripts\PVEAutopilot-SetupComplete.cmd'
    $existingCommand = $runSync.SelectSingleNode(".//u:Path[contains(text(),'PVEAutopilot-SetupComplete.cmd')]", $ns)
    if (-not $existingCommand) {
        $order = 1
        $existingOrders = $runSync.SelectNodes('u:RunSynchronousCommand/u:Order', $ns)
        foreach ($existingOrder in $existingOrders) {
            $parsed = 0
            if ([int]::TryParse($existingOrder.InnerText, [ref] $parsed) -and $parsed -ge $order) {
                $order = $parsed + 1
            }
        }

        $command = $xml.CreateElement('RunSynchronousCommand', $unattendNs)
        $command.SetAttribute('action', $wcmNs, 'add')
        foreach ($entry in @(
            @{ Name = 'Order'; Value = [string] $order },
            @{ Name = 'Description'; Value = 'Start ProxmoxVEAutopilot CloudOSD first boot task' },
            @{ Name = 'Path'; Value = "cmd.exe /c $sentinelPath" }
        )) {
            $node = $xml.CreateElement($entry.Name, $unattendNs)
            $node.InnerText = $entry.Value
            $command.AppendChild($node) | Out-Null
        }
        $runSync.AppendChild($command) | Out-Null
    }

    $windowsComputerName = ConvertTo-PVEAutopilotWindowsComputerName -Name $ComputerName
    if ($windowsComputerName) {
        $shellComponent = $settings.SelectSingleNode("u:component[@name='Microsoft-Windows-Shell-Setup' and @processorArchitecture='amd64']", $ns)
        if (-not $shellComponent) {
            $shellComponent = $xml.CreateElement('component', $unattendNs)
            $shellComponent.SetAttribute('name', 'Microsoft-Windows-Shell-Setup')
            $shellComponent.SetAttribute('processorArchitecture', 'amd64')
            $shellComponent.SetAttribute('publicKeyToken', '31bf3856ad364e35')
            $shellComponent.SetAttribute('language', 'neutral')
            $shellComponent.SetAttribute('versionScope', 'nonSxS')
            $settings.AppendChild($shellComponent) | Out-Null
        }
        $computerNameNode = $shellComponent.SelectSingleNode('u:ComputerName', $ns)
        if (-not $computerNameNode) {
            $computerNameNode = $xml.CreateElement('ComputerName', $unattendNs)
            $shellComponent.AppendChild($computerNameNode) | Out-Null
        }
        $computerNameNode.InnerText = $windowsComputerName
    }

    $writerSettings = New-Object System.Xml.XmlWriterSettings
    $writerSettings.Encoding = New-Object System.Text.UTF8Encoding($false)
    $writerSettings.Indent = $true
    $writer = [System.Xml.XmlWriter]::Create($unattendPath, $writerSettings)
    try {
        $xml.Save($writer)
    } finally {
        $writer.Close()
    }
}

function Get-PVEAutopilotPackageComputerName {
    param([Parameter(Mandatory)] [object] $Package)
    if ($Package.PSObject.Properties.Match('identity').Count -gt 0 -and $Package.identity) {
        if ($Package.identity.PSObject.Properties.Match('computer_name').Count -gt 0 -and $Package.identity.computer_name) {
            return [string] $Package.identity.computer_name
        }
    }
    if ($Package.PSObject.Properties.Match('computer_name').Count -gt 0 -and $Package.computer_name) {
        return [string] $Package.computer_name
    }
    return $null
}

function Add-PVEAutopilotSetupSpecializePackage {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string] $ModuleRoot
    )

    $setupSpecializeRoot = Join-Path $WindowsRoot 'Temp\osdcloud'
    New-Item -ItemType Directory -Path $setupSpecializeRoot -Force | Out-Null
    $setupSpecializeCmd = Join-Path $setupSpecializeRoot 'SetupSpecialize.cmd'
    $content = @'
@echo off
echo [%date% %time%] Starting ProxmoxVEAutopilot CloudOSD first boot task>>C:\Windows\Temp\osdcloud\PVEAutopilot-SetupSpecialize.log
call C:\Windows\Setup\Scripts\PVEAutopilot-SetupComplete.cmd>>C:\Windows\Temp\osdcloud\PVEAutopilot-SetupSpecialize.log 2>>&1
exit /b 0
'@
    Set-Content -LiteralPath $setupSpecializeCmd -Value $content -Encoding ASCII

    $package = Join-Path $ModuleRoot 'core\setupspecialize\setupspecialize.ppkg'
    if (-not (Test-Path -LiteralPath $package)) {
        throw "OSDCloud SetupSpecialize provisioning package not found: $package"
    }
    $imageRoot = Split-Path -Parent $WindowsRoot
    $argumentList = "/Image:$imageRoot /Add-ProvisioningPackage /PackagePath:`"$package`""
    $process = Start-Process -FilePath 'dism.exe' -ArgumentList $argumentList -Wait -NoNewWindow -PassThru
    if ($process.ExitCode -ne 0) {
        throw "DISM failed to add OSDCloud SetupSpecialize provisioning package: $($process.ExitCode)"
    }
}

function Disable-PVEAutopilotAutomaticDeviceEncryption {
    param([Parameter(Mandatory)] [string] $WindowsRoot)

    $systemHive = Join-Path $WindowsRoot 'System32\Config\SYSTEM'
    if (-not (Test-Path -LiteralPath $systemHive)) {
        throw "Offline SYSTEM registry hive not found: $systemHive"
    }

    $mountName = 'HKLM\PVEAutopilotSYSTEM'
    & reg.exe load $mountName $systemHive | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to load offline SYSTEM registry hive: $LASTEXITCODE"
    }
    try {
        $controlSets = @()
        $queryOutput = & reg.exe query $mountName 2>$null
        foreach ($line in $queryOutput) {
            if ($line -match '\\(ControlSet\d{3})$') {
                $controlSets += $matches[1]
            }
        }
        if (-not $controlSets) { $controlSets = @('ControlSet001') }
        foreach ($controlSet in ($controlSets | Select-Object -Unique)) {
            & reg.exe add "$mountName\$controlSet\Control\BitLocker" `
                /v PreventDeviceEncryption /t REG_DWORD /d 1 /f | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to disable automatic device encryption in $controlSet`: $LASTEXITCODE"
            }
        }
    } finally {
        & reg.exe unload $mountName | Out-Null
    }
}

function Test-CloudOSDOfflineWindows {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $EfiRoot
    )
    $errors = @()
    if (-not (Test-Path -LiteralPath $WindowsRoot)) {
        $errors += "Windows volume not found: $WindowsRoot"
    }
    $storageDriver = Test-CloudOSDOfflineDriverPresent -WindowsRoot $WindowsRoot `
        -InfNames @('vioscsi.inf', 'viostor.inf') `
        -DriverFiles @('vioscsi.sys', 'viostor.sys')
    if (-not $storageDriver) { $errors += 'VirtIO storage driver is missing from offline OS' }
    $networkDriver = Test-CloudOSDOfflineDriverPresent -WindowsRoot $WindowsRoot `
        -InfNames @('netkvm.inf') `
        -DriverFiles @('netkvm.sys')
    if (-not $networkDriver) {
        $errors += 'VirtIO network driver is missing from offline OS'
    }
    $setupComplete = Join-Path $WindowsRoot 'Setup\Scripts\SetupComplete.cmd'
    if (-not (Test-Path -LiteralPath $setupComplete)) {
        $errors += 'SetupComplete chain is missing'
    } elseif ((Get-Content -LiteralPath $setupComplete -Raw) -notmatch 'PVEAUTOPILOT-CLOUDOSD-SENTINEL') {
        $errors += 'SetupComplete sentinel is missing'
    }
    $unattendPath = Join-Path $WindowsRoot 'Panther\Unattend.xml'
    if (-not (Test-Path -LiteralPath $unattendPath)) {
        $errors += 'Specialize unattend chain is missing'
    } elseif ((Get-Content -LiteralPath $unattendPath -Raw) -notmatch 'PVEAutopilot-SetupComplete\.cmd') {
        $errors += 'Specialize unattend chain does not start ProxmoxVEAutopilot first boot'
    }
    $setupSpecializeCmd = Join-Path $WindowsRoot 'Temp\osdcloud\SetupSpecialize.cmd'
    if (-not (Test-Path -LiteralPath $setupSpecializeCmd)) {
        $errors += 'OSDCloud SetupSpecialize command is missing'
    } elseif ((Get-Content -LiteralPath $setupSpecializeCmd -Raw) -notmatch 'PVEAutopilot-SetupComplete\.cmd') {
        $errors += 'OSDCloud SetupSpecialize command does not start ProxmoxVEAutopilot first boot'
    }
    $runJson = Join-Path (Get-OfflineProgramDataPath -WindowsRoot $WindowsRoot) 'ProxmoxVEAutopilot\CloudOSD\cloudosd-run.json'
    if (-not (Test-Path -LiteralPath $runJson)) {
        $errors += 'CloudOSD run package is not staged in offline OS'
    }
    if ($EfiRoot) {
        $bcd = Join-Path $EfiRoot 'EFI\Microsoft\Boot\BCD'
        if (-not (Test-Path -LiteralPath $bcd)) { $errors += 'EFI BCD boot files are missing' }
    }
    return [pscustomobject]@{
        ok = ($errors.Count -eq 0)
        errors = $errors
    }
}

function Test-CloudOSDOfflineDriverPresent {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string[]] $InfNames,
        [Parameter(Mandatory)] [string[]] $DriverFiles
    )
    $driversPath = Join-Path $WindowsRoot 'System32\drivers'
    foreach ($driverFile in $DriverFiles) {
        if (Test-Path -LiteralPath (Join-Path $driversPath $driverFile)) {
            return $true
        }
    }

    $driverStore = Join-Path $WindowsRoot 'System32\DriverStore\FileRepository'
    if (-not (Test-Path -LiteralPath $driverStore)) { return $false }
    foreach ($infName in $InfNames) {
        $dirs = Get-ChildItem -LiteralPath $driverStore -Directory -Filter "$infName*" -ErrorAction SilentlyContinue
        foreach ($dir in $dirs) {
            $hasInf = Test-Path -LiteralPath (Join-Path $dir.FullName $infName)
            $hasSys = $false
            foreach ($driverFile in $DriverFiles) {
                if (Test-Path -LiteralPath (Join-Path $dir.FullName $driverFile)) {
                    $hasSys = $true
                    break
                }
            }
            if ($hasInf -and $hasSys) { return $true }
        }
    }
    return $false
}

function Get-CloudOSDVirtioSearchRoots {
    $roots = @()
    try {
        $cdroms = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 5' -ErrorAction Stop
        foreach ($disk in $cdroms) {
            if ($disk.DeviceID) { $roots += "$($disk.DeviceID)\" }
        }
    } catch {
        foreach ($drive in Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue) {
            if ($drive.Root -and $drive.Root -ne 'C:\') { $roots += $drive.Root }
        }
    }
    $driverStore = 'X:\Windows\System32\DriverStore\FileRepository'
    if (Test-Path -LiteralPath $driverStore) { $roots += $driverStore }
    return ($roots | Select-Object -Unique)
}

function Get-CloudOSDVirtioOsKey {
    param([object] $Package)
    if (-not $Package -or $Package.PSObject.Properties.Match('os_settings').Count -eq 0) {
        return ''
    }
    $settings = $Package.os_settings
    if (-not $settings -or $settings.PSObject.Properties.Match('OperatingSystem').Count -eq 0) {
        return ''
    }
    $osValue = [string] $settings.OperatingSystem.default
    if ($osValue -match 'Windows\s+10') { return 'w10' }
    if ($osValue -match 'Windows\s+11') { return 'w11' }
    return ''
}

function Resolve-CloudOSDVirtioInf {
    param(
        [Parameter(Mandatory)] [string] $InfName,
        [string] $PreferredOsKey = '',
        [string[]] $SearchRoots = $(Get-CloudOSDVirtioSearchRoots)
    )
    $matches = @()
    foreach ($root in $SearchRoots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $matches += Get-ChildItem -LiteralPath $root -Filter $InfName -Recurse -ErrorAction SilentlyContinue
    }
    $preferredKey = $PreferredOsKey.ToLowerInvariant()
    $preferred = $matches |
        Sort-Object -Property @{
            Expression = {
                $path = $_.FullName.ToLowerInvariant()
                if ($preferredKey -and $path -match "\\$preferredKey\\" -and $path -match '\\amd64\\') { 0 }
                elseif ($preferredKey -and $path -match "\\$preferredKey\\" -and $path -match '\\x64\\') { 1 }
                elseif ($preferredKey -and $path -match "\\$preferredKey\\") { 2 }
                elseif ($path -match '\\w11\\' -and $path -match '\\amd64\\') { 3 }
                elseif ($path -match '\\w10\\' -and $path -match '\\amd64\\') { 4 }
                elseif ($path -match '\\amd64\\') { 5 }
                elseif ($path -match '\\x64\\') { 6 }
                else { 7 }
            }
        }, FullName |
        Select-Object -First 1
    return $preferred
}

function Add-CloudOSDOfflineVirtIODrivers {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $PreferredOsKey = '',
        [string[]] $SearchRoots = $(Get-CloudOSDVirtioSearchRoots)
    )
    $imageRoot = Split-Path -Parent $WindowsRoot
    foreach ($infName in @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')) {
        $inf = Resolve-CloudOSDVirtioInf `
            -InfName $infName `
            -PreferredOsKey $PreferredOsKey `
            -SearchRoots $SearchRoots
        if (-not $inf) { throw "VirtIO driver INF not found for offline injection: $infName" }
        & dism.exe /Image:$imageRoot /Add-Driver /Driver:$($inf.FullName) /ForceUnsigned | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "DISM failed to add $infName to offline Windows image: $LASTEXITCODE"
        }
    }
}

function Invoke-CloudOSDDeploy {
    param([Parameter(Mandatory)] [string] $WorkflowName)
    Deploy-OSDCloud -WorkflowName $WorkflowName -CLI
}

function Stop-CloudOSDForControllerHandoff {
    Write-Host 'CloudOSD PE phase is complete. Shutting down for controller media detach and disk boot.'
    $wpeutil = Get-Command wpeutil.exe -ErrorAction SilentlyContinue
    if ($wpeutil) {
        & $wpeutil.Source shutdown
        return
    }
    Stop-Computer -Force
}

function Invoke-CloudOSDBridge {
    param([string] $ConfigPath = (Join-Path $PSScriptRoot 'config.json'))

    $config = Read-CloudOSDConfig -Path $ConfigPath
    $baseUrl = [string] $config.flask_base_url
    $fallbackUrl = [string] $config.fallback_base_url
    $identity = Get-CloudOSDVMIdentity
    $register = Invoke-CloudOSDRequest -BaseUrl $baseUrl `
        -FallbackBaseUrl $fallbackUrl `
        -Path '/api/cloudosd/pe/register' `
        -Method POST `
        -Body @{
            vm_uuid = $identity.vm_uuid
            mac = $identity.mac
            architecture = $config.architecture
            build_sha = $config.build_sha
            manufacturer = $identity.manufacturer
            model = $identity.model
        }
    $token = [string] $register.bearer_token
    $runId = [string] $register.run_id

    $package = Invoke-CloudOSDRequest -BaseUrl $baseUrl `
        -FallbackBaseUrl $fallbackUrl `
        -Path "/api/cloudosd/pe/package/$runId" `
        -Method GET `
        -BearerToken $token

    $moduleRoot = Import-CloudOSDModule -ExpectedVersion $config.osdcloud_module_version
    $null = New-CloudOSDWorkflow -ModuleRoot $moduleRoot `
        -Package $package `
        -Architecture $config.architecture

    $transcript = "X:\cloudosd-$runId.log"
    Start-Transcript -Path $transcript -Force | Out-Null
    try {
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'pe' `
            -EventType 'osdcloud_start' -Message 'Starting OSDCloud deploy'

        Invoke-CloudOSDDeploy -WorkflowName $package.workflow_name

        $windowsRoot = Get-CloudOSDWindowsRoot
        Add-CloudOSDOfflineVirtIODrivers `
            -WindowsRoot $windowsRoot `
            -PreferredOsKey (Get-CloudOSDVirtioOsKey -Package $package)
        $stageRoot = Save-CloudOSDRunPackage -Package $package `
            -WindowsRoot $windowsRoot `
            -BridgeRoot $PSScriptRoot `
            -BearerToken $token
        Add-PVEAutopilotSetupCompleteChain -WindowsRoot $windowsRoot
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'setupcomplete' `
            -EventType 'setupcomplete_chained' `
            -Message 'SetupComplete first-boot chain staged' `
            -Data @{ windows_root = $windowsRoot; staged_root = $stageRoot }
        Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot `
            -ComputerName (Get-PVEAutopilotPackageComputerName -Package $package)
        Add-PVEAutopilotSetupSpecializePackage -WindowsRoot $windowsRoot -ModuleRoot $moduleRoot
        Disable-PVEAutopilotAutomaticDeviceEncryption -WindowsRoot $windowsRoot
        $validation = Test-CloudOSDOfflineWindows -WindowsRoot $windowsRoot
        if (-not $validation.ok) {
            Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
                -RunId $runId -BearerToken $token -Phase 'offline_validation' `
                -EventType 'offline_validation_failed' -Severity 'error' `
                -Message "CloudOSD offline validation failed: $($validation.errors -join '; ')" `
                -Data @{ windows_root = $windowsRoot; errors = $validation.errors }
            throw "CloudOSD offline validation failed: $($validation.errors -join '; ')"
        }
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'offline_validation' `
            -EventType 'offline_validation_ok' `
            -Message 'Offline Windows validation passed' `
            -Data @{ windows_root = $windowsRoot; staged_root = $stageRoot }

        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'pe' `
            -EventType 'cloudosd_pe_complete' `
            -Message "CloudOSD PE phase complete; staged $stageRoot"
    } catch {
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'pe' `
            -EventType 'cloudosd_failed' -Severity 'error' `
            -Message $_.Exception.Message
        throw
    } finally {
        Stop-Transcript | Out-Null
    }

    Stop-CloudOSDForControllerHandoff
}

if ($env:CLOUDOSD_BRIDGE_LIBRARY_ONLY -ne '1') {
    Invoke-CloudOSDBridge
}
