BeforeAll {
    $script:BuildScript = (Resolve-Path "$PSScriptRoot/../build-winpe.ps1").Path
}

Describe 'build-winpe.ps1 parameter validation' {
    It 'accepts -Arch amd64' {
        { & $script:BuildScript -Arch amd64 -DryRun } | Should -Not -Throw
    }

    It 'accepts -Arch arm64' {
        { & $script:BuildScript -Arch arm64 -DryRun } | Should -Not -Throw
    }

    It 'rejects unknown -Arch values' {
        { & $script:BuildScript -Arch x86 -DryRun } | Should -Throw
    }

    It 'emits a manifest path to stdout when -DryRun is set' {
        $out = & $script:BuildScript -Arch amd64 -DryRun
        ($out -join "`n") | Should -Match 'winpe-autopilot-amd64-'
    }
}
