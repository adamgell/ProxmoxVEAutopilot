BeforeAll {
    $env:OSDEPLOY_BRIDGE_LIBRARY_ONLY = '1'
    $script:BridgePath = (Resolve-Path "$PSScriptRoot/../Invoke-OSDeployBridge.ps1").Path
    . $script:BridgePath
}

AfterAll {
    Remove-Item Env:\OSDEPLOY_BRIDGE_LIBRARY_ONLY -ErrorAction SilentlyContinue
}

Describe 'Invoke-OSDeployImageApply' {
    BeforeEach {
        $script:WindowsDriveRoot = Join-Path $TestDrive 'windows-drive'
        New-Item -ItemType Directory -Path $script:WindowsDriveRoot -Force | Out-Null
        New-PSDrive -Name W -PSProvider FileSystem -Root $script:WindowsDriveRoot -Scope Global | Out-Null
    }

    AfterEach {
        Remove-PSDrive -Name W -Force -ErrorAction SilentlyContinue
    }

    It 'fails when DISM exits successfully but the Windows image is missing' {
        Mock Invoke-OSDeployNativeProcessWithHeartbeat {
            [pscustomobject]@{
                ExitCode = 0
                Stdout = 'The operation completed successfully.'
                Stderr = ''
            }
        }

        {
            Invoke-OSDeployImageApply `
                -InstallImage 'D:\sources\install.wim' `
                -ImageIndex 4 `
                -WindowsDrive 'W:' `
                -BaseUrl 'http://controller' `
                -RunId 'run-1' `
                -BearerToken 'token'
        } | Should -Throw '*ntoskrnl.exe*'
    }

    It 'accepts a DISM success only after core Windows files exist' {
        Mock Invoke-OSDeployNativeProcessWithHeartbeat {
            [pscustomobject]@{
                ExitCode = 0
                Stdout = 'The operation completed successfully.'
                Stderr = ''
            }
        }
        New-Item -ItemType Directory -Path 'W:\Windows\System32\Config' -Force | Out-Null
        'kernel' | Set-Content -LiteralPath 'W:\Windows\System32\ntoskrnl.exe'
        'hive' | Set-Content -LiteralPath 'W:\Windows\System32\Config\SYSTEM'

        {
            Invoke-OSDeployImageApply `
                -InstallImage 'D:\sources\install.wim' `
                -ImageIndex 4 `
                -WindowsDrive 'W:' `
                -BaseUrl 'http://controller' `
                -RunId 'run-1' `
                -BearerToken 'token'
        } | Should -Not -Throw
    }
}

Describe 'Save-OSDeployOsdClientPackage' {
    BeforeEach {
        $script:OfflineProgramDataRoot = Join-Path $TestDrive 'ProgramData'
        New-Item -ItemType Directory -Path $script:OfflineProgramDataRoot -Force | Out-Null
        Mock Get-OSDeployOfflineProgramDataPath { return $script:OfflineProgramDataRoot }
        Mock Invoke-RestMethod {
            [pscustomobject]@{
                config = [pscustomobject]@{
                    engine = 'v2'
                    api_version = 2
                    flask_base_url = ''
                    run_id = 'run-qga'
                    phase = 'full_os'
                    bearer_token = 'osd-token'
                }
                files = @()
            }
        }
    }

    It 'stages QEMU Guest Agent MSI from the provided VirtIO media root into the offline OSD root' {
        $windowsRoot = Join-Path $TestDrive 'Windows'
        $virtioRoot = Join-Path $TestDrive 'VirtIO'
        New-Item -ItemType Directory -Path $windowsRoot, (Join-Path $virtioRoot 'guest-agent') -Force | Out-Null
        'qga-msi' | Set-Content -LiteralPath (Join-Path $virtioRoot 'guest-agent/qemu-ga-x86_64.msi') -Encoding ASCII
        $package = [pscustomobject]@{
            server_base_url = 'https://autopilot.local'
            payloads = [pscustomobject]@{
                osd_client = [pscustomobject]@{
                    url = 'https://autopilot.local/osd/v2/agent/package/run-qga?phase=full_os'
                }
            }
        }

        $osdRoot = Save-OSDeployOsdClientPackage `
            -Package $package `
            -WindowsRoot $windowsRoot `
            -BearerToken 'token-1' `
            -BaseUrl 'https://autopilot.local' `
            -RunId 'run-qga' `
            -QgaSearchRoots @($virtioRoot)

        $stagedMsi = Join-Path $osdRoot 'guest-agent/qemu-ga-x86_64.msi'
        Test-Path -LiteralPath $stagedMsi | Should -BeTrue
        Get-Content -LiteralPath $stagedMsi -Raw | Should -Match 'qga-msi'
    }

    It 'fails during PE staging when the QEMU Guest Agent MSI is missing from the provided media root' {
        $windowsRoot = Join-Path $TestDrive 'WindowsMissingQga'
        $virtioRoot = Join-Path $TestDrive 'VirtIOMissingQga'
        New-Item -ItemType Directory -Path $windowsRoot, $virtioRoot -Force | Out-Null
        $package = [pscustomobject]@{
            server_base_url = 'https://autopilot.local'
            payloads = [pscustomobject]@{
                osd_client = [pscustomobject]@{
                    url = 'https://autopilot.local/osd/v2/agent/package/run-qga?phase=full_os'
                }
            }
        }

        {
            Save-OSDeployOsdClientPackage `
                -Package $package `
                -WindowsRoot $windowsRoot `
                -BearerToken 'token-1' `
                -BaseUrl 'https://autopilot.local' `
                -RunId 'run-qga' `
                -QgaSearchRoots @($virtioRoot)
        } | Should -Throw '*QEMU Guest Agent MSI not found*'
    }
}
