BeforeAll {
    # Cross-platform stubs for Windows-only commands (so Pester can mock them on macOS)
    if (-not (Get-Command wpeutil -ErrorAction SilentlyContinue)) {
        function global:wpeutil { param() }
    }
    if (-not (Get-Command bcdboot -ErrorAction SilentlyContinue)) {
        function global:bcdboot { param() }
    }

    $modulePath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Modules' 'Autopilot.PESteps' 'Autopilot.PESteps.psd1'
    Import-Module $modulePath -Force
}

Describe 'Autopilot.PESteps module' {
    It 'imports' {
        Get-Module Autopilot.PESteps | Should -Not -BeNullOrEmpty
    }
    It 'exports the four simple step cmdlets' {
        $names = (Get-Command -Module Autopilot.PESteps).Name
        $names | Should -Contain 'Invoke-LogStep'
        $names | Should -Contain 'Invoke-RebootStep'
        $names | Should -Contain 'Invoke-ShutdownStep'
        $names | Should -Contain 'Invoke-BcdbootStep'
    }
}

Describe 'Invoke-LogStep' {
    It 'returns the message verbatim' {
        $r = Invoke-LogStep -Message 'hello'
        $r.LogTail | Should -Be 'hello'
    }
}

Describe 'Invoke-RebootStep' {
    BeforeAll {
        $script:wpeArgs = @()
        Mock -ModuleName Autopilot.PESteps wpeutil { $script:wpeArgs += $args }
    }
    It 'invokes wpeutil reboot' {
        Invoke-RebootStep
        $script:wpeArgs | Should -Contain 'reboot'
    }
}

Describe 'Invoke-ShutdownStep' {
    BeforeAll {
        $script:wpeArgs = @()
        Mock -ModuleName Autopilot.PESteps wpeutil { $script:wpeArgs += $args }
    }
    It 'invokes wpeutil shutdown' {
        Invoke-ShutdownStep
        $script:wpeArgs | Should -Contain 'shutdown'
    }
}

Describe 'Invoke-BcdbootStep' {
    BeforeAll {
        $script:bcdArgs = @()
        $script:bcdExitCode = 0
        Mock -ModuleName Autopilot.PESteps bcdboot {
            $script:bcdArgs = $args
            $global:LASTEXITCODE = $script:bcdExitCode
        }
    }

    It 'invokes bcdboot with /s and /f UEFI' {
        $script:bcdExitCode = 0
        Invoke-BcdbootStep -Windows 'W:' -Esp 'S:'
        $script:bcdArgs[0] | Should -Be 'W:\Windows'
        ($script:bcdArgs -join ' ') | Should -Match '/s S:'
        ($script:bcdArgs -join ' ') | Should -Match '/f UEFI'
    }

    It 'throws on non-zero exit' {
        $script:bcdExitCode = 1
        { Invoke-BcdbootStep -Windows 'W:' -Esp 'S:' } | Should -Throw -ExpectedMessage '*bcdboot*'
    }
}

Describe 'Invoke-PartitionStep' {
    BeforeAll {
        # Storage cmdlets are Windows-only; stub for macOS Pester
        foreach ($cmd in 'Get-Disk','Clear-Disk','Initialize-Disk','New-Partition','Format-Volume','Set-Partition','Add-PartitionAccessPath','Get-Partition') {
            if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
                $stubBody = "param() Write-Verbose 'stub $cmd'"
                Invoke-Expression "function global:$cmd { $stubBody }"
            }
        }

        Mock -ModuleName Autopilot.PESteps Get-Disk { return [pscustomobject]@{ Number = 0; FriendlyName = 'fake' } }
        Mock -ModuleName Autopilot.PESteps Clear-Disk { }
        Mock -ModuleName Autopilot.PESteps Initialize-Disk { }
        $script:partitions = @(
            [pscustomobject]@{ Type = 'System';   DriveLetter = $null; PartitionNumber = 1; AccessPaths = @() },
            [pscustomobject]@{ Type = 'Reserved'; DriveLetter = $null; PartitionNumber = 2; AccessPaths = @() },
            [pscustomobject]@{ Type = 'Basic';    DriveLetter = 'W';   PartitionNumber = 3; AccessPaths = @('W:\') }
        )
        $script:newCalls = 0
        Mock -ModuleName Autopilot.PESteps New-Partition {
            $p = $script:partitions[$script:newCalls]
            $script:newCalls++
            return $p
        }
        Mock -ModuleName Autopilot.PESteps Format-Volume { }
        Mock -ModuleName Autopilot.PESteps Set-Partition { }
        Mock -ModuleName Autopilot.PESteps Add-PartitionAccessPath {
            $script:partitions[0].DriveLetter = 'S'
            $script:partitions[0].AccessPaths = @('S:\')
        }
        Mock -ModuleName Autopilot.PESteps Get-Partition {
            param($DiskNumber, $PartitionNumber)
            return $script:partitions[$PartitionNumber - 1]
        }
    }

    BeforeEach {
        $script:newCalls = 0
    }

    It 'returns esp and windows drive letters' {
        $r = Invoke-PartitionStep -Layout 'uefi-standard'
        $r.Extra.esp | Should -Be 'S:'
        $r.Extra.windows | Should -Be 'W:'
    }

    It 'rejects unknown layouts' {
        { Invoke-PartitionStep -Layout 'novel-experimental' } | Should -Throw -ExpectedMessage '*layout*'
    }
}
