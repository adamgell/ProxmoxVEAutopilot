BeforeAll {
    $script:BuildScript = (Resolve-Path "$PSScriptRoot/../build-cloudosd.ps1").Path
}

Describe 'build-cloudosd.ps1 parameter validation' {
    It 'accepts -Arch amd64' {
        { & $script:BuildScript -Arch amd64 -DryRun } | Should -Not -Throw
    }

    It 'rejects unsupported architectures for the first CloudOSD path' {
        { & $script:BuildScript -Arch arm64 -DryRun } | Should -Throw
    }

    It 'emits CloudOSD artifact paths during dry run' {
        $out = & $script:BuildScript -Arch amd64 -DryRun
        ($out -join "`n") | Should -Match 'cloudosd-autopilot-amd64-'
        ($out -join "`n") | Should -Match '\.iso'
        ($out -join "`n") | Should -Match '\.wim'
        ($out -join "`n") | Should -Match '\.json'
    }

    It 'detects both root-level and arch-specific ADK copype layouts' {
        $content = Get-Content -LiteralPath $script:BuildScript -Raw
        $content | Should -Match 'Resolve-CopypePath'
        $content | Should -Match 'copype\.cmd'
        $content | Should -Match '\$Arch\\copype\.cmd'
    }

    It 'pins and stages OSDCloud into the mounted WIM' {
        $content = Get-Content -LiteralPath $script:BuildScript -Raw
        $content | Should -Match '26\.4\.17\.1'
        $content | Should -Match 'Save-Module'
        $content | Should -Match 'OSDCloud'
    }

    It 'stages curl.exe because OSDCloud requires it in WinPE' {
        $content = Get-Content -LiteralPath $script:BuildScript -Raw
        $content | Should -Match 'Resolve-CurlPath'
        $content | Should -Match 'System32\\curl\.exe'
        $content | Should -Match 'curl_source'
        $content | Should -Match 'curl_source_url'
        $content | Should -Match 'curl-8\.20\.0_2-win64-mingw\.zip'
        $content | Should -Match '57b07ba8f3634ffb7773d7fe1321f720316d11acc2ed5654fce97589c2e8a7d1'
        $content | Should -Match 'Get-CloudOSDExecutableMachine'
    }

    It 'bakes the computed build sha into the PE bridge config' {
        $content = Get-Content -LiteralPath $script:BuildScript -Raw
        $content | Should -Match 'Write-CloudOSDConfigForBuild'
        $content | Should -Match 'config\.build_sha = \$BuildSha'
        $content | Should -Match '-BuildSha \$sha'
        $content | Should -Match "'build-cloudosd\.ps1'"
    }
}
