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
}
