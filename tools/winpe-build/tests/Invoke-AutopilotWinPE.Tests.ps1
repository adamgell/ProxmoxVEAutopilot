BeforeAll {
    $script:AgentPath = (Resolve-Path "$PSScriptRoot/../Invoke-AutopilotWinPE.ps1").Path
    . $script:AgentPath
}

Describe 'Read-AgentConfig' {
    It 'returns the parsed config from a JSON file' {
        $tmp = [System.IO.Path]::GetTempFileName()
        '{"flask_base_url": "http://10.0.0.1:5000", "build_sha": "abc"}' |
            Set-Content -Path $tmp -Encoding UTF8
        try {
            $cfg = Read-AgentConfig -Path $tmp
            $cfg.flask_base_url | Should -Be 'http://10.0.0.1:5000'
            $cfg.build_sha | Should -Be 'abc'
        } finally {
            Remove-Item $tmp -Force
        }
    }

    It 'throws on missing file' {
        { Read-AgentConfig -Path '/nonexistent' } | Should -Throw
    }
}

Describe 'Write-AgentLog' {
    It 'appends to the log file with a timestamp prefix' {
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            Write-AgentLog -Path $tmp -Level 'INFO' -Message 'hello'
            $content = Get-Content $tmp
            $content | Should -Match '\[INFO\]'
            $content | Should -Match 'hello'
        } finally {
            Remove-Item $tmp -Force
        }
    }
}
