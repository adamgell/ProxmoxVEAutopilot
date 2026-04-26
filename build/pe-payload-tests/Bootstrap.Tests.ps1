BeforeAll {
    $bootstrapPath = Join-Path $PSScriptRoot '..' 'pe-payload' 'Bootstrap.ps1'
    . $bootstrapPath -DryRunForTesting
}

Describe 'Invoke-BootstrapManifest' {
    BeforeEach {
        $script:checkins = @()
        Mock Send-Checkin {
            $script:checkins += @{
                stepId = $StepId; status = $Status; errorMessage = $ErrorMessage
            }
        }
    }

    It 'dispatches each step type to the matching cmdlet and posts checkin' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'halt'
            steps = @(
                [pscustomobject]@{ id = 'l1'; type = 'log'; message = 'hello' }
            )
        }
        Mock Invoke-LogStep { return [pscustomobject]@{ LogTail = 'hello'; Extra = @{} } }

        Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u'

        $script:checkins.Count | Should -Be 2
        $script:checkins[0].stepId | Should -Be 'l1'
        $script:checkins[0].status | Should -Be 'starting'
        $script:checkins[1].status | Should -Be 'ok'
    }

    It 'on step failure with onError=halt, posts error checkin and throws to caller' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'halt'
            steps = @(
                [pscustomobject]@{ id = 'l1'; type = 'log'; message = 'will fail' }
            )
        }
        Mock Invoke-LogStep { throw 'simulated failure' }

        { Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u' } |
            Should -Throw -ExpectedMessage '*simulated failure*'

        $errorCheckin = $script:checkins | Where-Object status -eq 'error'
        $errorCheckin | Should -Not -BeNullOrEmpty
        $errorCheckin.errorMessage | Should -Match 'simulated failure'
    }

    It 'on step failure with onError=continue, logs and continues' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'continue'
            steps = @(
                [pscustomobject]@{ id = 'a'; type = 'log'; message = 'first' },
                [pscustomobject]@{ id = 'b'; type = 'log'; message = 'second' }
            )
        }
        $script:logCalls = 0
        Mock Invoke-LogStep {
            $script:logCalls++
            if ($script:logCalls -eq 1) { throw 'first fails' }
            return [pscustomobject]@{ LogTail = 'second'; Extra = @{} }
        }

        { Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u' } |
            Should -Not -Throw
        $statuses = $script:checkins | Sort-Object stepId | ForEach-Object { "$($_.stepId)=$($_.status)" }
        $statuses | Should -Contain 'a=error'
        $statuses | Should -Contain 'b=ok'
    }

    It 'unknown step type fails with onError=halt' {
        $manifest = [pscustomobject]@{
            version = 1; vmUuid = 'u'; onError = 'halt'
            steps = @( [pscustomobject]@{ id = 'x'; type = 'mystery-step' } )
        }
        { Invoke-BootstrapManifest -Manifest $manifest -OrchestratorUrl 'http://o:5000' -VmUuid 'u' } |
            Should -Throw -ExpectedMessage '*unknown step type*'
    }
}
