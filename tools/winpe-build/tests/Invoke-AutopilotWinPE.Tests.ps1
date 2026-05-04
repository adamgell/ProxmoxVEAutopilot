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

Describe 'Invoke-ActionLoop' {
    It 'runs each action and refreshes the bearer token from step results' {
        $script:invoked = @()
        $script:tokens = @('initial')
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:invoked += "$Method $Uri"
            if ($Uri -match '/step/\d+/result$') {
                return [pscustomobject]@{ ok = $true; bearer_token = "tok-$($script:invoked.Count)" }
            }
            return [pscustomobject]@{ ok = $true }
        }
        $handlers = @{
            'partition_disk' = { param($p, $tok) }
            'apply_wim'      = { param($p, $tok) }
        }
        $actions = @(
            @{ step_id = 1; kind = 'partition_disk'; params = @{} },
            @{ step_id = 2; kind = 'apply_wim'; params = @{} }
        )
        $finalToken = Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers $handlers -RestInvoker $invoker
        $finalToken | Should -Not -Be 'initial'
        ($script:invoked | Where-Object { $_ -match 'POST.*/step/1/result' }).Count |
            Should -Be 2  # running + ok
    }

    It 'aborts the loop on handler failure and posts state=error' {
        $script:reported = @()
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            if ($Uri -match '/step/\d+/result$') {
                $script:reported += $Body
                return [pscustomobject]@{ ok = $true; bearer_token = 'rolling' }
            }
            return [pscustomobject]@{ ok = $true }
        }
        $handlers = @{
            'apply_wim' = { param($p, $tok) throw "disk too small" }
        }
        $actions = @(@{ step_id = 99; kind = 'apply_wim'; params = @{} })
        { Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers $handlers -RestInvoker $invoker } |
            Should -Throw '*disk too small*'
        ($script:reported -join '|') | Should -Match 'error'
        ($script:reported -join '|') | Should -Match 'disk too small'
    }

    It 'fails fast when no handler is registered for a kind' {
        $invoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            return [pscustomobject]@{ ok = $true; bearer_token = 'rolling' }
        }
        $actions = @(@{ step_id = 1; kind = 'unknown_kind'; params = @{} })
        { Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers @{} -RestInvoker $invoker } |
            Should -Throw '*no handler*unknown_kind*'
    }
}

Describe 'Invoke-Action-PartitionDisk' {
    It 'emits a diskpart script with Recovery before C: for layout=recovery_before_c' {
        $captured = $null
        $runner = { param($script) $script:captured = $script }
        Invoke-Action-PartitionDisk -Params @{ layout = 'recovery_before_c' } `
            -DiskpartRunner $runner
        $script:captured | Should -Match 'select disk 0'
        $script:captured | Should -Match 'create partition efi size=100'
        $script:captured | Should -Match 'create partition msr size=16'
        # Recovery comes before Windows
        $idxRecovery = $script:captured.IndexOf("create partition primary size=1024")
        $idxOs = $script:captured.IndexOf("create partition primary",
                                          $idxRecovery + 1)
        $idxRecovery | Should -BeLessThan $idxOs
        $idxRecovery | Should -BeGreaterThan -1
        $idxOs | Should -BeGreaterThan -1
    }

    It 'rejects unknown layout values' {
        { Invoke-Action-PartitionDisk -Params @{ layout = 'unknown' } `
            -DiskpartRunner { param($s) } } | Should -Throw '*layout*'
    }
}

Describe 'Invoke-Action-ApplyWim' {
    It 'invokes dism /apply-image with index resolved by metadata name' {
        $script:invocations = @()
        $dismRunner = { param($a) $script:invocations += ,$a
            return @{ ExitCode = 0; Stdout = ''; Stderr = '' } }
        $resolveIndex = { param($wim,$name) 6 }   # known mock index
        $resolveSource = { 'D:\sources\install.wim' }
        Invoke-Action-ApplyWim `
            -Params @{ image_index_metadata_name = 'Windows 11 Enterprise' } `
            -DismRunner $dismRunner `
            -SourceWimResolver $resolveSource `
            -IndexResolver $resolveIndex
        $applied = $script:invocations[0]
        ($applied -join ' ') | Should -Match '/Apply-Image'
        ($applied -join ' ') | Should -Match '/ImageFile:D:\\sources\\install.wim'
        ($applied -join ' ') | Should -Match '/Index:6'
        ($applied -join ' ') | Should -Match '/ApplyDir:V:\\\\'
    }

    It 'throws on dism non-zero exit' {
        $dismRunner = { param($a)
            return @{ ExitCode = 5; Stdout = ''; Stderr = 'access denied' } }
        { Invoke-Action-ApplyWim `
            -Params @{ image_index_metadata_name = 'X' } `
            -DismRunner $dismRunner `
            -SourceWimResolver { 'D:\x.wim' } `
            -IndexResolver { 1 } } | Should -Throw '*dism*5*'
    }
}

Describe 'Invoke-Action-InjectDrivers' {
    It 'invokes dism /add-driver against the VirtIO ISO root with /recurse' {
        $script:invocations = @()
        $dismRunner = { param($a) $script:invocations += ,$a
            return @{ ExitCode = 0; Stdout = '' } }
        $resolveVirtio = { 'E:\' }
        Invoke-Action-InjectDrivers `
            -Params @{ required_infs = @('vioscsi.inf') } `
            -DismRunner $dismRunner `
            -VirtioPathResolver $resolveVirtio
        $args = $script:invocations[0] -join ' '
        $args | Should -Match '/Image:V:\\\\'
        $args | Should -Match '/Add-Driver'
        $args | Should -Match '/Driver:E:\\'
        $args | Should -Match '/Recurse'
        $args | Should -Match '/ForceUnsigned'
    }

    It 'throws when the VirtIO source cannot be located' {
        { Invoke-Action-InjectDrivers `
            -Params @{ required_infs = @('vioscsi.inf') } `
            -DismRunner { param($a) @{ ExitCode = 0 } } `
            -VirtioPathResolver { throw 'no virtio iso found' } } |
            Should -Throw '*virtio*'
    }
}
