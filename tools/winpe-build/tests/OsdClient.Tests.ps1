Describe 'OsdClient.ps1 contract' {
    BeforeAll {
        $script:ClientPath = (Resolve-Path "$PSScriptRoot/../../../autopilot-proxmox/files/osd-client/OsdClient.ps1").Path
        $script:Body = Get-Content -LiteralPath $script:ClientPath -Raw
    }

    It 'logs to SetupComplete and ProgramData OSD logs' {
        $script:Body | Should -Match 'Setup\\Scripts\\SetupComplete\.log'
        $script:Body | Should -Match 'osd-client\.log'
        $script:Body | Should -Match 'Add-OsdLogLine -Path \$ClientLog -Line \$line'
        $script:Body | Should -Match 'Add-OsdLogLine -Path \$SetupLog -Line \$line -BestEffort'
    }

    It 'treats SetupComplete log write contention as non-fatal' {
        $script:Body | Should -Match 'function Add-OsdLogLine'
        $script:Body | Should -Match '\[switch\] \$BestEffort'
        $script:Body | Should -Match 'if \(-not \$BestEffort\)'
        $script:Body | Should -Not -Match 'setup log write skipped'
    }

    It 'registers with the OSD endpoint and marks completion' {
        $script:Body | Should -Match '/osd/client/register'
        $script:Body | Should -Match '/osd/client/complete'
        $script:Body | Should -Match 'computer_name = \$env:COMPUTERNAME'
    }

    It 'reports OSD step running, ok, and error states' {
        $script:Body | Should -Match 'Send-StepState'
        $script:Body | Should -Match '-State running'
        $script:Body | Should -Match '-State ok'
        $script:Body | Should -Match '-State error'
        $script:Body | Should -Match '/osd/client/step/\$StepId/result'
    }

    It 'retries transient orchestrator requests before failing' {
        $script:Body | Should -Match '\[int\] \$MaxAttempts = 8'
        $script:Body | Should -Match 'for \(\$i = 1; \$i -le \$MaxAttempts; \$i\+\+\)'
        $script:Body | Should -Match 'Start-Sleep -Seconds'
        $script:Body | Should -Match 'throw \$lastErr'
    }

    It 'runs QGA and recovery fix as explicit OSD steps' {
        $script:Body | Should -Match "'install_qga' \{ Invoke-InstallQga \}"
        $script:Body | Should -Match "'fix_recovery_partition' \{ Invoke-RecoveryFix \}"
        $script:Body | Should -Match 'qemu-ga-x86_64\.msi'
        $script:Body | Should -Match 'Get-Service -Name QEMU-GA'
        $script:Body | Should -Match 'WaitForStatus'
        $script:Body | Should -Match 'FixRecoveryPartition\.ps1'
    }
}
