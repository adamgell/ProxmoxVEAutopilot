BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'Autopilot.Build.psd1'
    Import-Module $modulePath -Force
}

Describe 'Autopilot.Build module' {
    It 'imports without error' {
        Get-Module Autopilot.Build | Should -Not -BeNullOrEmpty
    }
    It 'exports no functions yet (skeleton only)' {
        (Get-Command -Module Autopilot.Build).Count | Should -Be 0
    }
}
