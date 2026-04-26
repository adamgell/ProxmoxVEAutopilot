BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Modules' 'Autopilot.PETransport' 'Autopilot.PETransport.psd1'
    Import-Module $modulePath -Force

    # On non-Windows platforms Get-CimInstance does not exist; define a stub so
    # Pester can register a mock against it inside the module scope.
    if (-not (Get-Command -Name Get-CimInstance -ErrorAction SilentlyContinue)) {
        function global:Get-CimInstance { param([string]$ClassName) }
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
