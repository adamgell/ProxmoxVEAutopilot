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
            -RunOsdClient { $script:Calls += 'osd-client' } `
            -RemoveScheduledTask { param($Name) $script:Calls += "cleanup:$Name" } `
            -EndBootstrapSession { param($Name) $script:Calls += "end-session:$Name" } `
            -ReportEvent { param($ServerUrl,$RunId,$BearerToken,$Phase,$EventType,$Message,$Severity,$Data) }

        ($script:Calls -join '|') |
            Should -Be 'network|server:https://autopilot.local|msi:C:\Stage\AutopilotAgent.msi|postinstall:C:\Stage\autopilotagent-postinstall.ps1:cloudosd|heartbeat|osd-client|cleanup:PVEAutopilot-CloudOSD-FirstBoot|end-session:PVEAutopilot'
    }

    It 'posts SetupComplete and first-boot milestone events to the controller' {
        $source = Get-Content -LiteralPath $script:FirstBootPath -Raw

        $source | Should -Match 'Write-PVEAutopilotCloudOSDEvent'
        $source | Should -Match "EventType 'setupcomplete_task_started'"
        $source | Should -Match "EventType 'firstboot_complete'"
        $source | Should -Match '/api/cloudosd/runs/.*/events'
    }

    It 'redacts domain join passwords from Panther unattend files during first boot' {
        $panther = Join-Path $TestDrive 'Panther'
        New-Item -ItemType Directory -Path $panther -Force | Out-Null
        $unattend = Join-Path $panther 'Unattend.xml'
        @'
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-UnattendedJoin" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <Identification>
        <Credentials>
          <Domain>HOME</Domain>
          <Username>svc-cloudjoin</Username>
          <Password>join-secret</Password>
        </Credentials>
        <JoinDomain>home.gell.one</JoinDomain>
      </Identification>
    </component>
  </settings>
</unattend>
'@ | Set-Content -LiteralPath $unattend -Encoding UTF8

        Clear-PVEAutopilotDomainJoinSecrets -PantherRoot $panther

        $content = Get-Content -LiteralPath $unattend -Raw
        $content | Should -Not -Match 'join-secret'
        $content | Should -Match '<Password>REDACTED-BY-PVEAUTOPILOT</Password>'
        $content | Should -Match '<JoinDomain>home.gell.one</JoinDomain>'
    }

    It 'redacts CloudOSD OOBE bootstrap account passwords from Panther unattend files' {
        $panther = Join-Path $TestDrive 'PantherOobe'
        New-Item -ItemType Directory -Path $panther -Force | Out-Null
        $unattend = Join-Path $panther 'Unattend.xml'
        @'
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount>
            <Name>PVEAutopilot</Name>
            <Password>
              <Value>oobe-secret</Value>
              <PlainText>true</PlainText>
            </Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon>
        <Username>PVEAutopilot</Username>
        <Password>
          <Value>autologon-secret</Value>
          <PlainText>true</PlainText>
        </Password>
      </AutoLogon>
    </component>
  </settings>
</unattend>
'@ | Set-Content -LiteralPath $unattend -Encoding UTF8

        $redacted = Clear-PVEAutopilotDomainJoinSecrets -PantherRoot $panther

        $content = Get-Content -LiteralPath $unattend -Raw
        $redacted | Should -Be 2
        $content | Should -Not -Match 'oobe-secret'
        $content | Should -Not -Match 'autologon-secret'
        ([regex]::Matches($content, '<Value>REDACTED-BY-PVEAUTOPILOT</Value>')).Count |
            Should -Be 2
    }

    It 'clears OOBE bootstrap AutoLogon state and disables the temporary account' {
        $script:CleanupCalls = @()

        Clear-PVEAutopilotOobeBootstrapAccount `
            -RegistryCleanup { $script:CleanupCalls += 'registry' } `
            -DisableUser { param($Name) $script:CleanupCalls += "disable:$Name" }

        ($script:CleanupCalls -join '|') |
            Should -Be 'registry|disable:PVEAutopilot'
    }

    It 'logs off the temporary OOBE bootstrap desktop session' {
        $script:LogoffCalls = @()
        $queryUserOutput = @'
 USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
>PVEAutopilot         console             1  Active      none   5/13/2026 2:11 PM
 adam                 rdp-tcp#1           2  Active      none   5/13/2026 2:12 PM
'@

        $loggedOff = Invoke-PVEAutopilotBootstrapSessionLogoff `
            -UserName 'PVEAutopilot' `
            -QueryUser { $queryUserOutput -split "`n" } `
            -LogoffSession { param([int] $SessionId) $script:LogoffCalls += "logoff:$SessionId" }

        $loggedOff | Should -Be 1
        ($script:LogoffCalls -join '|') |
            Should -Be 'logoff:1'
    }

    It 'skips non-unattend Panther XML files during domain join cleanup' {
        $panther = Join-Path $TestDrive 'Panther'
        $unattendGc = Join-Path $panther 'UnattendGC'
        New-Item -ItemType Directory -Path $unattendGc -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $unattendGc 'diagerr.xml') `
            -Value '<diag><unclosed>' `
            -Encoding UTF8
        $unattend = Join-Path $panther 'Unattend.xml'
        @'
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-UnattendedJoin" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <Identification>
        <Credentials>
          <Domain>HOME</Domain>
          <Username>svc-cloudjoin</Username>
          <Password>join-secret</Password>
        </Credentials>
        <JoinDomain>home.gell.one</JoinDomain>
      </Identification>
    </component>
  </settings>
</unattend>
'@ | Set-Content -LiteralPath $unattend -Encoding UTF8

        { Clear-PVEAutopilotDomainJoinSecrets -PantherRoot $panther } |
            Should -Not -Throw
        Get-Content -LiteralPath $unattend -Raw |
            Should -Match '<Password>REDACTED-BY-PVEAUTOPILOT</Password>'
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
