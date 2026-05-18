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

function Get-CloudOSDObjectProperty {
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

function Test-CloudOSDDomainJoinEnabled {
    param([AllowNull()] [object] $DomainJoin)
    $enabled = Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'enabled'
    return ($null -ne $enabled -and [bool] $enabled)
}

function Get-CloudOSDSanitizedRunPackage {
    param([Parameter(Mandatory)] [object] $Package)
    $copy = ConvertTo-CloudOSDHashtable $Package
    if ($copy -isnot [System.Collections.IDictionary]) { return $copy }

    if ($copy.Contains('bearer_token')) {
        $copy.Remove('bearer_token')
    }
    if ($copy.Contains('domain_join') -and
        $copy['domain_join'] -is [System.Collections.IDictionary]) {
        foreach ($secretKey in @('username', 'password', 'credential_id')) {
            if ($copy['domain_join'].Contains($secretKey)) {
                $copy['domain_join'].Remove($secretKey)
            }
        }
    }
    return $copy
}

function New-PVEAutopilotUnattendTextElement {
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

function Set-PVEAutopilotUnattendTextElement {
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

function New-PVEAutopilotOobeBootstrapPassword {
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

function Set-PVEAutopilotUnattendPasswordElement {
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
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $passwordNode `
        -NamespaceManager $NamespaceManager `
        -Namespace $Namespace `
        -Name 'Value' `
        -Value $Value | Out-Null
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $passwordNode `
        -NamespaceManager $NamespaceManager `
        -Namespace $Namespace `
        -Name 'PlainText' `
        -Value 'true' | Out-Null
}

function Add-PVEAutopilotUnattendedJoinComponent {
    param(
        [Parameter(Mandatory)] [System.Xml.XmlDocument] $Xml,
        [Parameter(Mandatory)] [System.Xml.XmlElement] $Settings,
        [Parameter(Mandatory)] [System.Xml.XmlNamespaceManager] $NamespaceManager,
        [Parameter(Mandatory)] [string] $UnattendNamespace,
        [AllowNull()] [object] $DomainJoin
    )
    if (-not (Test-CloudOSDDomainJoinEnabled -DomainJoin $DomainJoin)) { return }

    $domainFqdn = [string] (Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'domain_fqdn')
    $credentialDomain = [string] (Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'credential_domain')
    $username = [string] (Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'username')
    $password = [string] (Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'password')
    $ouPath = [string] (Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'ou_path')
    if ([string]::IsNullOrWhiteSpace($domainFqdn) -or
        [string]::IsNullOrWhiteSpace($username) -or
        [string]::IsNullOrWhiteSpace($password)) {
        throw 'CloudOSD domain join package is missing domain_fqdn, username, or password'
    }
    if ([string]::IsNullOrWhiteSpace($credentialDomain)) {
        $credentialDomain = $domainFqdn
    }

    $existing = $Settings.SelectNodes("u:component[@name='Microsoft-Windows-UnattendedJoin']", $NamespaceManager)
    foreach ($component in @($existing)) {
        $hasSentinel = $false
        foreach ($child in $component.ChildNodes) {
            if ($child.NodeType -eq [System.Xml.XmlNodeType]::Comment -and
                $child.Value -match 'PVEAUTOPILOT-CLOUDOSD-DOMAIN-JOIN') {
                $hasSentinel = $true
            }
        }
        if (-not $hasSentinel) {
            throw 'Refusing to overwrite existing Microsoft-Windows-UnattendedJoin component without ProxmoxVEAutopilot sentinel'
        }
        $Settings.RemoveChild($component) | Out-Null
    }

    $component = $Xml.CreateElement('component', $UnattendNamespace)
    $component.SetAttribute('name', 'Microsoft-Windows-UnattendedJoin')
    $component.SetAttribute('processorArchitecture', 'amd64')
    $component.SetAttribute('publicKeyToken', '31bf3856ad364e35')
    $component.SetAttribute('language', 'neutral')
    $component.SetAttribute('versionScope', 'nonSxS')
    $component.AppendChild($Xml.CreateComment('PVEAUTOPILOT-CLOUDOSD-DOMAIN-JOIN')) | Out-Null

    $identification = $Xml.CreateElement('Identification', $UnattendNamespace)
    $credentials = $Xml.CreateElement('Credentials', $UnattendNamespace)
    New-PVEAutopilotUnattendTextElement -Xml $Xml -Parent $credentials -Namespace $UnattendNamespace -Name 'Domain' -Value $credentialDomain | Out-Null
    New-PVEAutopilotUnattendTextElement -Xml $Xml -Parent $credentials -Namespace $UnattendNamespace -Name 'Username' -Value $username | Out-Null
    New-PVEAutopilotUnattendTextElement -Xml $Xml -Parent $credentials -Namespace $UnattendNamespace -Name 'Password' -Value $password | Out-Null
    $identification.AppendChild($credentials) | Out-Null
    New-PVEAutopilotUnattendTextElement -Xml $Xml -Parent $identification -Namespace $UnattendNamespace -Name 'JoinDomain' -Value $domainFqdn | Out-Null
    if (-not [string]::IsNullOrWhiteSpace($ouPath)) {
        New-PVEAutopilotUnattendTextElement -Xml $Xml -Parent $identification -Namespace $UnattendNamespace -Name 'MachineObjectOU' -Value $ouPath | Out-Null
    }
    $component.AppendChild($identification) | Out-Null
    $Settings.AppendChild($component) | Out-Null
}

function Add-PVEAutopilotOobeSystemUnattend {
    param(
        [Parameter(Mandatory)] [System.Xml.XmlDocument] $Xml,
        [Parameter(Mandatory)] [System.Xml.XmlElement] $RootElement,
        [Parameter(Mandatory)] [System.Xml.XmlNamespaceManager] $NamespaceManager,
        [Parameter(Mandatory)] [string] $UnattendNamespace
    )

    $settings = $RootElement.SelectSingleNode("u:settings[@pass='oobeSystem']", $NamespaceManager)
    if (-not $settings) {
        $settings = $Xml.CreateElement('settings', $UnattendNamespace)
        $settings.SetAttribute('pass', 'oobeSystem')
        $RootElement.AppendChild($settings) | Out-Null
    }

    $shellComponent = $settings.SelectSingleNode("u:component[@name='Microsoft-Windows-Shell-Setup' and @processorArchitecture='amd64']", $NamespaceManager)
    if (-not $shellComponent) {
        $shellComponent = $Xml.CreateElement('component', $UnattendNamespace)
        $shellComponent.SetAttribute('name', 'Microsoft-Windows-Shell-Setup')
        $shellComponent.SetAttribute('processorArchitecture', 'amd64')
        $shellComponent.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        $shellComponent.SetAttribute('language', 'neutral')
        $shellComponent.SetAttribute('versionScope', 'nonSxS')
        $settings.AppendChild($shellComponent) | Out-Null
    }
    $oobe = $shellComponent.SelectSingleNode('u:OOBE', $NamespaceManager)
    if (-not $oobe) {
        $oobe = $Xml.CreateElement('OOBE', $UnattendNamespace)
        $shellComponent.AppendChild($oobe) | Out-Null
    }
    foreach ($entry in @(
        @{ Name = 'HideEULAPage'; Value = 'true' },
        @{ Name = 'HideOEMRegistrationScreen'; Value = 'true' },
        @{ Name = 'HideLocalAccountScreen'; Value = 'true' },
        @{ Name = 'HideOnlineAccountScreens'; Value = 'true' },
        @{ Name = 'HideWirelessSetupInOOBE'; Value = 'true' },
        @{ Name = 'ProtectYourPC'; Value = '3' }
    )) {
        Set-PVEAutopilotUnattendTextElement -Xml $Xml `
            -Parent $oobe `
            -NamespaceManager $NamespaceManager `
            -Namespace $UnattendNamespace `
            -Name $entry.Name `
            -Value $entry.Value | Out-Null
    }

    $bootstrapUser = 'PVEAutopilot'
    $bootstrapPassword = New-PVEAutopilotOobeBootstrapPassword
    $userAccounts = $shellComponent.SelectSingleNode('u:UserAccounts', $NamespaceManager)
    if (-not $userAccounts) {
        $userAccounts = $Xml.CreateElement('UserAccounts', $UnattendNamespace)
        $shellComponent.AppendChild($userAccounts) | Out-Null
    }
    $localAccounts = $userAccounts.SelectSingleNode('u:LocalAccounts', $NamespaceManager)
    if (-not $localAccounts) {
        $localAccounts = $Xml.CreateElement('LocalAccounts', $UnattendNamespace)
        $userAccounts.AppendChild($localAccounts) | Out-Null
    }
    $localAccount = $localAccounts.SelectSingleNode("u:LocalAccount[u:Name='$bootstrapUser']", $NamespaceManager)
    if (-not $localAccount) {
        $localAccount = $Xml.CreateElement('LocalAccount', $UnattendNamespace)
        $localAccount.SetAttribute('action', 'http://schemas.microsoft.com/WMIConfig/2002/State', 'add')
        $localAccounts.AppendChild($localAccount) | Out-Null
    }
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $localAccount `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Name 'Name' `
        -Value $bootstrapUser | Out-Null
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $localAccount `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Name 'Group' `
        -Value 'Administrators' | Out-Null
    Set-PVEAutopilotUnattendPasswordElement -Xml $Xml `
        -Parent $localAccount `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Value $bootstrapPassword

    $autoLogon = $shellComponent.SelectSingleNode('u:AutoLogon', $NamespaceManager)
    if (-not $autoLogon) {
        $autoLogon = $Xml.CreateElement('AutoLogon', $UnattendNamespace)
        $shellComponent.AppendChild($autoLogon) | Out-Null
    }
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $autoLogon `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Name 'Enabled' `
        -Value 'true' | Out-Null
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $autoLogon `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Name 'Username' `
        -Value $bootstrapUser | Out-Null
    Set-PVEAutopilotUnattendPasswordElement -Xml $Xml `
        -Parent $autoLogon `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Value $bootstrapPassword
    Set-PVEAutopilotUnattendTextElement -Xml $Xml `
        -Parent $autoLogon `
        -NamespaceManager $NamespaceManager `
        -Namespace $UnattendNamespace `
        -Name 'LogonCount' `
        -Value '1' | Out-Null

    $intlComponent = $settings.SelectSingleNode("u:component[@name='Microsoft-Windows-International-Core' and @processorArchitecture='amd64']", $NamespaceManager)
    if (-not $intlComponent) {
        $intlComponent = $Xml.CreateElement('component', $UnattendNamespace)
        $intlComponent.SetAttribute('name', 'Microsoft-Windows-International-Core')
        $intlComponent.SetAttribute('processorArchitecture', 'amd64')
        $intlComponent.SetAttribute('publicKeyToken', '31bf3856ad364e35')
        $intlComponent.SetAttribute('language', 'neutral')
        $intlComponent.SetAttribute('versionScope', 'nonSxS')
        $settings.AppendChild($intlComponent) | Out-Null
    }
    foreach ($entry in @(
        @{ Name = 'InputLocale'; Value = 'en-US' },
        @{ Name = 'SystemLocale'; Value = 'en-US' },
        @{ Name = 'UILanguage'; Value = 'en-US' },
        @{ Name = 'UserLocale'; Value = 'en-US' }
    )) {
        Set-PVEAutopilotUnattendTextElement -Xml $Xml `
            -Parent $intlComponent `
            -NamespaceManager $NamespaceManager `
            -Namespace $UnattendNamespace `
            -Name $entry.Name `
            -Value $entry.Value | Out-Null
    }
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

function Get-CloudOSDEfiSystemRoot {
    param([string] $PreferredDriveLetter = 'S')

    $efi = Get-Partition |
        Where-Object {
            ($_.GptType -and $_.GptType.ToString().Trim('{}').Equals('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', [System.StringComparison]::OrdinalIgnoreCase)) -or
            ($_.Type -eq 'System')
        } |
        Sort-Object DiskNumber, PartitionNumber |
        Select-Object -First 1
    if (-not $efi) {
        throw 'could not locate EFI system partition'
    }

    $letter = [string] $efi.DriveLetter
    if ([string]::IsNullOrWhiteSpace($letter)) {
        $used = @(Get-Volume | Where-Object DriveLetter | ForEach-Object { [string] $_.DriveLetter })
        $letter = @($PreferredDriveLetter, 'T', 'U', 'V', 'W', 'Y') |
            Where-Object { $used -notcontains $_ } |
            Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($letter)) {
            throw 'no drive letter is available for EFI system partition'
        }
        Add-PartitionAccessPath `
            -DiskNumber $efi.DiskNumber `
            -PartitionNumber $efi.PartitionNumber `
            -AccessPath "$letter`:" | Out-Null
    }

    return "$letter`:"
}

function Invoke-CloudOSDBootFiles {
    param(
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string] $EfiRoot
    )

    $bcdboot = Get-Command bcdboot.exe -ErrorAction Stop
    & $bcdboot.Source $WindowsRoot /s $EfiRoot /f UEFI
    if ($LASTEXITCODE -ne 0) {
        throw "bcdboot failed with exit code $LASTEXITCODE"
    }
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

function Join-CloudOSDPath {
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

function Resolve-CloudOSDPackageTargetPath {
    param(
        [Parameter(Mandatory)] [string] $TargetPath,
        [Parameter(Mandatory)] [string] $WindowsRoot
    )
    if ($TargetPath -match '^[A-Za-z]:\\Windows\\(.+)$') {
        return Join-CloudOSDPath -Root $WindowsRoot -RelativePath $Matches[1]
    }
    if ($TargetPath -match '^[A-Za-z]:\\ProgramData\\(.+)$') {
        return Join-CloudOSDPath `
            -Root (Get-OfflineProgramDataPath -WindowsRoot $WindowsRoot) `
            -RelativePath $Matches[1]
    }
    throw "unsupported OSD client package target path: $TargetPath"
}

function Set-CloudOSDObjectProperty {
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

function Save-CloudOSDOsdClientPackage {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $BearerToken,
        [object] $OsdClientPackage,
        [scriptblock] $PackageDownloader = {
            param($Url,$Headers)
            Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec 300
        }
    )
    if (
        $Package.PSObject.Properties.Match('payloads').Count -eq 0 -or
        $Package.payloads.PSObject.Properties.Match('osd_client').Count -eq 0 -or
        -not $Package.payloads.osd_client
    ) {
        return $null
    }

    if (-not $OsdClientPackage) {
        if (
            $Package.payloads.osd_client.PSObject.Properties.Match('url').Count -eq 0 -or
            -not $Package.payloads.osd_client.url
        ) {
            throw 'OSD client payload is missing package URL'
        }
        $headers = @{}
        if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
        $OsdClientPackage = & $PackageDownloader ([string] $Package.payloads.osd_client.url) $headers
    }

    $programDataRoot = Get-OfflineProgramDataPath -WindowsRoot $WindowsRoot
    $osdRoot = Join-CloudOSDPath `
        -Root $programDataRoot `
        -RelativePath 'ProxmoxVEAutopilot\OSD'
    New-Item -ItemType Directory -Path $osdRoot -Force | Out-Null

    foreach ($file in @($OsdClientPackage.files)) {
        $targetPath = [string] $file.path
        if ($targetPath -match '\\Windows\\Setup\\Scripts\\SetupComplete\.cmd$') {
            continue
        }
        $destination = Resolve-CloudOSDPackageTargetPath `
            -TargetPath $targetPath `
            -WindowsRoot $WindowsRoot
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        [System.IO.File]::WriteAllBytes(
            $destination,
            [Convert]::FromBase64String([string] $file.content_b64)
        )
    }

    $config = $OsdClientPackage.config | ConvertTo-Json -Depth 30 | ConvertFrom-Json
    Set-CloudOSDObjectProperty `
        -InputObject $config `
        -Name 'flask_base_url' `
        -Value ([string] $Package.server_base_url)
    if (
        $Package.PSObject.Properties.Match('server_base_url_fallback').Count -gt 0 -and
        $Package.server_base_url_fallback
    ) {
        Set-CloudOSDObjectProperty `
            -InputObject $config `
            -Name 'flask_base_url_fallback' `
            -Value ([string] $Package.server_base_url_fallback)
    }
    $configPath = Join-CloudOSDPath -Root $osdRoot -RelativePath 'osd-config.json'
    $config | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $configPath -Encoding UTF8

    Add-Member -InputObject $Package.payloads.osd_client `
        -NotePropertyName local_path `
        -NotePropertyValue ([string] $osdRoot) `
        -Force
    return $osdRoot
}

function Save-CloudOSDRunPackage {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [Parameter(Mandatory)] [string] $BridgeRoot,
        [string] $BearerToken,
        [object] $OsdClientPackage,
        [string[]] $QgaSearchRoots,
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
    $osdClientArgs = @{
        Package = $Package
        WindowsRoot = $WindowsRoot
        BearerToken = $BearerToken
    }
    if ($OsdClientPackage) { $osdClientArgs.OsdClientPackage = $OsdClientPackage }
    Save-CloudOSDOsdClientPackage @osdClientArgs | Out-Null
    if ($Package.payloads.first_boot_script) {
        $Package.payloads.first_boot_script |
            Add-Member -NotePropertyName local_path `
                -NotePropertyValue (Join-Path $stageRoot 'PVEAutopilot-FirstBoot.ps1') `
                -Force
    }
    $qgaMsi = Copy-CloudOSDQemuGuestAgentMsi `
        -Destination (Join-Path $stageRoot 'qemu-ga-x86_64.msi') `
        -SearchRoots $QgaSearchRoots
    if (-not $qgaMsi) {
        throw 'QEMU Guest Agent MSI not found in attached VirtIO media; cannot stage first-boot QGA install.'
    }

    $runJson = Join-Path $stageRoot 'cloudosd-run.json'
    Get-CloudOSDSanitizedRunPackage -Package $Package |
        ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath $runJson -Encoding UTF8
    return $stageRoot
}

function Find-CloudOSDQemuGuestAgentMsi {
    param([string[]] $SearchRoots)
    $roots = @()
    if ($SearchRoots) {
        $roots += @($SearchRoots | Where-Object { $_ })
    } else {
        $roots += @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Root })
    }
    foreach ($root in @($roots)) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        foreach ($relative in @(
            'guest-agent\qemu-ga-x86_64.msi',
            'qemu-ga-x86_64.msi',
            'qemu\qemu-ga-x86_64.msi'
        )) {
            $candidate = Join-Path $root $relative
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
    }
    return $null
}

function Copy-CloudOSDQemuGuestAgentMsi {
    param(
        [Parameter(Mandatory)] [string] $Destination,
        [string[]] $SearchRoots
    )
    $source = Find-CloudOSDQemuGuestAgentMsi -SearchRoots $SearchRoots
    if (-not $source) { return $null }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $Destination -Force
    return $Destination
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
        [string] $ComputerName,
        [object] $DomainJoin
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

    Add-PVEAutopilotUnattendedJoinComponent -Xml $xml `
        -Settings $settings `
        -NamespaceManager $ns `
        -UnattendNamespace $unattendNs `
        -DomainJoin $DomainJoin

    Add-PVEAutopilotOobeSystemUnattend -Xml $xml `
        -RootElement $rootElement `
        -NamespaceManager $ns `
        -UnattendNamespace $unattendNs

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
        [string] $EfiRoot,
        [object] $DomainJoin
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
    } else {
        $runJsonText = Get-Content -LiteralPath $runJson -Raw
        if ($runJsonText -match '"password"\s*:' -or $runJsonText -match '"username"\s*:') {
            $errors += 'CloudOSD offline run package contains domain join credentials'
        }
        if (-not (Test-CloudOSDDomainJoinEnabled -DomainJoin $DomainJoin)) {
            try {
                $runConfig = $runJsonText | ConvertFrom-Json
                if ($runConfig.PSObject.Properties.Match('domain_join').Count -gt 0) {
                    $DomainJoin = $runConfig.domain_join
                }
            } catch {
                $errors += "CloudOSD run package JSON is not readable: $($_.Exception.Message)"
            }
        }
    }
    if (Test-CloudOSDDomainJoinEnabled -DomainJoin $DomainJoin) {
        if (-not (Test-Path -LiteralPath $unattendPath)) {
            $errors += 'Domain join requested but UnattendedJoin XML is missing'
        } else {
            try {
                $domainXml = New-Object System.Xml.XmlDocument
                $domainXml.Load($unattendPath)
                $domainNs = New-Object System.Xml.XmlNamespaceManager($domainXml.NameTable)
                $domainNs.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
                $joinComponent = $domainXml.SelectSingleNode("//u:component[@name='Microsoft-Windows-UnattendedJoin']", $domainNs)
                if (-not $joinComponent) {
                    $errors += 'Domain join requested but Microsoft-Windows-UnattendedJoin is missing'
                } else {
                    $joinDomain = $joinComponent.SelectSingleNode('.//u:JoinDomain', $domainNs)
                    $expectedDomain = [string] (Get-CloudOSDObjectProperty -Value $DomainJoin -Name 'domain_fqdn')
                    if (-not $joinDomain -or [string]::IsNullOrWhiteSpace($joinDomain.InnerText)) {
                        $errors += 'UnattendedJoin JoinDomain is missing'
                    } elseif (-not [string]::IsNullOrWhiteSpace($expectedDomain) -and
                        $joinDomain.InnerText -ine $expectedDomain) {
                        $errors += "UnattendedJoin JoinDomain '$($joinDomain.InnerText)' does not match expected domain '$expectedDomain'"
                    }
                    $passwordNode = $joinComponent.SelectSingleNode('.//u:Credentials/u:Password', $domainNs)
                    if (-not $passwordNode -or [string]::IsNullOrWhiteSpace($passwordNode.InnerText)) {
                        $errors += 'UnattendedJoin password is missing before specialize'
                    }
                }
            } catch {
                $errors += "UnattendedJoin XML could not be read: $($_.Exception.Message)"
            }
        }
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

function Set-CloudOSDFeatureImageCacheSource {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $ModuleRoot
    )

    $cache = Get-CloudOSDObjectProperty -Value $Package -Name 'cache'
    $feature = Get-CloudOSDObjectProperty -Value $cache -Name 'feature_image'
    if (-not $feature) {
        return [pscustomobject]@{ applied = $false; reason = 'no_feature_cache_payload' }
    }
    $hit = Get-CloudOSDObjectProperty -Value $feature -Name 'hit'
    $downloadUrl = [string] (Get-CloudOSDObjectProperty -Value $feature -Name 'download_url')
    if (-not $hit -or [string]::IsNullOrWhiteSpace($downloadUrl)) {
        return [pscustomobject]@{
            applied = $false
            reason = 'cache_miss'
            entry_id = Get-CloudOSDObjectProperty -Value $feature -Name 'entry_id'
            status = Get-CloudOSDObjectProperty -Value $feature -Name 'status'
        }
    }

    $catalogFile = [string] (Get-CloudOSDObjectProperty -Value $feature -Name 'catalog_file')
    $fileName = [string] (Get-CloudOSDObjectProperty -Value $feature -Name 'file_name')
    $expectedSha256 = [string] (Get-CloudOSDObjectProperty -Value $feature -Name 'expected_sha256')
    if ([string]::IsNullOrWhiteSpace($catalogFile)) {
        throw 'CloudOSD feature cache hit is missing catalog_file'
    }
    if ([string]::IsNullOrWhiteSpace($fileName)) {
        throw 'CloudOSD feature cache hit is missing file_name'
    }

    $catalogPath = Join-CloudOSDPath -Root $ModuleRoot -RelativePath $catalogFile
    if (-not (Test-Path -LiteralPath $catalogPath)) {
        throw "OSDCloud catalog file not found for cache rewrite: $catalogPath"
    }

    [xml] $catalog = Get-Content -LiteralPath $catalogPath -Raw
    $target = $null
    foreach ($node in @($catalog.SelectNodes('//File'))) {
        $nameNode = $node.SelectSingleNode('FileName')
        $shaNode = $node.SelectSingleNode('Sha256')
        $nameMatches = $nameNode -and ([string] $nameNode.InnerText) -eq $fileName
        $shaMatches = $false
        if ($expectedSha256 -and $shaNode) {
            $shaMatches = ([string] $shaNode.InnerText).ToLowerInvariant() -eq $expectedSha256.ToLowerInvariant()
        }
        if ($nameMatches -or $shaMatches) {
            $target = $node
            break
        }
    }
    if (-not $target) {
        throw "OSDCloud catalog entry not found for cached feature image: $fileName"
    }

    $filePathNode = $target.SelectSingleNode('FilePath')
    if (-not $filePathNode) {
        throw "OSDCloud catalog entry has no FilePath node: $fileName"
    }
    $originalUrl = [string] $filePathNode.InnerText
    $filePathNode.InnerText = $downloadUrl
    $catalog.Save($catalogPath)

    return [pscustomobject]@{
        applied = $true
        entry_id = Get-CloudOSDObjectProperty -Value $feature -Name 'entry_id'
        catalog_file = $catalogFile
        file_name = $fileName
        original_url = $originalUrl
        download_url = $downloadUrl
        expected_sha256 = $expectedSha256
    }
}

function Invoke-CloudOSDQualityUpdateServicing {
    param(
        [Parameter(Mandatory)] [object] $Package,
        [Parameter(Mandatory)] [string] $WindowsRoot,
        [string] $BaseUrl,
        [string] $FallbackBaseUrl,
        [string] $RunId,
        [string] $BearerToken,
        [string] $DownloadRoot = 'X:\CloudOSDQualityUpdates',
        [scriptblock] $Downloader = {
            param($Url, $OutFile)
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutFile -TimeoutSec 900
        },
        [scriptblock] $DismRunner = {
            param($ImageRoot, $PackagePath)
            & dism.exe /Image:$ImageRoot /Add-Package /PackagePath:$PackagePath | Out-Null
            return $LASTEXITCODE
        }
    )

    $cache = Get-CloudOSDObjectProperty -Value $Package -Name 'cache'
    $updates = @(Get-CloudOSDObjectProperty -Value $cache -Name 'quality_updates')
    $readyUpdates = @()
    foreach ($update in $updates) {
        $status = [string] (Get-CloudOSDObjectProperty -Value $update -Name 'status')
        $url = [string] (Get-CloudOSDObjectProperty -Value $update -Name 'url')
        if ($status -eq 'ready' -and -not [string]::IsNullOrWhiteSpace($url)) {
            $readyUpdates += $update
        }
    }

    if ($readyUpdates.Count -eq 0) {
        if ($BaseUrl -and $RunId -and $BearerToken) {
            Write-CloudOSDEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
                -RunId $RunId -BearerToken $BearerToken -Phase 'quality_update' `
                -EventType 'cloudosd_quality_update_skipped' `
                -Message 'No ready cached quality update packages were available for offline servicing'
        }
        return [pscustomobject]@{ applied = 0; skipped = $true }
    }

    if ($BaseUrl -and $RunId -and $BearerToken) {
        Write-CloudOSDEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
            -RunId $RunId -BearerToken $BearerToken -Phase 'quality_update' `
            -EventType 'cloudosd_quality_update_start' `
            -Message "Applying $($readyUpdates.Count) cached quality update package(s) offline"
    }

    $imageRoot = Split-Path -Parent $WindowsRoot
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    $applied = @()
    foreach ($update in $readyUpdates) {
        $fileName = [string] (Get-CloudOSDObjectProperty -Value $update -Name 'file_name')
        if ([string]::IsNullOrWhiteSpace($fileName)) {
            $fileName = [System.IO.Path]::GetFileName(([uri] ([string] (Get-CloudOSDObjectProperty -Value $update -Name 'url'))).AbsolutePath)
        }
        $destination = Join-Path $DownloadRoot ([System.IO.Path]::GetFileName($fileName))
        & $Downloader ([string] (Get-CloudOSDObjectProperty -Value $update -Name 'url')) $destination
        $expectedSha256 = [string] (Get-CloudOSDObjectProperty -Value $update -Name 'sha256')
        if ($expectedSha256) {
            $actualSha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actualSha256 -ne $expectedSha256.ToLowerInvariant()) {
                throw "SHA256 mismatch for cached quality update $fileName"
            }
        }
        $exitCode = & $DismRunner $imageRoot $destination
        if ($exitCode -ne 0) {
            if ($BaseUrl -and $RunId -and $BearerToken) {
                Write-CloudOSDEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
                    -RunId $RunId -BearerToken $BearerToken -Phase 'quality_update' `
                    -EventType 'cloudosd_quality_update_failed' -Severity 'error' `
                    -Message "DISM failed while applying cached quality update ${fileName}: $exitCode" `
                    -Data @{ package = $fileName; exit_code = $exitCode }
            }
            throw "DISM failed while applying cached quality update ${fileName}: $exitCode"
        }
        $applied += $fileName
    }

    if ($BaseUrl -and $RunId -and $BearerToken) {
        Write-CloudOSDEvent -BaseUrl $BaseUrl -FallbackBaseUrl $FallbackBaseUrl `
            -RunId $RunId -BearerToken $BearerToken -Phase 'quality_update' `
            -EventType 'cloudosd_quality_update_applied' `
            -Message "Applied $($applied.Count) cached quality update package(s) offline" `
            -Data @{ packages = $applied; image_root = $imageRoot }
    }
    return [pscustomobject]@{ applied = $applied.Count; packages = $applied }
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
    $cachePatch = Set-CloudOSDFeatureImageCacheSource -Package $package -ModuleRoot $moduleRoot
    if ($cachePatch.applied) {
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'cache' `
            -EventType 'cloudosd_cache_feature_image_hit' `
            -Message "CloudOSD feature image cache source applied: $($cachePatch.file_name)" `
            -Data @{
                entry_id = $cachePatch.entry_id
                catalog_file = $cachePatch.catalog_file
                file_name = $cachePatch.file_name
                expected_sha256 = $cachePatch.expected_sha256
            }
    } else {
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'cache' `
            -EventType 'cloudosd_cache_feature_image_miss' `
            -Message "CloudOSD feature image cache miss; continuing with OSDCloud source URL" `
            -Data @{ reason = $cachePatch.reason; entry_id = $cachePatch.entry_id; status = $cachePatch.status }
    }
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
        $efiRoot = Get-CloudOSDEfiSystemRoot
        Invoke-CloudOSDBootFiles -WindowsRoot $windowsRoot -EfiRoot $efiRoot
        Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
            -RunId $runId -BearerToken $token -Phase 'pe' `
            -EventType 'uefi_boot_files_staged' `
            -Message 'UEFI boot files staged with bcdboot' `
            -Data @{ windows_root = $windowsRoot; efi_root = $efiRoot }
        Invoke-CloudOSDQualityUpdateServicing -Package $package `
            -WindowsRoot $windowsRoot `
            -BaseUrl $baseUrl `
            -FallbackBaseUrl $fallbackUrl `
            -RunId $runId `
            -BearerToken $token | Out-Null
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
            -ComputerName (Get-PVEAutopilotPackageComputerName -Package $package) `
            -DomainJoin $package.domain_join
        if (Test-CloudOSDDomainJoinEnabled -DomainJoin $package.domain_join) {
            Write-CloudOSDEvent -BaseUrl $baseUrl -FallbackBaseUrl $fallbackUrl `
                -RunId $runId -BearerToken $token -Phase 'domain_join' `
                -EventType 'domain_join_unattend_staged' `
                -Message 'AD domain join specialize unattend staged'
        }
        Add-PVEAutopilotSetupSpecializePackage -WindowsRoot $windowsRoot -ModuleRoot $moduleRoot
        Disable-PVEAutopilotAutomaticDeviceEncryption -WindowsRoot $windowsRoot
        $validation = Test-CloudOSDOfflineWindows -WindowsRoot $windowsRoot `
            -EfiRoot $efiRoot `
            -DomainJoin $package.domain_join
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
