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

Describe 'CloudOSD cache integration' {
    It 'patches the OSDCloud catalog FilePath for a ready cached feature image without changing hash metadata' {
        $moduleRoot = Join-Path $TestDrive 'OSDCloudCacheModule'
        $catalogDir = Join-Path $moduleRoot 'catalogs/operatingsystem'
        New-Item -ItemType Directory -Path $catalogDir -Force | Out-Null
        $catalogPath = Join-Path $catalogDir '26200.8246-win11-25h2.xml'
        @'
<Catalog>
  <File>
    <FileName>win11-25h2.esd</FileName>
    <FilePath>https://download.microsoft.test/win11-25h2.esd</FilePath>
    <Sha256>abcdef</Sha256>
  </File>
</Catalog>
'@ | Set-Content -LiteralPath $catalogPath -Encoding UTF8
        $package = [pscustomobject]@{
            cache = [pscustomobject]@{
                feature_image = [pscustomobject]@{
                    hit = $true
                    entry_id = 'cache-entry-1'
                    catalog_file = 'catalogs/operatingsystem/26200.8246-win11-25h2.xml'
                    file_name = 'win11-25h2.esd'
                    expected_sha256 = 'abcdef'
                    download_url = 'https://autopilot.test/api/cloudosd/cache/cache-entry-1/download/win11-25h2.esd?token=run'
                }
            }
        }

        $result = Set-CloudOSDFeatureImageCacheSource -Package $package -ModuleRoot $moduleRoot

        $result.applied | Should -BeTrue
        [xml] $catalog = Get-Content -LiteralPath $catalogPath -Raw
        $catalog.Catalog.File.FilePath | Should -Be 'https://autopilot.test/api/cloudosd/cache/cache-entry-1/download/win11-25h2.esd?token=run'
        $catalog.Catalog.File.Sha256 | Should -Be 'abcdef'
    }

    It 'applies cached quality updates to the offline Windows image with DISM' {
        $offlineRoot = Join-Path $TestDrive 'offline'
        $windowsRoot = Join-Path $offlineRoot 'Windows'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null
        $script:dismCalls = @()
        $package = [pscustomobject]@{
            cache = [pscustomobject]@{
                quality_updates = @(
                    [pscustomobject]@{
                        status = 'ready'
                        file_name = 'windows11.0-kb5089549-x64.msu'
                        url = 'https://autopilot.test/api/cloudosd/cache/update/download/windows11.0-kb5089549-x64.msu?token=run'
                    }
                )
            }
        }

        $result = Invoke-CloudOSDQualityUpdateServicing `
            -Package $package `
            -WindowsRoot $windowsRoot `
            -DownloadRoot (Join-Path $TestDrive 'quality-updates') `
            -Downloader {
                param($Url, $OutFile)
                'msu' | Set-Content -LiteralPath $OutFile -Encoding ASCII
            } `
            -DismRunner {
                param($ImageRoot, $PackagePath)
                $script:dismCalls += @{ ImageRoot = $ImageRoot; PackagePath = $PackagePath }
                return 0
            }

        $result.applied | Should -Be 1
        $script:dismCalls.Count | Should -Be 1
        $script:dismCalls[0].ImageRoot | Should -Be $offlineRoot
        $script:dismCalls[0].PackagePath | Should -Match 'windows11\.0-kb5089549-x64\.msu'
    }

    It 'fails the PE phase when cached quality update servicing fails' {
        $offlineRoot = Join-Path $TestDrive 'offline-fail'
        $windowsRoot = Join-Path $offlineRoot 'Windows'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null
        $package = [pscustomobject]@{
            cache = [pscustomobject]@{
                quality_updates = @(
                    [pscustomobject]@{
                        status = 'ready'
                        file_name = 'windows11.0-kb5089549-x64.msu'
                        url = 'https://autopilot.test/api/cloudosd/cache/update/download/windows11.0-kb5089549-x64.msu?token=run'
                    }
                )
            }
        }

        {
            Invoke-CloudOSDQualityUpdateServicing `
                -Package $package `
                -WindowsRoot $windowsRoot `
                -DownloadRoot (Join-Path $TestDrive 'quality-updates-fail') `
                -Downloader {
                    param($Url, $OutFile)
                    'msu' | Set-Content -LiteralPath $OutFile -Encoding ASCII
                } `
                -DismRunner {
                    param($ImageRoot, $PackagePath)
                    return 112
                }
        } | Should -Throw '*DISM failed*'
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

    It 'writes oobeSystem locale and OOBE suppression so CloudOSD does not stop at Region' {
        $windowsRoot = Join-Path $TestDrive 'WindowsOobe'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null

        Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot -ComputerName 'GELL-AD-001'

        $content = Get-Content -LiteralPath (Join-Path $windowsRoot 'Panther/Unattend.xml') -Raw
        $content | Should -Match '<settings pass="oobeSystem">'
        $content | Should -Match 'Microsoft-Windows-International-Core'
        $content | Should -Match '<InputLocale>en-US</InputLocale>'
        $content | Should -Match '<SystemLocale>en-US</SystemLocale>'
        $content | Should -Match '<UILanguage>en-US</UILanguage>'
        $content | Should -Match '<UserLocale>en-US</UserLocale>'
        $content | Should -Match '<OOBE>'
        $content | Should -Match '<HideEULAPage>true</HideEULAPage>'
        $content | Should -Match '<HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>'
        $content | Should -Match '<HideLocalAccountScreen>true</HideLocalAccountScreen>'
        $content | Should -Match '<HideOnlineAccountScreens>true</HideOnlineAccountScreens>'
        $content | Should -Match '<HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>'
        $content | Should -Match '<ProtectYourPC>3</ProtectYourPC>'
        $content | Should -Match '<UserAccounts>'
        $content | Should -Match '<LocalAccounts>'
        $content | Should -Match '<Name>PVEAutopilot</Name>'
        $content | Should -Match '<Group>Administrators</Group>'
        $content | Should -Match '<AutoLogon>'
        $content | Should -Match '<Enabled>true</Enabled>'
        $content | Should -Match '<Username>PVEAutopilot</Username>'
        $content | Should -Match '<LogonCount>1</LogonCount>'
        $content | Should -Not -Match 'Nsta1200'
    }

    It 'writes Microsoft-Windows-UnattendedJoin when domain join is requested' {
        $windowsRoot = Join-Path $TestDrive 'WindowsDomain'
        New-Item -ItemType Directory -Path $windowsRoot -Force | Out-Null
        $domainJoin = [pscustomobject]@{
            enabled = $true
            domain_fqdn = 'home.gell.one'
            credential_domain = 'HOME'
            username = 'svc-cloudjoin'
            password = 'join-secret'
            ou_path = 'OU=CloudOSD,DC=home,DC=gell,DC=one'
        }

        Add-PVEAutopilotSpecializeUnattend `
            -WindowsRoot $windowsRoot `
            -ComputerName 'GELL-AD-001' `
            -DomainJoin $domainJoin

        $content = Get-Content -LiteralPath (Join-Path $windowsRoot 'Panther/Unattend.xml') -Raw
        $content | Should -Match 'Microsoft-Windows-UnattendedJoin'
        $content | Should -Match '<JoinDomain>home\.gell\.one</JoinDomain>'
        $content | Should -Match '<Domain>HOME</Domain>'
        $content | Should -Match '<Username>svc-cloudjoin</Username>'
        $content | Should -Match '<Password>join-secret</Password>'
        $content | Should -Match '<MachineObjectOU>OU=CloudOSD,DC=home,DC=gell,DC=one</MachineObjectOU>'
    }

    It 'refuses to overwrite an existing non-autopilot UnattendedJoin component' {
        $windowsRoot = Join-Path $TestDrive 'WindowsExistingDomain'
        $panther = Join-Path $windowsRoot 'Panther'
        New-Item -ItemType Directory -Path $panther -Force | Out-Null
        @'
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-UnattendedJoin" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <Identification>
        <JoinDomain>other.example</JoinDomain>
      </Identification>
    </component>
  </settings>
</unattend>
'@ | Set-Content -LiteralPath (Join-Path $panther 'Unattend.xml') -Encoding UTF8
        $domainJoin = [pscustomobject]@{
            enabled = $true
            domain_fqdn = 'home.gell.one'
            credential_domain = 'HOME'
            username = 'svc-cloudjoin'
            password = 'join-secret'
        }

        { Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot -DomainJoin $domainJoin } |
            Should -Throw '*existing Microsoft-Windows-UnattendedJoin*'
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
        $virtioRoot = Join-Path $TestDrive 'VirtIOPayloads'
        New-Item -ItemType Directory -Path $windowsRoot, $bridgeRoot, (Join-Path $virtioRoot 'guest-agent') -Force | Out-Null
        'firstboot' | Set-Content -LiteralPath (Join-Path $bridgeRoot 'PVEAutopilot-FirstBoot.ps1')
        'qga-msi' | Set-Content -LiteralPath (Join-Path $virtioRoot 'guest-agent/qemu-ga-x86_64.msi') -Encoding ASCII
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
            -QgaSearchRoots @($virtioRoot) `
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

    It 'stages QEMU Guest Agent MSI from the attached VirtIO media for first boot' {
        $windowsRoot = Join-Path $TestDrive 'WindowsQga'
        $bridgeRoot = Join-Path $TestDrive 'BridgeQga'
        $virtioRoot = Join-Path $TestDrive 'VirtIO'
        New-Item -ItemType Directory -Path $windowsRoot, $bridgeRoot, (Join-Path $virtioRoot 'guest-agent') -Force | Out-Null
        'firstboot' | Set-Content -LiteralPath (Join-Path $bridgeRoot 'PVEAutopilot-FirstBoot.ps1')
        'qga-msi' | Set-Content -LiteralPath (Join-Path $virtioRoot 'guest-agent/qemu-ga-x86_64.msi') -Encoding ASCII
        $package = [pscustomobject]@{
            run_id = 'run-qga'
            server_base_url = 'https://autopilot.local'
            payloads = [pscustomobject]@{}
        }

        $stageRoot = Save-CloudOSDRunPackage -Package $package `
            -WindowsRoot $windowsRoot `
            -BridgeRoot $bridgeRoot `
            -BearerToken 'token-1' `
            -QgaSearchRoots @($virtioRoot)

        $stagedMsi = Join-Path $stageRoot 'qemu-ga-x86_64.msi'
        Test-Path -LiteralPath $stagedMsi | Should -BeTrue
        Get-Content -LiteralPath $stagedMsi -Raw | Should -Match 'qga-msi'
    }

    It 'fails when the VirtIO QEMU Guest Agent MSI cannot be staged for first boot' {
        $windowsRoot = Join-Path $TestDrive 'WindowsQgaMissing'
        $bridgeRoot = Join-Path $TestDrive 'BridgeQgaMissing'
        $virtioRoot = Join-Path $TestDrive 'VirtIOMissing'
        New-Item -ItemType Directory -Path $windowsRoot, $bridgeRoot, $virtioRoot -Force | Out-Null
        'firstboot' | Set-Content -LiteralPath (Join-Path $bridgeRoot 'PVEAutopilot-FirstBoot.ps1')
        $package = [pscustomobject]@{
            run_id = 'run-qga-missing'
            server_base_url = 'https://autopilot.local'
            payloads = [pscustomobject]@{}
        }

        {
            Save-CloudOSDRunPackage -Package $package `
                -WindowsRoot $windowsRoot `
                -BridgeRoot $bridgeRoot `
                -BearerToken 'token-1' `
                -QgaSearchRoots @($virtioRoot)
        } | Should -Throw '*QEMU Guest Agent MSI not found*'
    }

    It 'redacts PE-only domain join secrets from the offline run package' {
        $windowsRoot = Join-Path $TestDrive 'WindowsRedact'
        $bridgeRoot = Join-Path $TestDrive 'BridgeRedact'
        $virtioRoot = Join-Path $TestDrive 'VirtIORedact'
        New-Item -ItemType Directory -Path $windowsRoot, $bridgeRoot, (Join-Path $virtioRoot 'guest-agent') -Force | Out-Null
        'firstboot' | Set-Content -LiteralPath (Join-Path $bridgeRoot 'PVEAutopilot-FirstBoot.ps1')
        'qga-msi' | Set-Content -LiteralPath (Join-Path $virtioRoot 'guest-agent/qemu-ga-x86_64.msi') -Encoding ASCII
        $package = [pscustomobject]@{
            run_id = 'run-domain'
            bearer_token = 'pe-token'
            server_base_url = 'https://autopilot.local'
            domain_join = [pscustomobject]@{
                enabled = $true
                domain_fqdn = 'home.gell.one'
                credential_domain = 'HOME'
                username = 'svc-cloudjoin'
                password = 'join-secret'
                ou_path = 'OU=CloudOSD,DC=home,DC=gell,DC=one'
            }
            payloads = [pscustomobject]@{}
        }

        $stageRoot = Save-CloudOSDRunPackage -Package $package `
            -WindowsRoot $windowsRoot `
            -BridgeRoot $bridgeRoot `
            -BearerToken 'token-1' `
            -QgaSearchRoots @($virtioRoot)

        $jsonText = Get-Content -LiteralPath (Join-Path $stageRoot 'cloudosd-run.json') -Raw
        $jsonText | Should -Not -Match 'join-secret'
        $jsonText | Should -Not -Match 'svc-cloudjoin'
        $jsonText | Should -Not -Match 'pe-token'
        $json = $jsonText | ConvertFrom-Json
        $json.domain_join.enabled | Should -BeTrue
        $json.domain_join.domain_fqdn | Should -Be 'home.gell.one'
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

    It 'fails domain validation when required UnattendedJoin XML is missing' {
        $offlineRoot = Join-Path $TestDrive 'offline-domain'
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
        Add-PVEAutopilotSpecializeUnattend -WindowsRoot $windowsRoot -ComputerName 'GELL-AD-001'
        $specialize = Join-Path $windowsRoot 'Temp/osdcloud'
        New-Item -ItemType Directory -Path $specialize -Force | Out-Null
        'call C:\Windows\Setup\Scripts\PVEAutopilot-SetupComplete.cmd' |
            Set-Content -LiteralPath (Join-Path $specialize 'SetupSpecialize.cmd') -Encoding ASCII
        $stage = Join-Path $offlineRoot 'ProgramData/ProxmoxVEAutopilot/CloudOSD'
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        '{"domain_join":{"enabled":true,"domain_fqdn":"home.gell.one"}}' |
            Set-Content -LiteralPath (Join-Path $stage 'cloudosd-run.json')

        $result = Test-CloudOSDOfflineWindows `
            -WindowsRoot $windowsRoot `
            -DomainJoin ([pscustomobject]@{ enabled = $true; domain_fqdn = 'home.gell.one' })

        $result.ok | Should -BeFalse
        ($result.errors -join '|') | Should -Match 'UnattendedJoin'
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
