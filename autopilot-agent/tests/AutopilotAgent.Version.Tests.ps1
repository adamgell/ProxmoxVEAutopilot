BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..\scripts\AutopilotAgent.Version.psm1'
    Import-Module $modulePath -Force
}

Describe 'ConvertTo-AutopilotAgentMsiVersion' {
    It 'maps the public CalVer release to a Windows Installer-safe version' {
        ConvertTo-AutopilotAgentMsiVersion -Version '2026.8.2' | Should -Be '226.8.2'
    }

    It 'keeps a supported legacy MSI version unchanged' {
        ConvertTo-AutopilotAgentMsiVersion -Version '0.1.4' | Should -Be '0.1.4'
    }

    It 'rejects a CalVer year outside the supported MSI mapping window' {
        { ConvertTo-AutopilotAgentMsiVersion -Version '2056.1.0' } |
            Should -Throw '*2000 through 2055*'
    }
}
