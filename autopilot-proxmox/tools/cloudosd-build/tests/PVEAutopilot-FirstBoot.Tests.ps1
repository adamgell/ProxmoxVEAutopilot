BeforeAll {
    $env:CLOUDOSD_FIRSTBOOT_LIBRARY_ONLY = '1'
    $script:FirstBootPath = (Resolve-Path "$PSScriptRoot/../PVEAutopilot-FirstBoot.ps1").Path
    . $script:FirstBootPath
}

AfterAll {
    Remove-Item Env:\CLOUDOSD_FIRSTBOOT_LIBRARY_ONLY -ErrorAction SilentlyContinue
}

Describe 'Invoke-PVEAutopilotFirstBoot' {
    BeforeEach {
        $env:ProgramData = Join-Path $TestDrive 'ProgramData'
        New-Item -ItemType Directory -Path $env:ProgramData -Force | Out-Null
    }

    It 'installs the AutopilotAgent MSI before running postinstall' {
        $script:Calls = @()
        $runConfig = [pscustomobject]@{
            run_id = 'run-1'
            vmid = 221
            server_base_url = 'https://autopilot.local'
            agent = [pscustomobject]@{
                bootstrap_token = 'bootstrap-token'
            }
            payloads = [pscustomobject]@{
                autopilotagent_msi = [pscustomobject]@{
                    local_path = 'C:\Stage\AutopilotAgent.msi'
                }
                autopilotagent_postinstall = [pscustomobject]@{
                    local_path = 'C:\Stage\autopilotagent-postinstall.ps1'
                }
            }
        }
        $agentRoot = Join-Path $env:ProgramData 'ProxmoxVEAutopilot\AutopilotAgent'
        New-Item -ItemType Directory -Path $agentRoot -Force | Out-Null
        @{ agentToken = 'agent-token' } |
            ConvertTo-Json |
            Set-Content -LiteralPath (Join-Path $agentRoot 'agent.json') -Encoding UTF8

        Invoke-PVEAutopilotFirstBoot -RunConfig $runConfig `
            -WaitForNetwork { $script:Calls += 'network' } `
            -WaitForServer { param($Url) $script:Calls += "server:$Url" } `
            -InstallMsi { param($Path) $script:Calls += "msi:$Path" } `
            -RunPostinstall { param($ScriptPath,$PostinstallArgs) $script:Calls += "postinstall:$($ScriptPath):$($PostinstallArgs.Phase)" } `
            -ConfirmHeartbeat { param($ConfigUrl,$Token) $script:Calls += 'heartbeat' } `
            -RemoveScheduledTask { param($Name) $script:Calls += "cleanup:$Name" }

        ($script:Calls -join '|') |
            Should -Be 'network|server:https://autopilot.local|msi:C:\Stage\AutopilotAgent.msi|postinstall:C:\Stage\autopilotagent-postinstall.ps1:cloudosd|heartbeat|cleanup:PVEAutopilot-CloudOSD-FirstBoot'
    }

    It 'passes postinstall arguments through the real helper without using the automatic args variable' -Skip:$IsWindows {
        $fakeBin = Join-Path $TestDrive 'bin'
        New-Item -ItemType Directory -Path $fakeBin -Force | Out-Null
        $capturePath = Join-Path $TestDrive 'powershell-args.txt'
        $shimPath = Join-Path $fakeBin 'powershell.exe'
        @'
#!/bin/sh
printf '%s\n' "$@" > "$PVEA_FAKE_POWERSHELL_ARGS"
exit 0
'@ | Set-Content -LiteralPath $shimPath -Encoding UTF8
        chmod +x $shimPath
        $oldPath = $env:PATH
        $env:PATH = "$fakeBin$([IO.Path]::PathSeparator)$oldPath"
        $env:PVEA_FAKE_POWERSHELL_ARGS = $capturePath
        try {
            $postinstall = Join-Path $TestDrive 'autopilotagent-postinstall.ps1'
            Set-Content -LiteralPath $postinstall -Value '# test' -Encoding UTF8
            Invoke-AutopilotAgentPostinstall -ScriptPath $postinstall -PostinstallArgs @{
                ServerUrl = 'https://autopilot.local'
                BootstrapToken = 'bootstrap-token'
                RunId = 'run-1'
                Vmid = 221
                Phase = 'cloudosd'
            }
            $argsText = Get-Content -LiteralPath $capturePath -Raw
            $argsText | Should -Match '-ServerUrl'
            $argsText | Should -Match 'https://autopilot.local'
            $argsText | Should -Match '-BootstrapToken'
            $argsText | Should -Match 'bootstrap-token'
            $argsText | Should -Match '-RunId'
            $argsText | Should -Match 'run-1'
            $argsText | Should -Match '-Vmid'
            $argsText | Should -Match '221'
            $argsText | Should -Match '-Phase'
            $argsText | Should -Match 'cloudosd'
        }
        finally {
            $env:PATH = $oldPath
            Remove-Item Env:\PVEA_FAKE_POWERSHELL_ARGS -ErrorAction SilentlyContinue
        }
    }
}
