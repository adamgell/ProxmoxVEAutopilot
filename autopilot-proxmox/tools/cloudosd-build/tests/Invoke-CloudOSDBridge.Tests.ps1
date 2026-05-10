BeforeAll {
    $env:CLOUDOSD_BRIDGE_LIBRARY_ONLY = '1'
    $script:BridgePath = (Resolve-Path "$PSScriptRoot/../Invoke-CloudOSDBridge.ps1").Path
    . $script:BridgePath
}

AfterAll {
    Remove-Item Env:\CLOUDOSD_BRIDGE_LIBRARY_ONLY -ErrorAction SilentlyContinue
}

Describe 'New-CloudOSDWorkflow' {
    It 'writes run-specific OSDCloud workflow JSON from the package' {
        $moduleRoot = Join-Path $TestDrive 'OSDCloud'
        $defaultTasks = Join-Path $moduleRoot 'workflow/default/tasks'
        New-Item -ItemType Directory -Path $defaultTasks -Force | Out-Null
        @{
            name = 'OSDCloud [No Firmware Update]'
            amd64 = $true
            arm64 = $true
            steps = @(
                @{ name = 'Initialize OSDCloud Workflow'; command = 'step-initialize-osdcloudworkflowtask' },
                @{ name = 'Clear Local Disk'; command = 'step-preinstall-cleartargetdisk' },
                @{ name = 'Apply WinPE Drivers to offline Windows Image'; command = 'step-Add-WindowsDriver-OemWinOS' },
                @{ name = 'Update PowerShell Modules - Offline'; command = 'step-powershell-updatemodule' }
            )
        } | ConvertTo-Json -Depth 10 |
            Set-Content -LiteralPath (Join-Path $defaultTasks 'osdcloud-nofirmware.json')
        $package = [pscustomobject]@{
            workflow_name = 'pveautopilot-run-1'
            os_settings = [pscustomobject]@{
                OperatingSystem = [pscustomobject]@{ default = 'Windows 11 25H2' }
                OSActivation = [pscustomobject]@{ default = 'Volume' }
                OSEdition = [pscustomobject]@{ default = 'Enterprise' }
                OSLanguageCode = [pscustomobject]@{ default = 'en-us' }
            }
            user_settings = [pscustomobject]@{
                DriverPacks = [pscustomobject]@{ Default = 'None' }
                UpdateSystemFirmware = $false
            }
            task = [pscustomobject]@{
                name = 'osdcloud-nofirmware'
                cli = $true
            }
        }

        $workflowPath = New-CloudOSDWorkflow -ModuleRoot $moduleRoot `
            -Package $package -Architecture 'amd64'

        Test-Path -LiteralPath (Join-Path $workflowPath 'os-amd64.json') |
            Should -BeTrue
        Test-Path -LiteralPath (Join-Path $workflowPath 'user-amd64.json') |
            Should -BeTrue
        Test-Path -LiteralPath (Join-Path $workflowPath 'tasks/osdcloud-nofirmware.json') |
            Should -BeTrue

        $osJson = Get-Content -LiteralPath (Join-Path $workflowPath 'os-amd64.json') -Raw |
            ConvertFrom-Json
        $osJson.OperatingSystem.default | Should -Be 'Windows 11 25H2'
        $taskJson = Get-Content -LiteralPath (Join-Path $workflowPath 'tasks/osdcloud-nofirmware.json') -Raw |
            ConvertFrom-Json
        $taskJson.name | Should -Be 'OSDCloud [No Firmware Update]'
        $taskJson.amd64 | Should -BeTrue
        $taskJson.steps[0].command | Should -Be 'step-initialize-osdcloudworkflowtask'
        $taskJson.steps[1].parameters.Confirm | Should -BeFalse
        $taskJson.steps[2].skip | Should -BeTrue
        $taskJson.steps[3].skip | Should -BeTrue
    }
}

Describe 'Invoke-CloudOSDDeploy' {
    It 'delegates to public Deploy-OSDCloud without directly calling private initialization' {
        $script:deployCall = $null
        function global:Deploy-OSDCloud {
            param(
                [string] $WorkflowName,
                [switch] $CLI
            )
            $script:deployCall = @{
                WorkflowName = $WorkflowName
                CLI = $CLI.IsPresent
            }
        }
        function global:Initialize-OSDCloudDeploy {
            throw 'private initialization should not be called directly'
        }

        try {
            Invoke-CloudOSDDeploy -WorkflowName 'pveautopilot-run-1'
        } finally {
            Remove-Item Function:\Deploy-OSDCloud -ErrorAction SilentlyContinue
            Remove-Item Function:\Initialize-OSDCloudDeploy -ErrorAction SilentlyContinue
        }

        $script:deployCall.WorkflowName | Should -Be 'pveautopilot-run-1'
        $script:deployCall.CLI | Should -BeTrue
    }
}

Describe 'Add-PVEAutopilotSetupCompleteChain' {
    It 'preserves existing SetupComplete content and appends the sentinel once' {
        $windowsRoot = Join-Path $TestDrive 'Windows'
        $scripts = Join-Path $windowsRoot 'Setup/Scripts'
        New-Item -ItemType Directory -Path $scripts -Force | Out-Null
        $setupComplete = Join-Path $scripts 'SetupComplete.cmd'
        '@echo off
echo existing osdcloud line
' | Set-Content -LiteralPath $setupComplete -Encoding ASCII

        Add-PVEAutopilotSetupCompleteChain -WindowsRoot $windowsRoot
        Add-PVEAutopilotSetupCompleteChain -WindowsRoot $windowsRoot

        $content = Get-Content -LiteralPath $setupComplete -Raw
        $content | Should -Match 'existing osdcloud line'
        ([regex]::Matches($content, 'PVEAUTOPILOT-CLOUDOSD-SENTINEL')).Count |
            Should -Be 1
        $content | Should -Match 'PVEAutopilot-SetupComplete\.cmd'
    }
}

Describe 'Add-PVEAutopilotSpecializeUnattend' {
    It 'writes a specialize RunSynchronous command that starts the first-boot task before OOBE' {
        $windowsRoot = Join-Path $TestDrive 'Windows'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null

        Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot -ComputerName 'LAB-2452956F'
        Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot -ComputerName 'LAB-2452956F'

        $unattendPath = Join-Path $windowsRoot 'Panther/Unattend.xml'
        Test-Path -LiteralPath $unattendPath | Should -BeTrue
        $content = Get-Content -LiteralPath $unattendPath -Raw
        $content | Should -Match '<settings pass="specialize">'
        $content | Should -Match '<ComputerName>LAB-2452956F</ComputerName>'
        $content | Should -Match 'RunSynchronousCommand'
        ([regex]::Matches($content, 'PVEAutopilot-SetupComplete\.cmd')).Count |
            Should -Be 1
    }
}

Describe 'Add-PVEAutopilotSetupSpecializePackage' {
    It 'stages the OSDCloud SetupSpecialize command and adds the provisioning package' {
        $offlineRoot = Join-Path $TestDrive 'offline'
        $windowsRoot = Join-Path $offlineRoot 'Windows'
        $moduleRoot = Join-Path $TestDrive 'OSDCloud'
        $packageRoot = Join-Path $moduleRoot 'core/setupspecialize'
        New-Item -ItemType Directory -Path $windowsRoot, $packageRoot -Force | Out-Null
        'ppkg' | Set-Content -LiteralPath (Join-Path $packageRoot 'setupspecialize.ppkg')
        $script:startProcessCall = $null
        function global:Start-Process {
            param(
                [string] $FilePath,
                [string] $ArgumentList,
                [switch] $Wait,
                [switch] $NoNewWindow,
                [switch] $PassThru
            )
            $script:startProcessCall = @{
                FilePath = $FilePath
                ArgumentList = $ArgumentList
                Wait = $Wait.IsPresent
                PassThru = $PassThru.IsPresent
            }
            [pscustomobject]@{ ExitCode = 0 }
        }

        try {
            Add-PVEAutopilotSetupSpecializePackage -WindowsRoot $windowsRoot -ModuleRoot $moduleRoot
        } finally {
            Remove-Item Function:\Start-Process -ErrorAction SilentlyContinue
        }

        $cmd = Join-Path $windowsRoot 'Temp/osdcloud/SetupSpecialize.cmd'
        Test-Path -LiteralPath $cmd | Should -BeTrue
        Get-Content -LiteralPath $cmd -Raw |
            Should -Match 'PVEAutopilot-SetupComplete\.cmd'
        $script:startProcessCall.FilePath | Should -Be 'dism.exe'
        $script:startProcessCall.ArgumentList | Should -Match '/Add-ProvisioningPackage'
        $script:startProcessCall.ArgumentList | Should -Match 'setupspecialize\.ppkg'
    }
}

Describe 'Disable-PVEAutopilotAutomaticDeviceEncryption' {
    It 'sets PreventDeviceEncryption in the offline SYSTEM control sets' {
        $windowsRoot = Join-Path $TestDrive 'Windows'
        $configRoot = Join-Path $windowsRoot 'System32/Config'
        New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
        'hive' | Set-Content -LiteralPath (Join-Path $configRoot 'SYSTEM')
        $script:regCalls = @()
        function global:reg.exe {
            $script:regCalls += ,($args -join ' ')
            $global:LASTEXITCODE = 0
            if ($args[0] -eq 'query') {
                @(
                    'HKEY_LOCAL_MACHINE\PVEAutopilotSYSTEM\ControlSet001',
                    'HKEY_LOCAL_MACHINE\PVEAutopilotSYSTEM\ControlSet002'
                )
            }
        }

        try {
            Disable-PVEAutopilotAutomaticDeviceEncryption -WindowsRoot $windowsRoot
        } finally {
            Remove-Item Function:\reg.exe -ErrorAction SilentlyContinue
        }

        ($script:regCalls -join '|') | Should -Match 'load HKLM\\PVEAutopilotSYSTEM'
        ($script:regCalls -join '|') | Should -Match 'ControlSet001\\Control\\BitLocker /v PreventDeviceEncryption'
        ($script:regCalls -join '|') | Should -Match 'ControlSet002\\Control\\BitLocker /v PreventDeviceEncryption'
        ($script:regCalls -join '|') | Should -Match 'unload HKLM\\PVEAutopilotSYSTEM'
    }
}

Describe 'Save-CloudOSDRunPackage' {
    It 'downloads first-boot payloads, stages the OSD client package, and records local paths in cloudosd-run.json' {
        $windowsRoot = Join-Path $TestDrive 'Windows'
        $bridgeRoot = Join-Path $TestDrive 'Bridge'
        New-Item -ItemType Directory -Path $windowsRoot, $bridgeRoot -Force | Out-Null
        'firstboot' | Set-Content -LiteralPath (Join-Path $bridgeRoot 'PVEAutopilot-FirstBoot.ps1')
        $package = [pscustomobject]@{
            run_id = 'run-1'
            server_base_url = 'https://autopilot.local'
            server_base_url_fallback = 'http://192.168.2.4:5000'
            payloads = [pscustomobject]@{
                osd_client = [pscustomobject]@{
                    url = 'https://autopilot.local/osd/v2/agent/package/run-1?phase=full_os'
                    sha256 = $null
                }
                autopilotagent_msi = [pscustomobject]@{
                    url = 'https://autopilot.local/api/cloudosd/assets/autopilotagent.msi'
                    sha256 = $null
                }
                autopilotagent_postinstall = [pscustomobject]@{
                    url = 'https://autopilot.local/api/cloudosd/assets/autopilotagent-postinstall.ps1'
                    sha256 = $null
                }
            }
        }
        $encode = {
            param([string] $Text)
            [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Text))
        }
        $osdClientPackage = [pscustomobject]@{
            config = [pscustomobject]@{
                engine = 'v2'
                api_version = 2
                flask_base_url = ''
                run_id = 'run-1'
                agent_id = 'osd-fullos-run-1'
                phase = 'full_os'
                bearer_token = 'osd-token'
            }
            files = @(
                [pscustomobject]@{
                    path = 'V:\Windows\Setup\Scripts\SetupComplete.cmd'
                    content_b64 = & $encode 'do not overwrite setupcomplete'
                },
                [pscustomobject]@{
                    path = 'V:\ProgramData\ProxmoxVEAutopilot\OSD\OsdClient.ps1'
                    content_b64 = & $encode 'osd client'
                },
                [pscustomobject]@{
                    path = 'V:\ProgramData\ProxmoxVEAutopilot\OSD\Get-WindowsAutopilotInfo.ps1'
                    content_b64 = & $encode 'hash script'
                }
            )
        }

        $stageRoot = Save-CloudOSDRunPackage -Package $package `
            -WindowsRoot $windowsRoot `
            -BridgeRoot $bridgeRoot `
            -BearerToken 'token-1' `
            -OsdClientPackage $osdClientPackage `
            -Downloader {
                param($Url,$OutFile,$Headers)
                Set-Content -LiteralPath $OutFile -Value $Url -Encoding ASCII
            }

        Test-Path -LiteralPath (Join-Path $stageRoot 'AutopilotAgent.msi') |
            Should -BeTrue
        $runJson = Get-Content -LiteralPath (Join-Path $stageRoot 'cloudosd-run.json') -Raw |
            ConvertFrom-Json
        $runJson.payloads.autopilotagent_msi.local_path |
            Should -Match 'AutopilotAgent\.msi'
        $runJson.payloads.autopilotagent_postinstall.local_path |
            Should -Match 'autopilotagent-postinstall\.ps1'
        $runJson.payloads.osd_client.local_path |
            Should -Match 'ProxmoxVEAutopilot.*OSD'

        $programData = Get-OfflineProgramDataPath -WindowsRoot $windowsRoot
        Test-Path -LiteralPath (Join-Path $programData 'ProxmoxVEAutopilot/OSD/OsdClient.ps1') |
            Should -BeTrue
        Test-Path -LiteralPath (Join-Path $programData 'ProxmoxVEAutopilot/OSD/Get-WindowsAutopilotInfo.ps1') |
            Should -BeTrue
        $config = Get-Content -LiteralPath (Join-Path $programData 'ProxmoxVEAutopilot/OSD/osd-config.json') -Raw |
            ConvertFrom-Json
        $config.flask_base_url | Should -Be 'https://autopilot.local'
        $config.flask_base_url_fallback | Should -Be 'http://192.168.2.4:5000'
        Test-Path -LiteralPath (Join-Path $windowsRoot 'Setup/Scripts/SetupComplete.cmd') |
            Should -BeFalse
    }
}

Describe 'Test-CloudOSDOfflineWindows' {
    It 'fails when required VirtIO boot drivers or first-boot chain are missing' {
        $windowsRoot = Join-Path $TestDrive 'Windows'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null

        $result = Test-CloudOSDOfflineWindows -WindowsRoot $windowsRoot

        $result.ok | Should -BeFalse
        ($result.errors -join '|') | Should -Match 'VirtIO storage'
        ($result.errors -join '|') | Should -Match 'SetupComplete'
    }

    It 'accepts VirtIO drivers staged in the offline DriverStore' {
        $offlineRoot = Join-Path $TestDrive 'offline'
        $windowsRoot = Join-Path $offlineRoot 'Windows'
        $driverStore = Join-Path $windowsRoot 'System32/DriverStore/FileRepository'
        foreach ($driver in @(
            @{ dir = 'vioscsi.inf_amd64_test'; inf = 'vioscsi.inf'; sys = 'vioscsi.sys' },
            @{ dir = 'netkvm.inf_amd64_test'; inf = 'netkvm.inf'; sys = 'netkvm.sys' }
        )) {
            $driverDir = Join-Path $driverStore $driver.dir
            New-Item -ItemType Directory -Path $driverDir -Force | Out-Null
            'inf' | Set-Content -LiteralPath (Join-Path $driverDir $driver.inf)
            'sys' | Set-Content -LiteralPath (Join-Path $driverDir $driver.sys)
        }
        $scripts = Join-Path $windowsRoot 'Setup/Scripts'
        New-Item -ItemType Directory -Path $scripts -Force | Out-Null
        'rem PVEAUTOPILOT-CLOUDOSD-SENTINEL' |
            Set-Content -LiteralPath (Join-Path $scripts 'SetupComplete.cmd') -Encoding ASCII
        Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot
        $specialize = Join-Path $windowsRoot 'Temp/osdcloud'
        New-Item -ItemType Directory -Path $specialize -Force | Out-Null
        'call C:\Windows\Setup\Scripts\PVEAutopilot-SetupComplete.cmd' |
            Set-Content -LiteralPath (Join-Path $specialize 'SetupSpecialize.cmd') -Encoding ASCII
        $stage = Join-Path $offlineRoot 'ProgramData/ProxmoxVEAutopilot/CloudOSD'
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        '{}' | Set-Content -LiteralPath (Join-Path $stage 'cloudosd-run.json')

        $result = Test-CloudOSDOfflineWindows -WindowsRoot $windowsRoot

        $result.ok | Should -BeTrue
    }

    It 'reports offline validation and SetupComplete milestones before PE completion' {
        $source = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..' 'Invoke-CloudOSDBridge.ps1') -Raw

        $source | Should -Match "EventType 'offline_validation_ok'"
        $source | Should -Match "Phase 'offline_validation'"
        $source | Should -Match "EventType 'setupcomplete_chained'"
        $source | Should -Match "Phase 'setupcomplete'"
    }
}

Describe 'Add-CloudOSDOfflineVirtIODrivers' {
    It 'prefers the VirtIO driver folder matching the selected Windows version' {
        $virtioRoot = Join-Path $TestDrive 'virtio-versioned'
        foreach ($osKey in @('w10', 'w11')) {
            $infDir = Join-Path $virtioRoot "netkvm/$osKey/amd64"
            New-Item -ItemType Directory -Path $infDir -Force | Out-Null
            'driver' | Set-Content -LiteralPath (Join-Path $infDir 'netkvm.inf')
        }

        $selected = Resolve-CloudOSDVirtioInf `
            -InfName 'netkvm.inf' `
            -PreferredOsKey 'w10' `
            -SearchRoots @($virtioRoot)

        $selected.FullName | Should -Match '[\\/]w10[\\/]amd64[\\/]netkvm\.inf$'
    }

    It 'injects required VirtIO drivers into the offline Windows image' {
        $windowsRoot = Join-Path $TestDrive 'offline/Windows'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null
        $virtioRoot = Join-Path $TestDrive 'virtio'
        foreach ($infName in @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')) {
            $infDir = Join-Path $virtioRoot "$infName/w11/amd64"
            New-Item -ItemType Directory -Path $infDir -Force | Out-Null
            'driver' | Set-Content -LiteralPath (Join-Path $infDir $infName)
        }
        $script:dismCalls = @()
        function global:dism.exe {
            $script:dismCalls += ,($args -join ' ')
            $global:LASTEXITCODE = 0
        }

        try {
            Add-CloudOSDOfflineVirtIODrivers -WindowsRoot $windowsRoot -SearchRoots @($virtioRoot)
        } finally {
            Remove-Item Function:\dism.exe -ErrorAction SilentlyContinue
        }

        $script:dismCalls.Count | Should -Be 3
        ($script:dismCalls -join ' ') | Should -Match '/Image:'
        ($script:dismCalls -join ' ') | Should -Match '/ForceUnsigned'
    }
}
