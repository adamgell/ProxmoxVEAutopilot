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
