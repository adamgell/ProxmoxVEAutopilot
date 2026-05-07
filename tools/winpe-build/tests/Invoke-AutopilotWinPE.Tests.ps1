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
    It 'resolves MAC from a physical adapter before DHCP marks it IP-enabled' {
        $mac = Resolve-MacAddress `
            -AdapterResolver {
                @(
                    [pscustomobject]@{
                        MACAddress = 'AA:BB:CC:DD:EE:FF'
                        PhysicalAdapter = $true
                        NetEnabled = $true
                        InterfaceIndex = 7
                        Index = 7
                    }
                )
            } `
            -IpAdapterResolver {
                @(
                    [pscustomobject]@{
                        MACAddress = $null
                        IPEnabled = $false
                        InterfaceIndex = 7
                        Index = 7
                    }
                )
            }
        $mac | Should -Be 'AA:BB:CC:DD:EE:FF'
    }

    It 'falls back to IP-enabled adapter config when physical adapters have no MAC' {
        $mac = Resolve-MacAddress `
            -AdapterResolver {
                @(
                    [pscustomobject]@{
                        MACAddress = $null
                        PhysicalAdapter = $true
                        NetEnabled = $true
                        InterfaceIndex = 7
                        Index = 7
                    }
                )
            } `
            -IpAdapterResolver {
                @(
                    [pscustomobject]@{
                        MACAddress = '11:22:33:44:55:66'
                        IPEnabled = $true
                        InterfaceIndex = 4
                        Index = 4
                    }
                )
            }
        $mac | Should -Be '11:22:33:44:55:66'
    }

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

    It 'converts JSON PSCustomObject params to hashtables before invoking handlers' {
        $script:seenType = $null
        $script:captured = $null
        $invoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            return [pscustomobject]@{ ok = $true; bearer_token = 'rolling' }
        }
        $handlers = @{
            'partition_disk' = {
                param($p, $tok)
                $script:seenType = $p.GetType().FullName
                Invoke-Action-PartitionDisk -Params $p -DiskpartRunner {
                    param($script)
                    $script:captured = $script
                }
            }
        }
        $actions = @(
            [pscustomobject]@{
                step_id = 1
                kind = 'partition_disk'
                params = [pscustomobject]@{ layout = 'recovery_before_c' }
            }
        )

        Invoke-ActionLoop -BaseUrl 'http://x:5000' -BearerToken 'initial' `
            -Actions $actions -Handlers $handlers -RestInvoker $invoker

        $script:seenType | Should -Be 'System.Collections.Hashtable'
        $script:captured | Should -Match 'select disk 0'
    }

    It 'does not emit handler stdout as part of the returned bearer token' {
        $script:stepPosts = 0
        $invoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            if ($Uri -match '/step/\d+/result$') {
                $script:stepPosts++
                return [pscustomobject]@{ ok = $true; bearer_token = "tok-$script:stepPosts" }
            }
            return [pscustomobject]@{ ok = $true }
        }
        $handlers = @{
            'partition_disk' = {
                param($p, $tok)
                Write-Output 'diskpart chatter'
            }
        }
        $actions = @(@{ step_id = 1; kind = 'partition_disk'; params = @{} })

        $finalToken = Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers $handlers -RestInvoker $invoker

        $finalToken | Should -Be 'tok-2'
        $finalToken | Should -Not -BeOfType [object[]]
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
        ($applied -join ' ') | Should -Match '/ApplyDir:V:\\'
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
    It 'adds only the selected modern VirtIO driver INFs to the installed image' {
        $script:invocations = @()
        $dismRunner = { param($a) $script:invocations += ,$a
            return @{ ExitCode = 0; Stdout = '' } }
        $tempRoot = [System.IO.Path]::GetTempPath()
        $root = New-Item -Type Directory -Path (Join-Path $tempRoot "virtio-$(New-Guid)")
        try {
            foreach ($path in @(
                'NetKVM/2k12/amd64/netkvm.inf',
                'NetKVM/w11/amd64/netkvm.inf',
                'amd64/w11/vioscsi.inf',
                'vioserial/w11/amd64/vioser.inf'
            )) {
                $full = Join-Path $root.FullName $path
                New-Item -Type Directory -Path (Split-Path -Parent $full) -Force | Out-Null
                Set-Content -LiteralPath $full -Value '[Version]' -Encoding ASCII
            }

            Invoke-Action-InjectDrivers `
                -Params @{ required_infs = @('vioscsi.inf','netkvm.inf','vioser.inf') } `
                -DismRunner $dismRunner `
                -VirtioPathResolver { $root.FullName }

            $script:invocations.Count | Should -Be 3
            $joined = ($script:invocations | ForEach-Object { $_ -join ' ' }) -join "`n"
            $joined | Should -Match '/Image:V:\\'
            $joined | Should -Match '/Add-Driver'
            $joined | Should -Match 'w11'
            $joined | Should -Match 'netkvm\.inf'
            $joined | Should -Match 'vioscsi\.inf'
            $joined | Should -Match 'vioser\.inf'
            $joined | Should -Not -Match '/Recurse'
            $joined | Should -Not -Match '2k12'
        } finally {
            Remove-Item -LiteralPath $root.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'throws when the VirtIO source cannot be located' {
        { Invoke-Action-InjectDrivers `
            -Params @{ required_infs = @('vioscsi.inf') } `
            -DismRunner { param($a) @{ ExitCode = 0 } } `
            -VirtioPathResolver { throw 'no virtio iso found' } } |
            Should -Throw '*virtio*'
    }

    It 'skips missing VirtIO media when the driver package is optional' {
        { Invoke-Action-InjectDrivers `
            -Params @{ required_infs = @('vioscsi.inf'); optional = $true } `
            -DismRunner { param($a) throw 'DISM should not run' } `
            -VirtioPathResolver { throw 'no virtio iso found' } } |
            Should -Not -Throw
    }
}

Describe 'Invoke-Action-ValidateBootDrivers' {
    It 'passes when all required INFs are present' {
        $resolver = { @('vioscsi.inf', 'netkvm.inf', 'vioser.inf', 'extra.inf') }
        { Invoke-Action-ValidateBootDrivers `
            -Params @{ required_infs = @('vioscsi.inf','netkvm.inf','vioser.inf') } `
            -DriverInfResolver $resolver } | Should -Not -Throw
    }

    It 'throws listing every missing INF' {
        $resolver = { @('vioscsi.inf') }   # netkvm + vioser missing
        { Invoke-Action-ValidateBootDrivers `
            -Params @{ required_infs = @('vioscsi.inf','netkvm.inf','vioser.inf') } `
            -DriverInfResolver $resolver } |
            Should -Throw '*netkvm.inf*vioser.inf*'
    }
}

Describe '_GetInjectedDriverInfs (parses dism /Format:List output)' {
    It 'extracts the leaf INF name from each "Original File Name" line' {
        # Realistic dism /Get-Drivers /Format:List shape (truncated):
        #   Published Name : oem3.inf
        #   Original File Name : E:\NetKVM\w11\amd64\netkvm.inf
        #   Inbox : No
        #   Class Name : Net
        #   ...
        $sampleOutput = @"
Deployment Image Servicing and Management tool
Version: 10.0.26100.1

Image Version: 10.0.26100.1

Driver packages listing:

Published Name : oem3.inf
Original File Name : E:\NetKVM\w11\amd64\netkvm.inf
Inbox : No
Class Name : Net
Provider Name : Red Hat, Inc.
Date : 1/8/2025
Version : 100.95.104.26200

Published Name : oem4.inf
Original File Name : E:\vioscsi\w11\amd64\vioscsi.inf
Inbox : No
Class Name : SCSIAdapter
Provider Name : Red Hat, Inc.
Date : 1/8/2025
Version : 100.95.104.26200

Published Name : oem5.inf
Original File Name : E:\vioserial\w11\amd64\vioser.inf
Inbox : No
Class Name : System
Provider Name : Red Hat, Inc.
Date : 1/8/2025
Version : 100.95.104.26200

The operation completed successfully.
"@
        # Stub dism.exe + LASTEXITCODE for the duration of the call.
        function global:dism.exe { $sampleOutput; $global:LASTEXITCODE = 0 }
        try {
            $infs = _GetInjectedDriverInfs
            $infs | Should -Contain 'netkvm.inf'
            $infs | Should -Contain 'vioscsi.inf'
            $infs | Should -Contain 'vioser.inf'
        } finally {
            Remove-Item Function:\dism.exe -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Invoke-Action-StageAutopilotConfig' {
    BeforeAll {
        # $env:TEMP is not set on macOS; fall back to $env:TMPDIR or /tmp.
        if ([string]::IsNullOrEmpty($env:TEMP)) {
            $env:TEMP = if ($env:TMPDIR) { $env:TMPDIR.TrimEnd('/') } else { '/tmp' }
        }
    }

    It 'fetches /winpe/autopilot-config and writes the response bytes verbatim' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-stage-$(New-Guid)"
        try {
            $expectedBytes = [System.Text.Encoding]::UTF8.GetBytes(
                '{"CloudAssignedTenantId":"abc","Version":2049}'
            )
            $invoker = {
                param($Uri,$Headers,$TimeoutSec)
                return [pscustomobject]@{ Content = $expectedBytes }
            }
            Invoke-Action-StageAutopilotConfig `
                -Params @{ guest_path = "$tmp\AutopilotConfigurationFile.json" } `
                -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
                -WebInvoker $invoker
            $writtenBytes = [System.IO.File]::ReadAllBytes(
                "$tmp\AutopilotConfigurationFile.json"
            )
            $writtenBytes | Should -Be $expectedBytes
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'creates the directory tree if missing' {
        $base = "$env:TEMP/wpe-stage-deep-$(New-Guid)"
        $tmp = Join-Path $base (Join-Path 'a' (Join-Path 'b' 'c'))
        $destFile = Join-Path $tmp 'AutopilotConfigurationFile.json'
        try {
            $invoker = {
                param($Uri,$Headers,$TimeoutSec)
                return [pscustomobject]@{
                    Content = [System.Text.Encoding]::UTF8.GetBytes('{}')
                }
            }
            Invoke-Action-StageAutopilotConfig `
                -Params @{ guest_path = $destFile } `
                -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
                -WebInvoker $invoker
            Test-Path $destFile | Should -BeTrue
        } finally {
            Remove-Item $base -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Invoke-Action-BakeBootEntry' {
    It 'invokes bcdboot V:\Windows /s S: /f UEFI' {
        $script:lastArgs = $null
        $runner = { param($a)
            $script:lastArgs = $a
            return @{ ExitCode = 0 } }
        Invoke-Action-BakeBootEntry -Params @{} -BcdbootRunner $runner
        ($script:lastArgs -join ' ') | Should -Match 'V:\\Windows\s+/s\s+S:\s+/f\s+UEFI'
    }

    It 'throws on non-zero exit' {
        $runner = { param($a) return @{ ExitCode = 1; Stdout = 'no bootmgr' } }
        { Invoke-Action-BakeBootEntry -Params @{} -BcdbootRunner $runner } |
            Should -Throw '*bcdboot*1*'
    }
}

Describe 'Invoke-Action-StageUnattend' {
    BeforeAll {
        if ([string]::IsNullOrEmpty($env:TEMP)) {
            $env:TEMP = if ($env:TMPDIR) { $env:TMPDIR.TrimEnd('/') } else { '/tmp' }
        }
    }

    It 'fetches /winpe/unattend and writes V:\Windows\Panther\unattend.xml' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-unat-$(New-Guid)"
        try {
            $invoker = {
                param($Uri,$Headers,$TimeoutSec)
                # Invoke-WebRequest .Content is a string for text/xml
                return [pscustomobject]@{ Content = '<unattend>...</unattend>' }
            }
            Invoke-Action-StageUnattend `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' `
                -PantherDirOverride "$tmp" `
                -WebInvoker $invoker
            $body = Get-Content "$tmp\unattend.xml" -Raw
            $body | Should -Match '<unattend>'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'writes XmlDocument responses as XML, not the type name' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-unat-xml-$(New-Guid)"
        try {
            $invoker = {
                param($Uri,$Headers,$TimeoutSec)
                return [pscustomobject]@{
                    Content = [xml] '<unattend><settings pass="specialize" /></unattend>'
                }
            }
            Invoke-Action-StageUnattend `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' `
                -PantherDirOverride "$tmp" `
                -WebInvoker $invoker
            $body = Get-Content "$tmp\unattend.xml" -Raw
            $body | Should -Match '<unattend>'
            $body | Should -Match 'pass="specialize"'
            $body | Should -Not -Match 'System.Xml.XmlDocument'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Invoke-Action-PrepareWindowsSetup' {
    BeforeAll {
        if ([string]::IsNullOrEmpty($env:TEMP)) {
            $env:TEMP = if ($env:TMPDIR) { $env:TMPDIR.TrimEnd('/') } else { '/tmp' }
        }
    }

    It 'writes Panther unattend and clears offline MountedDevices' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-prepare-$(New-Guid)"
        try {
            $script:regCalls = @()
            $web = {
                param($Uri,$Headers,$TimeoutSec)
                return [pscustomobject]@{
                    Content = '<unattend><settings pass="specialize" /></unattend>'
                }
            }
            $reg = {
                param($a)
                $script:regCalls += ,$a
                return @{ ExitCode = 0; Stdout = 'ok' }
            }

            Invoke-Action-PrepareWindowsSetup `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' -PantherDirOverride "$tmp" `
                -WebInvoker $web -RegRunner $reg

            $body = Get-Content "$tmp\unattend.xml" -Raw
            $body | Should -Match 'pass="specialize"'
            $joined = ($script:regCalls | ForEach-Object { $_ -join ' ' }) -join "`n"
            $joined | Should -Match 'load HKLM\\APVEOFFLINESYSTEM'
            $joined | Should -Match 'delete HKLM\\APVEOFFLINESYSTEM\\MountedDevices /f'
            $joined | Should -Match 'unload HKLM\\APVEOFFLINESYSTEM'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe 'Invoke-Action-StageOsdClient' {
    BeforeAll {
        if ([string]::IsNullOrEmpty($env:TEMP)) {
            $env:TEMP = if ($env:TMPDIR) { $env:TMPDIR.TrimEnd('/') } else { '/tmp' }
        }
    }

    It 'writes OSD package files and osd-config.json under the applied OS' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-osd-$(New-Guid)"
        $driveCreated = $false
        try {
            if (Get-PSDrive -Name V -ErrorAction SilentlyContinue) {
                throw 'test requires unused V: PSDrive'
            }
            New-PSDrive -Name V -PSProvider FileSystem -Root $tmp.FullName | Out-Null
            $driveCreated = $true
            $setupBytes = [System.Text.Encoding]::UTF8.GetBytes('@echo off')
            $clientBytes = [System.Text.Encoding]::UTF8.GetBytes('Write-Host osd')
            $rest = {
                param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
                return [pscustomobject]@{
                    run_id = 7
                    bearer_token = 'osd-token'
                    files = @(
                        [pscustomobject]@{
                            path = 'V:\Windows\Setup\Scripts\SetupComplete.cmd'
                            content_b64 = [System.Convert]::ToBase64String($setupBytes)
                        },
                        [pscustomobject]@{
                            path = 'V:\ProgramData\ProxmoxVEAutopilot\OSD\OsdClient.ps1'
                            content_b64 = [System.Convert]::ToBase64String($clientBytes)
                        }
                    )
                }
            }

            Invoke-Action-StageOsdClient `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' -RestInvoker $rest

            (Get-Content 'V:\Windows\Setup\Scripts\SetupComplete.cmd' -Raw) |
                Should -Match '@echo off'
            (Get-Content 'V:\ProgramData\ProxmoxVEAutopilot\OSD\OsdClient.ps1' -Raw) |
                Should -Match 'osd'
            $cfg = Get-Content 'V:\ProgramData\ProxmoxVEAutopilot\OSD\osd-config.json' -Raw |
                ConvertFrom-Json
            $cfg.run_id | Should -Be 7
            $cfg.bearer_token | Should -Be 'osd-token'
            $cfg.flask_base_url | Should -Be 'http://x:5000'
        } finally {
            if ($driveCreated) {
                Remove-PSDrive -Name V -Force -ErrorAction SilentlyContinue
            }
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'rejects OSD package responses without files' {
        $rest = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            return [pscustomobject]@{
                run_id = 7
                bearer_token = 'osd-token'
            }
        }

        {
            Invoke-Action-StageOsdClient `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' -RestInvoker $rest
        } | Should -Throw '*missing nonempty files array*'
    }

    It 'rejects OSD package files without paths with response context' {
        $rest = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            return [pscustomobject]@{
                run_id = 7
                bearer_token = 'osd-token'
                files = @(
                    [pscustomobject]@{
                        content_b64 = 'AA=='
                    }
                )
            }
        }

        {
            Invoke-Action-StageOsdClient `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' -RestInvoker $rest
        } | Should -Throw '*file*missing path*response_type=System.Management.Automation.PSCustomObject*'
    }

    It 'rejects OSD package files without content' {
        $rest = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            return [pscustomobject]@{
                run_id = 7
                bearer_token = 'osd-token'
                files = @(
                    [pscustomobject]@{
                        path = 'V:\ProgramData\ProxmoxVEAutopilot\OSD\OsdClient.ps1'
                    }
                )
            }
        }

        {
            Invoke-Action-StageOsdClient `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' -RestInvoker $rest
        } | Should -Throw '*file*missing content_b64*response_type=System.Management.Automation.PSCustomObject*'
    }
}

Describe 'Invoke-Action-CaptureHash' {
    It 'invokes Get-WindowsAutopilotInfo, parses CSV, and POSTs to /winpe/hash' {
        $tmpCsv = [System.IO.Path]::GetTempFileName()
        @'
Device Serial Number,Windows Product ID,Hardware Hash
TEST-SERIAL-1,XXXXXXX,DEADBEEFCAFE
'@ | Set-Content -LiteralPath $tmpCsv -Encoding UTF8

        $captureRunner = { param($outputPath)
            Copy-Item -Force -LiteralPath $tmpCsv -Destination $outputPath
            return @{ ExitCode = 0 }
        }
        $script:posted = $null
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:posted = @{ uri = $Uri; body = $Body }
            return [pscustomobject]@{ ok = $true }
        }
        Invoke-Action-CaptureHash -Params @{} `
            -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
            -CaptureRunner $captureRunner -RestInvoker $invoker

        $script:posted.uri | Should -Match '/winpe/hash$'
        $script:posted.body | Should -Match 'TEST-SERIAL-1'
        $script:posted.body | Should -Match 'DEADBEEFCAFE'

        Remove-Item $tmpCsv -Force
    }

    It 'throws when the capture script fails' {
        $captureRunner = { param($outputPath) return @{ ExitCode = 1 } }
        { Invoke-Action-CaptureHash -Params @{} `
            -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
            -CaptureRunner $captureRunner } |
            Should -Throw '*capture*1*'
    }
}

Describe 'Start-AutopilotWinPE' {
    It 'dry-runs identity discovery without register, actions, done, or reboot' {
        $script:posted = @()
        $script:rebootCalled = $false
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:posted += "$Method $Uri"
            return [pscustomobject]@{ ok = $true }
        }
        $tmpCfg = [System.IO.Path]::GetTempFileName()
        Set-Content -LiteralPath $tmpCfg -Value '{"flask_base_url":"http://x:5000","build_sha":"DEV"}' -Encoding UTF8
        try {
            $id = Start-AutopilotWinPE `
                -ConfigPath $tmpCfg `
                -LogPath ([System.IO.Path]::GetTempFileName()) `
                -RestInvoker $invoker `
                -RebootRunner { $script:rebootCalled = $true } `
                -UuidResolver { 'fake-uuid' } `
                -MacResolver { 'aa:bb' } `
                -DryRun
            $id.vm_uuid | Should -Be 'fake-uuid'
            $id.mac | Should -Be 'aa:bb'
            $script:posted.Count | Should -Be 0
            $script:rebootCalled | Should -BeFalse
        } finally {
            Remove-Item $tmpCfg -Force -ErrorAction SilentlyContinue
        }
    }

    It 'registers, runs the action loop, calls /winpe/done, then reboots' {
        $script:posted = @()
        $script:rebootCalled = $false
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:posted += "$Method $Uri"
            if ($Uri -match '/register$') {
                return [pscustomobject]@{
                    run_id = 7
                    bearer_token = 't1'
                    actions = @(
                        @{ step_id = 1; kind = 'partition_disk'; params = @{ layout = 'recovery_before_c' } }
                    )
                }
            } elseif ($Uri -match '/step/\d+/result$') {
                return [pscustomobject]@{ ok = $true; bearer_token = 't2' }
            } elseif ($Uri -match '/done$') {
                return [pscustomobject]@{ ok = $true }
            }
        }
        $rebootRunner = { $script:rebootCalled = $true }
        $tmpCfg = [System.IO.Path]::GetTempFileName()
        Set-Content -LiteralPath $tmpCfg -Value '{"flask_base_url":"http://x:5000","build_sha":"DEV"}' -Encoding UTF8
        try {
            Start-AutopilotWinPE `
                -ConfigPath $tmpCfg `
                -LogPath ([System.IO.Path]::GetTempFileName()) `
                -RestInvoker $invoker `
                -RebootRunner $rebootRunner `
                -UuidResolver { 'fake-uuid' } `
                -MacResolver { 'aa:bb' } `
                -PartitionRunner { param($s) }
            ($script:posted -join '|') | Should -Match 'POST.*/register'
            ($script:posted -join '|') | Should -Match 'POST.*/done'
            $script:rebootCalled | Should -BeTrue
        } finally {
            Remove-Item $tmpCfg -Force -ErrorAction SilentlyContinue
        }
    }
}
