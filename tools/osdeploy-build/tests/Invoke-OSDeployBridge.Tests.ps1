BeforeAll {
    $env:OSDEPLOY_BRIDGE_LIBRARY_ONLY = '1'
    . (Join-Path $PSScriptRoot '..' 'Invoke-OSDeployBridge.ps1')
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
