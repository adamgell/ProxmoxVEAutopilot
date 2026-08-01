BeforeAll {
    $script:Publisher = Join-Path $PSScriptRoot '../Publish-SetupCmModule.ps1'
}

Describe 'Publish-SetupCmModule' {
    It 'publishes a hash-pinned runtime-only module archive' {
        $source = Join-Path $TestDrive 'setup-cm'
        $module = Join-Path $source 'src/SetupCm'
        $scripts = Join-Path $source 'scripts'
        $destination = Join-Path $TestDrive 'Modules'
        New-Item -ItemType Directory -Force -Path $module | Out-Null
        New-Item -ItemType Directory -Force -Path $scripts | Out-Null
        Set-Content -LiteralPath (Join-Path $scripts 'Invoke-SetupCm.ps1') -Value '# entry point'
        Set-Content -LiteralPath (Join-Path $module 'SetupCm.psd1') -Value '# manifest'
        Set-Content -LiteralPath (Join-Path $module 'SetupCm.psm1') -Value '# root module'
        Set-Content -LiteralPath (Join-Path $source 'lab.local.yaml') -Value 'secret: never-package'
        Push-Location $source
        try {
            git init --quiet
            git config user.email 'setup-cm-test@example.invalid'
            git config user.name 'Setup-CM Test'
            git add scripts src
            git commit --quiet -m 'test source'
        }
        finally {
            Pop-Location
        }

        $result = & $script:Publisher -SetupCmRepository $source -DestinationRoot $destination | ConvertFrom-Json

        $result.sha256 | Should -Match '^[A-Fa-f0-9]{64}$'
        Test-Path -LiteralPath $result.archive_path -PathType Leaf | Should -BeTrue
        Test-Path -LiteralPath $result.manifest_path -PathType Leaf | Should -BeTrue
        (Get-FileHash -LiteralPath $result.archive_path -Algorithm SHA256).Hash | Should -Be $result.sha256
        $manifest = Get-Content -LiteralPath $result.manifest_path -Raw | ConvertFrom-Json
        $manifest.source_commit | Should -Match '^[A-Fa-f0-9]{40}$'
        $extract = Join-Path $TestDrive 'extract'
        Expand-Archive -LiteralPath $result.archive_path -DestinationPath $extract
        Test-Path -LiteralPath (Join-Path $extract 'scripts/Invoke-SetupCm.ps1') | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $extract 'src/SetupCm/SetupCm.psm1') | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $extract 'lab.local.yaml') | Should -BeFalse
    }
}
