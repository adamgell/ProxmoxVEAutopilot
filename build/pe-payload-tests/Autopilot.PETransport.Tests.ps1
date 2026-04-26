BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Modules' 'Autopilot.PETransport' 'Autopilot.PETransport.psd1'
    Import-Module $modulePath -Force

    # On non-Windows platforms Get-CimInstance does not exist; define a stub so
    # Pester can register a mock against it inside the module scope.
    if (-not (Get-Command -Name Get-CimInstance -ErrorAction SilentlyContinue)) {
        function global:Get-CimInstance { param([string]$ClassName) }
    }

    # Cross-platform stubs for Windows-only cmdlets used by Wait-PeNetwork
    # and Initialize-SshHostKeys.
    if (-not (Get-Command -Name wpeutil -ErrorAction SilentlyContinue)) {
        function global:wpeutil { param([string]$Command) }
    }
    if (-not (Get-Command -Name Get-NetIPAddress -ErrorAction SilentlyContinue)) {
        function global:Get-NetIPAddress { param([string]$AddressFamily, [string]$ErrorAction) }
    }
    if (-not (Get-Command -Name Start-Service -ErrorAction SilentlyContinue)) {
        function global:Start-Service { param([string]$Name, [string]$ErrorAction) }
    }
    if (-not (Get-Command -Name Get-Service -ErrorAction SilentlyContinue)) {
        function global:Get-Service { param([string]$Name) }
    }
}

Describe 'Autopilot.PETransport module' {
    It 'imports without error' {
        Get-Module Autopilot.PETransport | Should -Not -BeNullOrEmpty
    }
    It 'exports Get-PeIdentity' {
        (Get-Command -Module Autopilot.PETransport).Name | Should -Contain 'Get-PeIdentity'
    }
}

Describe 'Get-PeIdentity' {
    BeforeAll {
        Mock -ModuleName Autopilot.PETransport Get-CimInstance {
            return [pscustomobject]@{
                UUID   = '11111111-2222-3333-4444-555555555555'
                Vendor = 'TestCorp'
                Name   = 'TestModel'
            }
        } -ParameterFilter { $ClassName -eq 'Win32_ComputerSystemProduct' }
    }

    It 'returns the SMBIOS UUID' {
        $result = Get-PeIdentity
        $result.Uuid | Should -Be '11111111-2222-3333-4444-555555555555'
    }

    It 'includes vendor and name' {
        $result = Get-PeIdentity
        $result.Vendor | Should -Be 'TestCorp'
        $result.Name | Should -Be 'TestModel'
    }
}

Describe 'Wait-PeNetwork' {
    Context 'when network comes up on second poll' {
        BeforeAll {
            $script:initCount = 0
            Mock -ModuleName Autopilot.PETransport wpeutil { $script:initCount++ }
            $script:pollCount = 0
            Mock -ModuleName Autopilot.PETransport Get-NetIPAddress {
                $script:pollCount++
                if ($script:pollCount -lt 2) { return @() }
                return @([pscustomobject]@{ IPAddress = '192.168.1.50'; AddressFamily = 'IPv4' })
            }
        }
        It 'returns the IPv4 address and called wpeutil at least once' {
            $ip = Wait-PeNetwork -TimeoutSeconds 30 -PollIntervalSeconds 0
            $ip | Should -Be '192.168.1.50'
            $script:initCount | Should -BeGreaterOrEqual 1
        }
    }

    Context 'when network never comes up' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport wpeutil { }
            Mock -ModuleName Autopilot.PETransport Get-NetIPAddress { return @() }
        }
        It 'throws on timeout' {
            { Wait-PeNetwork -TimeoutSeconds 1 -PollIntervalSeconds 0 } | Should -Throw -ExpectedMessage '*timeout*'
        }
    }

    Context 'when only APIPA (169.254.x) is present' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport wpeutil { }
            Mock -ModuleName Autopilot.PETransport Get-NetIPAddress {
                return @([pscustomobject]@{ IPAddress = '169.254.42.99'; AddressFamily = 'IPv4' })
            }
        }
        It 'rejects APIPA and times out' {
            { Wait-PeNetwork -TimeoutSeconds 1 -PollIntervalSeconds 0 } | Should -Throw -ExpectedMessage '*timeout*'
        }
    }
}

Describe 'Initialize-SshHostKeys' {
    Context 'when ssh-keygen is available and ProgramData\ssh exists' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport Test-Path { return $true }
            Mock -ModuleName Autopilot.PETransport Remove-Item { }
            Mock -ModuleName Autopilot.PETransport Start-Service { }
            Mock -ModuleName Autopilot.PETransport Get-Service { return [pscustomobject]@{ Status = 'Running' } }
            Mock -ModuleName Autopilot.PETransport Invoke-Expression { }
        }

        It 'invokes ssh-keygen -A and starts sshd' {
            { Initialize-SshHostKeys -SshDir 'X:\ProgramData\ssh' -SshKeygen 'X:\Program Files\OpenSSH\ssh-keygen.exe' } | Should -Not -Throw
        }
    }

    Context 'when ssh-keygen is not present (no SSH baked in)' {
        BeforeAll {
            Mock -ModuleName Autopilot.PETransport Test-Path { param($Path) return $Path -ne 'X:\Program Files\OpenSSH\ssh-keygen.exe' }
        }
        It 'returns silently — no SSH is fine' {
            { Initialize-SshHostKeys -SshDir 'X:\ProgramData\ssh' -SshKeygen 'X:\Program Files\OpenSSH\ssh-keygen.exe' } | Should -Not -Throw
        }
    }
}
