BeforeAll {
    $modulePath = Join-Path $PSScriptRoot '..' 'Autopilot.Build.psd1'
    Import-Module $modulePath -Force
}

Describe 'Autopilot.Build module' {
    It 'imports without error' {
        Get-Module Autopilot.Build | Should -Not -BeNullOrEmpty
    }
    It 'exports Write-CmTraceLog' {
        (Get-Command -Module Autopilot.Build).Name | Should -Contain 'Write-CmTraceLog'
    }
}

Describe 'Write-CmTraceLog' {
    BeforeEach {
        $script:tmpLog = Join-Path ([System.IO.Path]::GetTempPath()) "Autopilot.Build.test-$([guid]::NewGuid()).log"
    }
    AfterEach {
        if (Test-Path $script:tmpLog) { Remove-Item $script:tmpLog -Force }
    }

    It 'writes a single line in cmtrace format' {
        Write-CmTraceLog -Path $script:tmpLog -Message 'hello' -Severity Info -Component 'Test'
        $content = Get-Content $script:tmpLog -Raw
        $content | Should -Match '<!\[LOG\[hello\]LOG\]!>'
        $content | Should -Match 'component="Test"'
        $content | Should -Match 'type="1"'
    }

    It 'maps severity to type correctly (Info=1, Warning=2, Error=3)' {
        Write-CmTraceLog -Path $script:tmpLog -Message 'a' -Severity Info -Component 'T'
        Write-CmTraceLog -Path $script:tmpLog -Message 'b' -Severity Warning -Component 'T'
        Write-CmTraceLog -Path $script:tmpLog -Message 'c' -Severity Error -Component 'T'
        $lines = Get-Content $script:tmpLog
        $lines[0] | Should -Match 'type="1"'
        $lines[1] | Should -Match 'type="2"'
        $lines[2] | Should -Match 'type="3"'
    }

    It 'appends to an existing log without truncating' {
        Write-CmTraceLog -Path $script:tmpLog -Message 'first' -Severity Info -Component 'T'
        Write-CmTraceLog -Path $script:tmpLog -Message 'second' -Severity Info -Component 'T'
        (Get-Content $script:tmpLog).Count | Should -Be 2
    }

    It 'creates the log file if it does not exist' {
        Test-Path $script:tmpLog | Should -BeFalse
        Write-CmTraceLog -Path $script:tmpLog -Message 'x' -Severity Info -Component 'T'
        Test-Path $script:tmpLog | Should -BeTrue
    }
}

Describe 'Get-FileSha256' {
    It 'returns sha256 of a known string ("hello world\n" → b94d27...)' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "sha256-test-$([guid]::NewGuid())"
        try {
            [System.IO.File]::WriteAllBytes($tmp, [byte[]](0x68,0x65,0x6c,0x6c,0x6f,0x20,0x77,0x6f,0x72,0x6c,0x64,0x0a))
            (Get-FileSha256 -Path $tmp) | Should -Be 'a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447'
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    It 'returns lowercase hex' {
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "sha256-test-$([guid]::NewGuid())"
        try {
            'x' | Set-Content -Path $tmp -NoNewline -Encoding ascii
            $h = Get-FileSha256 -Path $tmp
            $h | Should -MatchExactly '^[0-9a-f]{64}$'
        } finally {
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws on missing file' {
        { Get-FileSha256 -Path '/no/such/path/abc.bin' } | Should -Throw
    }
}

Describe 'New-BuildLock' {
    BeforeEach {
        $script:lockDir = Join-Path ([System.IO.Path]::GetTempPath()) "buildlock-test-$([guid]::NewGuid())"
        New-Item -ItemType Directory -Path $script:lockDir | Out-Null
        $script:lockPath = Join-Path $script:lockDir '.build.lock'
    }
    AfterEach {
        if (Test-Path $script:lockDir) { Remove-Item $script:lockDir -Recurse -Force }
    }

    It 'acquires when no lock file exists' {
        $lock = New-BuildLock -Path $script:lockPath -Owner 'TestScript'
        Test-Path $script:lockPath | Should -BeTrue
        $lock | Should -Not -BeNullOrEmpty
    }

    It 'records owner and PID in the lock file' {
        $null = New-BuildLock -Path $script:lockPath -Owner 'TestScript'
        $content = Get-Content $script:lockPath -Raw | ConvertFrom-Json
        $content.owner | Should -Be 'TestScript'
        $content.pid | Should -Be $PID
    }

    It 'releases via the returned object''s Release() method' {
        $lock = New-BuildLock -Path $script:lockPath -Owner 'TestScript'
        Test-Path $script:lockPath | Should -BeTrue
        $lock.Release()
        Test-Path $script:lockPath | Should -BeFalse
    }

    It 'throws when the lock is held by a live PID' {
        $null = New-BuildLock -Path $script:lockPath -Owner 'First'
        { New-BuildLock -Path $script:lockPath -Owner 'Second' } | Should -Throw -ExpectedMessage '*held*'
    }

    It 'reclaims when the recorded PID is dead' {
        @{ owner = 'GhostScript'; pid = 999999; acquiredAt = (Get-Date).ToString('o') } |
            ConvertTo-Json | Set-Content -Path $script:lockPath
        $lock = New-BuildLock -Path $script:lockPath -Owner 'Reclaimer'
        $lock | Should -Not -BeNullOrEmpty
        $content = Get-Content $script:lockPath -Raw | ConvertFrom-Json
        $content.owner | Should -Be 'Reclaimer'
    }
}

Describe 'Write-ArtifactSidecar' {
    BeforeEach {
        $script:sidecarPath = Join-Path ([System.IO.Path]::GetTempPath()) "sidecar-test-$([guid]::NewGuid()).json"
    }
    AfterEach {
        if (Test-Path $script:sidecarPath) { Remove-Item $script:sidecarPath -Force }
    }

    It 'writes a JSON file with required keys' {
        Write-ArtifactSidecar -Path $script:sidecarPath -Properties @{
            kind = 'install-wim'
            sha256 = 'a' * 64
            size = 1234
            extra = @{ buildHost = 'buildhost' }
        }
        $obj = Get-Content $script:sidecarPath -Raw | ConvertFrom-Json
        $obj.kind   | Should -Be 'install-wim'
        $obj.sha256 | Should -Be ('a' * 64)
        $obj.size   | Should -Be 1234
        $obj.extra.buildHost | Should -Be 'buildhost'
    }

    It 'pretty-prints (indented) JSON' {
        Write-ArtifactSidecar -Path $script:sidecarPath -Properties @{
            kind = 'pe-wim'; sha256 = 'b' * 64; size = 0
        }
        $raw = Get-Content $script:sidecarPath -Raw
        $raw | Should -Match "`n  "
    }

    It 'throws if Properties lacks required keys' {
        { Write-ArtifactSidecar -Path $script:sidecarPath -Properties @{ kind = 'install-wim' } } |
            Should -Throw -ExpectedMessage '*required*'
    }
}
