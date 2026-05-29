BeforeAll {
    $env:OSDEPLOY_BRIDGE_LIBRARY_ONLY = '1'
    . (Join-Path $PSScriptRoot '..' 'Invoke-OSDeployBridge.ps1')
}

AfterAll {
    Remove-Item Env:\OSDEPLOY_BRIDGE_LIBRARY_ONLY -ErrorAction SilentlyContinue
}

Describe 'Test-OSDeployExistingWindowsInstall' {
    It 'detects an existing Windows install before disk cleanup' {
        $root = Join-Path $TestDrive 'existing'
        $kernel = Join-Path $root 'Windows/System32/ntoskrnl.exe'
        New-Item -ItemType Directory -Path (Split-Path -Parent $kernel) -Force | Out-Null
        Set-Content -LiteralPath $kernel -Value 'kernel' -Encoding ASCII
        $driveName = 'OSDTEST'
        New-PSDrive -Name $driveName -PSProvider FileSystem -Root $root | Out-Null
        try {
            $result = Test-OSDeployExistingWindowsInstall
            $result.Found | Should -BeTrue
            $result.KernelPath | Should -Match 'ntoskrnl\.exe'
        } finally {
            Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
        }
    }

    It 'ignores excluded roots' {
        $root = Join-Path $TestDrive 'excluded'
        $kernel = Join-Path $root 'Windows/System32/ntoskrnl.exe'
        New-Item -ItemType Directory -Path (Split-Path -Parent $kernel) -Force | Out-Null
        Set-Content -LiteralPath $kernel -Value 'kernel' -Encoding ASCII
        $driveName = 'OSDEXCL'
        $drive = New-PSDrive -Name $driveName -PSProvider FileSystem -Root $root
        try {
            $result = Test-OSDeployExistingWindowsInstall -ExcludedRoots @($drive.Root)
            $result.Found | Should -BeFalse
        } finally {
            Remove-PSDrive -Name $driveName -Force -ErrorAction SilentlyContinue
        }
    }
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
