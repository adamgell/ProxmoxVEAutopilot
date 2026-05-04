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

Describe 'Get-VMIdentity' {
    It 'returns uuid and mac from injected resolvers' {
        $uuidResolver = { '11111111-2222-3333-4444-555555555555' }
        $macResolver = { '00:11:22:33:44:55' }
        $id = Get-VMIdentity -UuidResolver $uuidResolver -MacResolver $macResolver
        $id.vm_uuid | Should -Be '11111111-2222-3333-4444-555555555555'
        $id.mac | Should -Be '00:11:22:33:44:55'
    }

    It 'normalizes uuid to lowercase' {
        $uuidResolver = { 'AABBCCDD-EEFF-0011-2233-445566778899' }
        $macResolver = { 'aa:bb:cc:dd:ee:ff' }
        $id = Get-VMIdentity -UuidResolver $uuidResolver -MacResolver $macResolver
        $id.vm_uuid | Should -Be 'aabbccdd-eeff-0011-2233-445566778899'
    }

    It 'throws when resolver returns empty' {
        { Get-VMIdentity -UuidResolver { '' } -MacResolver { 'aa' } } |
            Should -Throw '*UUID*'
    }
}

Describe 'Invoke-OrchestratorRequest' {
    BeforeAll {
        function global:_MockInvokeRest {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:lastUri = $Uri
            $script:lastMethod = $Method
            $script:lastHeaders = $Headers
            $script:lastBody = $Body
            return [pscustomobject]@{ ok = $true; uri = $Uri }
        }
    }

    It 'sends a POST with bearer header when token provided' {
        $r = Invoke-OrchestratorRequest -BaseUrl 'http://x:5000' `
            -Path '/winpe/register' -Method POST `
            -Body @{ vm_uuid = 'u' } -BearerToken 'tok' `
            -RestInvoker (Get-Item Function:_MockInvokeRest).ScriptBlock
        $script:lastUri | Should -Be 'http://x:5000/winpe/register'
        $script:lastMethod | Should -Be 'POST'
        $script:lastHeaders.Authorization | Should -Be 'Bearer tok'
        $r.ok | Should -BeTrue
    }

    It 'omits bearer header when token is null' {
        $r = Invoke-OrchestratorRequest -BaseUrl 'http://x:5000' `
            -Path '/winpe/register' -Method POST -Body @{} `
            -RestInvoker (Get-Item Function:_MockInvokeRest).ScriptBlock
        $script:lastHeaders.ContainsKey('Authorization') | Should -BeFalse
    }

    It 'falls back to FallbackBaseUrl when BaseUrl exhausts retries' {
        $script:visited = @()
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:visited += $Uri
            if ($Uri -match '^http://primary') { throw 'connection refused' }
            return [pscustomobject]@{ ok = $true; uri = $Uri }
        }
        $r = Invoke-OrchestratorRequest -BaseUrl 'http://primary:5000' `
            -Path '/winpe/register' -Method POST -Body @{} `
            -FallbackBaseUrl 'http://fallback:5000' `
            -MaxAttempts 2 -RetryDelayMs 1 -RestInvoker $invoker
        $r.ok | Should -BeTrue
        @($script:visited | Where-Object { $_ -match 'primary' }).Count | Should -Be 2
        @($script:visited | Where-Object { $_ -match 'fallback' }).Count | Should -BeGreaterThan 0
    }

    It 'retries on transient failure up to MaxAttempts then throws' {
        $script:attempts = 0
        $boom = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:attempts++
            throw [System.Net.WebException]::new('connection refused')
        }
        { Invoke-OrchestratorRequest -BaseUrl 'http://x:5000' `
            -Path '/x' -Method GET -RestInvoker $boom `
            -MaxAttempts 3 -RetryDelayMs 1 } |
            Should -Throw '*connection refused*'
        $script:attempts | Should -Be 3
    }
}
