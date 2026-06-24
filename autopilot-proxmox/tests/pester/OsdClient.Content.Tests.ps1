$ErrorActionPreference = 'Stop'

BeforeAll {
    $repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
    $script:clientPath = Join-Path $repoRoot 'files\osd-client\OsdClient.ps1'
}

Describe 'OsdClient content materialization' {
    BeforeEach {
        $env:AUTOPILOT_OSD_CLIENT_LIBRARY_ONLY = '1'
        $env:ProgramData = Join-Path $TestDrive 'ProgramData'
        $env:WINDIR = Join-Path $TestDrive 'Windows'
        New-Item -ItemType Directory -Path $env:ProgramData -Force | Out-Null
        New-Item -ItemType Directory -Path $env:WINDIR -Force | Out-Null
        . $script:clientPath
        $script:Requests = @()
    }

    AfterEach {
        Remove-Item Env:\AUTOPILOT_OSD_CLIENT_LIBRARY_ONLY -ErrorAction SilentlyContinue
    }

    It 'reports staging and staged states around package download verification' {
        Mock Invoke-OsdRequest {
            $script:Requests += [pscustomobject]@{
                Path = $Path
                Body = $Body
                BearerToken = $BearerToken
            }
            return @{}
        }
        Mock Invoke-WebRequest {
            Set-Content -LiteralPath $OutFile -Value 'payload' -Encoding ASCII
        }
        Mock Get-FileHash {
            return [pscustomobject]@{ Hash = ('d' * 64) }
        }
        Mock Start-Process {
            return [pscustomobject]@{ ExitCode = 0 }
        }

        $stagePath = Join-Path $TestDrive 'Content\qga'
        $action = [pscustomobject]@{
            phase = 'full_os'
            content = @(
                [pscustomobject]@{
                    id = 'manifest-1'
                    logical_name = 'qemu-guest-agent'
                    source_uri = 'https://content.local/qga-107.msi'
                    sha256 = ('d' * 64)
                    staging_path = $stagePath
                }
            )
            params = [pscustomobject]@{
                install_command = 'msiexec.exe /i "{path}" /qn'
            }
        }
        $config = [pscustomobject]@{
            run_id = 'run-1'
            agent_id = 'osd-1'
            flask_base_url = 'https://autopilot.local'
        }

        Invoke-InstallPackage -Action $action -Config $config -BearerToken 'token-1'

        $script:Requests.Count | Should -Be 2
        $script:Requests[0].Path | Should -Be '/osd/v2/agent/content/manifest-1/stage'
        $script:Requests[0].Body.status | Should -Be 'staging'
        $script:Requests[0].Body.run_id | Should -Be 'run-1'
        $script:Requests[0].Body.agent_id | Should -Be 'osd-1'
        $script:Requests[0].Body.phase | Should -Be 'full_os'
        $script:Requests[0].Body.staging_path | Should -Be $stagePath
        $script:Requests[1].Body.status | Should -Be 'staged'
        $script:Requests[1].Body.staging_path | Should -Be $stagePath
    }

    It 'reports failed staging when hash verification fails' {
        Mock Invoke-OsdRequest {
            $script:Requests += [pscustomobject]@{
                Path = $Path
                Body = $Body
                BearerToken = $BearerToken
            }
            return @{}
        }
        Mock Invoke-WebRequest {
            Set-Content -LiteralPath $OutFile -Value 'payload' -Encoding ASCII
        }
        Mock Get-FileHash {
            return [pscustomobject]@{ Hash = ('e' * 64) }
        }

        $action = [pscustomobject]@{
            phase = 'full_os'
            content = @(
                [pscustomobject]@{
                    id = 'manifest-1'
                    logical_name = 'qemu-guest-agent'
                    source_uri = 'https://content.local/qga-107.msi'
                    sha256 = ('d' * 64)
                    staging_path = (Join-Path $TestDrive 'Content\qga')
                }
            )
            params = [pscustomobject]@{}
        }
        $config = [pscustomobject]@{
            run_id = 'run-1'
            agent_id = 'osd-1'
            flask_base_url = 'https://autopilot.local'
        }

        { Invoke-InstallPackage -Action $action -Config $config -BearerToken 'token-1' } |
            Should -Throw '*SHA256 mismatch*'

        $script:Requests.Count | Should -Be 2
        $script:Requests[0].Body.status | Should -Be 'staging'
        $script:Requests[1].Body.status | Should -Be 'failed'
        $script:Requests[1].Body.error | Should -Match 'SHA256 mismatch'
    }

    It 'runs a v2 package action through register next and result reporting' {
        Mock Invoke-OsdRequest {
            $script:Requests += [pscustomobject]@{
                Path = $Path
                Body = $Body
                BearerToken = $BearerToken
            }
            if ($Path -eq '/osd/v2/agent/register') {
                return [pscustomobject]@{ bearer_token = 'token-2' }
            }
            if ($Path -eq '/osd/v2/agent/next' -and $script:NextReturned) {
                return [pscustomobject]@{ bearer_token = 'token-4'; actions = @() }
            }
            if ($Path -eq '/osd/v2/agent/next') {
                $script:NextReturned = $true
                return [pscustomobject]@{
                    bearer_token = 'token-3'
                    actions = @(
                        [pscustomobject]@{
                            step_id = 'step-1'
                            kind = 'install_package'
                            phase = 'full_os'
                            params = [pscustomobject]@{
                                install_command = 'msiexec.exe /i "{path}" /qn'
                            }
                            content = @(
                                [pscustomobject]@{
                                    id = 'manifest-1'
                                    logical_name = 'notepad-plus-plus'
                                    source_uri = 'https://content.local/npp.msi'
                                    sha256 = ('d' * 64)
                                    staging_path = (Join-Path $TestDrive 'Content\npp')
                                }
                            )
                        }
                    )
                }
            }
            return [pscustomobject]@{ bearer_token = 'token-4' }
        }
        Mock Invoke-InstallPackage {}

        $script:NextReturned = $false
        $config = [pscustomobject]@{
            run_id = 'run-1'
            agent_id = 'osd-1'
            phase = 'full_os'
            bearer_token = 'token-1'
            flask_base_url = 'https://autopilot.local'
        }

        Invoke-OsdV2Client -Config $config

        $script:Requests[0].Path | Should -Be '/osd/v2/agent/register'
        $script:Requests[0].Body.run_id | Should -Be 'run-1'
        $script:Requests[0].Body.agent_id | Should -Be 'osd-1'
        $script:Requests[1].Path | Should -Be '/osd/v2/agent/next'
        $script:Requests[1].BearerToken | Should -Be 'token-2'
        $script:Requests[2].Path | Should -Be '/osd/v2/agent/step/step-1/result'
        $script:Requests[2].Body.status | Should -Be 'success'
        $script:Requests[2].Body.phase | Should -Be 'full_os'
        $script:Requests[3].Path | Should -Be '/osd/v2/agent/next'
        $script:Requests[4].Path | Should -Be '/osd/v2/agent/phase-complete'
        Should -Invoke Invoke-InstallPackage -Times 1 -Exactly
    }

    It 'uploads captured hashes to the v2 hash endpoint for v2 configs' {
        $osdRoot = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\OSD'
        New-Item -ItemType Directory -Path $osdRoot -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $osdRoot 'Get-WindowsAutopilotInfo.ps1') `
            -Value '# fake autopilot hash script' `
            -Encoding ASCII

        $createdCimShim = $false
        if (-not (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
            function global:Get-CimInstance {}
            $createdCimShim = $true
        }
        function global:powershell.exe {
            $outIndex = [Array]::IndexOf($args, '-OutputFile')
            if ($outIndex -lt 0) { throw 'missing -OutputFile' }
            $outFile = [string] $args[$outIndex + 1]
            @(
                'Device Serial Number,Windows Product ID,Hardware Hash',
                'CLOUDOSD-SERIAL,PRODUCT-ID,HASH-VALUE'
            ) | Set-Content -LiteralPath $outFile -Encoding ASCII
            $global:LASTEXITCODE = 0
        }
        try {
            Mock Get-CimInstance {
                return [pscustomobject]@{ SerialNumber = 'CLOUDOSD-SERIAL' }
            } -ParameterFilter { $ClassName -eq 'Win32_BIOS' }
            Mock Invoke-OsdRequest {
                $script:Requests += [pscustomobject]@{
                    Path = $Path
                    Body = $Body
                    BearerToken = $BearerToken
                }
                return @{}
            }
            $config = [pscustomobject]@{
                engine = 'v2'
                run_id = 'run-1'
                agent_id = 'osd-1'
                flask_base_url = 'https://autopilot.local'
            }

            Invoke-CaptureAutopilotHash -Config $config -BearerToken 'token-1'

            $script:Requests.Count | Should -Be 1
            $script:Requests[0].Path | Should -Be '/osd/v2/agent/hash'
            $script:Requests[0].Body.serial_number | Should -Be 'CLOUDOSD-SERIAL'
            $script:Requests[0].Body.product_id | Should -Be 'PRODUCT-ID'
            $script:Requests[0].Body.hardware_hash | Should -Be 'HASH-VALUE'
        }
        finally {
            Remove-Item Function:\powershell.exe -ErrorAction SilentlyContinue
            if ($createdCimShim) {
                Remove-Item Function:\Get-CimInstance -ErrorAction SilentlyContinue
            }
        }
    }

    It 'treats wait_agent_heartbeat as already satisfied for v2 CloudOSD clients' {
        $action = [pscustomobject]@{ kind = 'wait_agent_heartbeat' }
        $config = [pscustomobject]@{
            engine = 'v2'
            run_id = 'run-1'
            agent_id = 'osd-1'
            flask_base_url = 'https://autopilot.local'
        }

        { Invoke-OsdAction -Action $action -Config $config -BearerToken 'token-1' } |
            Should -Not -Throw
    }

    It 'installs AutopilotAgent for OSDeploy v2 agent install steps' {
        Mock Invoke-InstallAutopilotAgentForOsdeploy {}
        $action = [pscustomobject]@{ kind = 'install_autopilot_agent' }
        $config = [pscustomobject]@{
            engine = 'v2'
            run_id = 'run-1'
            agent_id = 'osd-1'
            flask_base_url = 'https://autopilot.local'
            osdeploy_agent = [pscustomobject]@{
                phase = 'full_os'
                bootstrap_token = 'bootstrap-token'
            }
        }

        Invoke-OsdAction -Action $action -Config $config -BearerToken 'token-1'

        Should -Invoke Invoke-InstallAutopilotAgentForOsdeploy -Times 1 -Exactly
    }

    It 'uses wait_agent_heartbeat as an OSDeploy agent-install fallback' {
        Mock Invoke-InstallAutopilotAgentForOsdeploy {}
        $action = [pscustomobject]@{ kind = 'wait_agent_heartbeat' }
        $config = [pscustomobject]@{
            engine = 'v2'
            run_id = 'run-1'
            agent_id = 'osd-1'
            flask_base_url = 'https://autopilot.local'
            osdeploy_agent = [pscustomobject]@{
                phase = 'full_os'
                bootstrap_token = 'bootstrap-token'
            }
        }

        Invoke-OsdAction -Action $action -Config $config -BearerToken 'token-1'

        Should -Invoke Invoke-InstallAutopilotAgentForOsdeploy -Times 1 -Exactly
    }

    It 'enables Remote Desktop and commits DHCP post-install config to AD while verifying an isolated domain controller role' {
        $script:DhcpAuthorized = $false
        $script:DhcpSecurityGroupsCommitted = $false
        $script:DhcpRestarted = $false
        $script:RdpRegistryValue = $null
        $script:RdpFirewallGroup = ''
        $script:RdpTermServiceStartup = ''
        $script:RdpTermServiceStarted = $false

        function Get-CimInstance {
            param([string] $ClassName)
            if ($ClassName -eq 'Win32_ComputerSystem') {
                return [pscustomobject]@{
                    Name = 'LABZ1-DC01'
                    Domain = 'test.gell.one'
                    PartOfDomain = $true
                    DomainRole = 5
                }
            }
            if ($ClassName -eq 'Win32_OperatingSystem') {
                return [pscustomobject]@{ Caption = 'Microsoft Windows Server 2022 Standard' }
            }
            return $null
        }
        function Get-Service {
            param([string] $Name)
            if ($Name -eq 'TermService') {
                return [pscustomobject]@{ Name = $Name; Status = 'Stopped' }
            }
            return [pscustomobject]@{ Name = $Name; Status = 'Running' }
        }
        function Set-ItemProperty {
            param([string] $Path, [string] $Name, [object] $Value)
            $Path | Should -Be 'HKLM:\System\CurrentControlSet\Control\Terminal Server'
            $Name | Should -Be 'fDenyTSConnections'
            $script:RdpRegistryValue = $Value
        }
        function Enable-NetFirewallRule {
            param([string] $DisplayGroup)
            $script:RdpFirewallGroup = $DisplayGroup
        }
        function Set-Service {
            param([string] $Name, [string] $StartupType)
            if ($Name -eq 'TermService') {
                $script:RdpTermServiceStartup = $StartupType
            }
        }
        function Start-Service {
            param([string] $Name)
            if ($Name -eq 'TermService') {
                $script:RdpTermServiceStarted = $true
            }
        }
        function Get-NetIPAddress {
            return [pscustomobject]@{ IPAddress = '192.168.16.10'; PrefixOrigin = 'Manual' }
        }
        function Get-ADDomain {
            return [pscustomobject]@{
                DNSRoot = 'test.gell.one'
                NetBIOSName = 'TEST'
                PDCEmulator = 'LABZ1-DC01.test.gell.one'
            }
        }
        function Get-ADForest {
            return [pscustomobject]@{
                Name = 'test.gell.one'
                RootDomain = 'test.gell.one'
                GlobalCatalogs = @('LABZ1-DC01.test.gell.one')
            }
        }
        function Get-DhcpServerInDC {
            if ($script:DhcpAuthorized) {
                return [pscustomobject]@{ DnsName = 'labz1-dc01.test.gell.one'; IPAddress = '192.168.16.10' }
            }
            return @()
        }
        function Get-ADGroup {
            param([string] $Identity)
            if ($script:DhcpSecurityGroupsCommitted -and $Identity -in @('DHCP Administrators', 'DHCP Users')) {
                return [pscustomobject]@{ Name = $Identity }
            }
            return $null
        }
        function Add-DhcpServerSecurityGroup {
            param([string] $ComputerName)
            $ComputerName | Should -Be 'LABZ1-DC01.test.gell.one'
            $script:DhcpSecurityGroupsCommitted = $true
        }
        function Add-DhcpServerInDC {
            param([string] $DnsName, [string] $IPAddress)
            $DnsName | Should -Be 'LABZ1-DC01.test.gell.one'
            $IPAddress | Should -Be '192.168.16.10'
            $script:DhcpAuthorized = $true
        }
        function Restart-Service {
            param([string] $Name)
            if ($Name -eq 'DHCPServer') { $script:DhcpRestarted = $true }
        }
        function Get-DhcpServerv4Scope {
            return [pscustomobject]@{
                ScopeId = [ipaddress] '192.168.16.0'
                Name = 'LABZ1 Clients'
                State = 'Active'
                StartRange = [ipaddress] '192.168.16.100'
                EndRange = [ipaddress] '192.168.16.199'
                SubnetMask = [ipaddress] '255.255.255.0'
            }
        }
        function Get-DnsServerZone {
            return @(
                [pscustomobject]@{ ZoneName = 'test.gell.one'; IsDsIntegrated = $true },
                [pscustomobject]@{ ZoneName = '_msdcs.test.gell.one'; IsDsIntegrated = $true }
            )
        }

        $action = [pscustomobject]@{
            kind = 'verify_isolated_domain_controller_role'
            params = [pscustomobject]@{
                forest_fqdn = 'test.gell.one'
                dhcp_scope = '192.168.16.0'
            }
        }

        $result = Invoke-OsdAction -Action $action -Config ([pscustomobject]@{}) -BearerToken 'token-1'

        $script:DhcpSecurityGroupsCommitted | Should -BeTrue
        $script:DhcpAuthorized | Should -BeTrue
        $script:DhcpRestarted | Should -BeTrue
        $script:RdpRegistryValue | Should -Be 0
        $script:RdpFirewallGroup | Should -Be 'Remote Desktop'
        $script:RdpTermServiceStartup | Should -Be 'Automatic'
        $script:RdpTermServiceStarted | Should -BeTrue
        $result.dc_readiness.ad_ds_ready | Should -BeTrue
        $result.dc_readiness.dns_ready | Should -BeTrue
        $result.dc_readiness.dhcp_ready | Should -BeTrue
        $result.dc_readiness.dhcp_security_groups_ready | Should -BeTrue
        $result.dc_readiness.dhcp_security_group_action | Should -Be 'created'
        $result.dc_readiness.dhcp_scope | Should -Be '192.168.16.0'
        $result.dc_readiness.dhcp_pool_start | Should -Be '192.168.16.100'
        $result.dc_readiness.dhcp_pool_end | Should -Be '192.168.16.199'
    }

    It 'installs the QGA watchdog script and scheduled task' {
        Mock Invoke-VerifyQga {}
        Mock Set-QgaServiceRecovery {}
        Mock Register-QgaWatchdogTask {}

        $action = [pscustomobject]@{
            params = [pscustomobject]@{
                task_interval_minutes = 7
                restart_interval_minutes = 31
            }
        }

        Invoke-InstallQgaWatchdog -Action $action

        $watchdogPath = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\OSD\QgaWatchdog.ps1'
        $statePath = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\OSD\qga-watchdog-last-restart.txt'
        Test-Path -LiteralPath $watchdogPath | Should -BeTrue
        Test-Path -LiteralPath $statePath | Should -BeTrue
        $watchdog = Get-Content -LiteralPath $watchdogPath -Raw
        $watchdog | Should -Match 'QEMU-GA'
        $watchdog | Should -Match 'Restart-Service -Name QEMU-GA'
        $watchdog | Should -Match '--retry-path'
        $watchdog | Should -Match '--block-rpcs=guest-network-get-interfaces'
        $watchdog | Should -Match 'org\.qemu\.guest_agent\.0'
        $watchdog | Should -Match 'Invoke-CimMethod'
        $watchdog | Should -Match 'MethodName Change'
        $watchdog | Should -Not -Match 'sc\.exe config QEMU-GA binPath='
        $watchdog | Should -Match 'restartIntervalMinutes = 31'

        Should -Invoke Invoke-VerifyQga -Times 1 -Exactly
        Should -Invoke Set-QgaServiceRecovery -Times 1 -Exactly
        Should -Invoke Register-QgaWatchdogTask -Times 1 -Exactly -ParameterFilter {
            $ScriptPath -eq $watchdogPath -and $TaskIntervalMinutes -eq 7
        }
    }
}
