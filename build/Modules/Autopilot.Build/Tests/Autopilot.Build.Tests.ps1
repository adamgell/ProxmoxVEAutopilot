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
