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
