import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_autopilot_agent_project_declares_worker_service_contract():
    csproj = _read("autopilot-agent/src/AutopilotAgent/AutopilotAgent.csproj")
    program = _read("autopilot-agent/src/AutopilotAgent/Program.cs")

    assert "<TargetFramework>net8.0</TargetFramework>" in csproj
    assert "Microsoft.Extensions.Hosting.WindowsServices" in csproj
    assert "UseWindowsService" in program
    assert 'ServiceName = "AutopilotAgent"' in program


def test_autopilot_agent_uses_programdata_config_and_logs():
    config = _read("autopilot-agent/src/AutopilotAgent/AgentConfig.cs")

    assert r"ProxmoxVEAutopilot\AutopilotAgent\agent.json" in config
    assert r"ProxmoxVEAutopilot\AutopilotAgent\logs" in config
    assert "/api/agent/v1/bootstrap" in _read(
        "autopilot-agent/src/AutopilotAgent/AgentApiClient.cs"
    )
    assert "/api/agent/v1/heartbeat" in _read(
        "autopilot-agent/src/AutopilotAgent/AgentApiClient.cs"
    )


def test_autopilot_agent_domain_telemetry_uses_computer_system_membership():
    telemetry = _read("autopilot-agent/src/AutopilotAgent/TelemetryCollector.cs")

    assert "Win32_ComputerSystem" in telemetry
    assert "PartOfDomain" in telemetry
    assert "ReadDomainState" in telemetry
    assert "IPGlobalProperties.GetIPGlobalProperties().DomainName" in telemetry


def test_autopilot_agent_claims_hash_capture_work_items():
    client = _read("autopilot-agent/src/AutopilotAgent/AgentApiClient.cs")
    worker = _read("autopilot-agent/src/AutopilotAgent/Worker.cs")
    capture = _read("autopilot-agent/src/AutopilotAgent/HashCaptureService.cs")
    program = _read("autopilot-agent/src/AutopilotAgent/Program.cs")

    assert "/api/agent/v1/work/next" in client
    assert "/api/agent/v1/hash-script" in client
    assert "/api/agent/v1/hash" in client
    assert "/api/agent/v1/work/{workItemId}/{action}" in client
    assert "capture_autopilot_hash" in worker
    assert "HashCaptureService" in program
    assert "Get-WindowsAutopilotInfo.ps1" in capture
    assert "-GroupTag" in capture
    assert "TextFieldParser" in capture


def test_hash_upload_playbook_preserves_selected_file_contract():
    playbook = _read("autopilot-proxmox/playbooks/upload_hashes.yml")

    assert "Check selected hash file" in playbook
    assert "Fail if selected hash file is missing" in playbook
    assert "upload_hash_output_dir:" in playbook
    assert "HASH_DIR: \"{{ upload_hash_output_dir }}\"" in playbook
    assert "hash_file is defined" in playbook
    assert "HASH_FILE: \"{{ hash_file | default('') }}\"" in playbook
    assert "GROUP_TAG: \"{{ vm_group_tag | default('') }}\"" in playbook
    assert "Upload hashes to Autopilot" in playbook


def test_hash_upload_script_uses_selected_file_and_cleans_temp_tagged_copy():
    script = _read("autopilot-proxmox/scripts/upload_hashes.ps1")

    assert "$hashFile  = $env:HASH_FILE" in script
    assert "$groupTag  = $env:GROUP_TAG" in script
    assert "Get-Item -LiteralPath $hashFile" in script
    assert "Applying group tag override:" in script
    assert script.count("function New-TaggedCsvCopy") == 1
    assert "try {" in script
    assert "finally {" in script
    assert "Import-AutopilotCSV -csvFile $uploadPath" in script
    assert "Disconnect-MgGraph | Out-Null" in script
    assert "Remove-Item -LiteralPath $uploadPath -Force" in script


def test_wix_installer_creates_delayed_auto_localsystem_service():
    wxs = _read("autopilot-agent/installer/AutopilotAgent.wxs")

    assert 'Name="AutopilotAgent"' in wxs
    assert 'DisplayName="AutopilotAgent"' in wxs
    assert 'Account="LocalSystem"' in wxs
    assert 'Start="auto"' in wxs
    assert 'Name="DelayedAutoStart"' in wxs
    assert 'Value="1"' in wxs
    assert 'Id="ProgramFiles6432Folder"' in wxs
    assert "MajorUpgrade" in wxs


def test_autopilot_agent_wix_installs_windows_service():
    wxs = _read("autopilot-agent/installer/AutopilotAgent.wxs")

    assert 'Name="AutopilotAgent"' in wxs
    assert "<ServiceInstall" in wxs
    assert "<ServiceControl" in wxs
    assert 'Start="auto"' in wxs
    assert 'Account="LocalSystem"' in wxs


def test_signing_scripts_use_artifact_signing_without_storing_credentials():
    build_script = _read("autopilot-agent/scripts/Build-AutopilotAgent.ps1")
    env_script = _read("autopilot-agent/scripts/AutopilotAgent.Signing.env.ps1")
    sign_script = _read("autopilot-agent/scripts/Sign-AutopilotAgent.ps1")

    assert "Remove-Item -Recurse -Force" in build_script
    assert "InstallerPlatform" in build_script
    assert "Ensure-NuGetOrgSource" in build_script
    assert "https://api.nuget.org/v3/index.json" in build_script
    assert "dotnet nuget add source $sourceUrl --name nuget.org" in build_script
    assert '"win-arm64" { "arm64" }' in build_script
    assert "az account show" in env_script
    assert "az account get-access-token" in env_script
    assert "$LASTEXITCODE" in env_script
    assert "https://codesigning.azure.net/.default" in env_script
    assert "dotnet --list-runtimes" in env_script
    assert "Where-Object" in env_script
    assert "metadata.json" in env_script
    assert "UTF8Encoding" in env_script
    assert "CodeSigningAccountName" in env_script
    assert "CertificateProfileName" in env_script
    assert "AZURE_CLIENT_SECRET" not in env_script
    assert "DotNetRootForSigning" in sign_script
    assert "/dlib" in sign_script
    assert "/dmdf" in sign_script
    assert "/fd SHA256" in sign_script
    assert "/td SHA256" in sign_script
    assert "http://timestamp.acs.microsoft.com/" in sign_script
    assert "signtool.exe verify" in sign_script


def test_ninja_postinstall_unblocks_qga_after_agent_health():
    post = _read("autopilot-proxmox/files/ninja/autopilotagent-postinstall.ps1")

    assert "/api/agent/v1/bootstrap" in post
    assert "ApprovalTimeoutSeconds" in post
    assert "approval_id" in post
    assert "poll_url" in post
    assert "$pollUrl = [string]$bootstrap.poll_url" in post
    assert '$claimUrl = $ServerUrl.TrimEnd("/") + $pollUrl' in post
    assert "Approval poll failed:" in post
    assert "Get-Sha256Hex" in post
    assert "Get-TokenDiagnostic" in post
    assert "BootstrapToken parameter diagnostic:" in post
    assert "BootstrapToken env diagnostic:" in post
    assert "BootstrapToken selected from env:" in post
    assert "BootstrapToken received. Length=" in post
    assert "ProofPrefix=" in post
    assert "Sha256OfProofPrefix=" in post
    assert "$BootstrapToken = [string]$env:BootstrapToken" in post
    assert "Set the Ninja script variable named BootstrapToken" in post
    assert '$BootstrapToken.Trim() -eq "BootstrapToken"' in post
    assert 'remove the literal -BootstrapToken `"BootstrapToken`" parameter' in post
    assert "$BootstrapToken = $BootstrapToken.Trim()" in post
    assert "AutopilotAgent" in post
    assert "guest-network-get-interfaces" in post
    assert "Invoke-CimMethod" in post
    assert "Restart-Service -Name QEMU-GA" in post
    assert "QGA RPC block cleanup failed after AutopilotAgent heartbeat; continuing:" in post
    assert "AutopilotAgent postinstall complete." in post


def test_ninja_update_script_takes_no_arguments_and_installs_unconditionally():
    """The push-update path deployed through NinjaOne.

    Exists because the agent's own upgrade loop never fires on part of the
    fleet: those agents heartbeat, get a 200 from update-check, and log
    neither a completed install nor a failed one, which means they are being
    told they are current while the fleet page shows "Upgrade available".

    Three properties this must keep. It takes no arguments, so a Ninja script
    action can run it with nothing configured. It installs unconditionally,
    because gating on the server's verdict would skip exactly the machines it
    exists for. And it records the installed-vs-published disagreement, which
    is the evidence needed to fix update-check rather than paper over it.
    """
    update = _read("autopilot-proxmox/files/ninja/autopilotagent-update.ps1")

    # Zero-argument: no script-level param block (the ones present belong to
    # internal functions, which are indented) and no [CmdletBinding()].
    assert not re.search(r"^param\s*\(", update, re.M)
    assert "[CmdletBinding()]" not in update
    assert "$FallbackServerUrl" in update
    assert "Resolve-ServerUrl" in update
    assert "$Identity.ServerUrl" in update

    # Reads the agent's own identity so the version query is authenticated.
    assert "agent.json" in update
    assert "/api/agent/v1/update-check" in update
    assert "Bearer $($Identity.AgentToken)" in update
    assert "win-arm64" in update

    # Both versions, because they can disagree and that disagreement is the bug.
    assert "AssemblyVersion" in update
    assert "FileVersion" in update
    assert "MISMATCH:" in update
    assert "This is why self-upgrade never fires on this machine." in update

    # Verifies before installing, and refuses on mismatch.
    assert "/api/cloudosd/assets/autopilotagent.msi" in update
    assert "Get-FileHash" in update
    assert "failed SHA-256 validation" in update
    assert "installing unverified" in update

    # Installs unconditionally: no status gate around msiexec.
    assert '"/i", $dst, "/qn", "/norestart"' in update
    assert "$proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010" in update
    assert "Start-Service -Name AutopilotAgent" in update
    assert 'status -ne "upgrade_available"' not in update.split("Running msiexec")[1]

    # Admin-gated like the sibling ninja scripts.
    assert "requires administrative context." in update
    assert "Version did not change." in update


def test_cloudosd_firstboot_recovers_postinstall_failure_when_agent_heartbeat_visible():
    firstboot = _read("autopilot-proxmox/tools/cloudosd-build/PVEAutopilot-FirstBoot.ps1")

    assert "AutopilotAgent postinstall reported failure:" in firstboot
    assert "firstboot_postinstall_recovered" in firstboot
    assert "AutopilotAgent heartbeat was visible after postinstall failure; continuing" in firstboot
    assert "AutopilotAgent heartbeat recovery after postinstall failure failed:" in firstboot
    assert "firstboot_postinstall_retry_scheduled" in firstboot
    assert "postinstall_failure_count" in firstboot
    assert "postinstall_retryable_failure_limit" in firstboot
    assert "[int] $PostinstallRetryableFailures = 12" in firstboot
    assert "[int] $PostinstallRetryWindowMinutes = 45" in firstboot
    assert "CloudOSD first boot postinstall failure is retryable from outer catch" in firstboot
    assert "throw $postinstallError" in firstboot
    assert "Data @{ recovered = $postinstallRecovered }" in firstboot


def test_cloudosd_firstboot_serializes_scheduled_task_overlap_and_reports_diagnostics():
    firstboot = _read("autopilot-proxmox/tools/cloudosd-build/PVEAutopilot-FirstBoot.ps1")

    assert "Invoke-PVEAutopilotFirstBootWithMutex" in firstboot
    assert "Global\\PVEAutopilotCloudOSDFirstBoot" in firstboot
    assert "firstboot_overlap_skipped" in firstboot
    assert "Get-CloudOSDFirstBootDiagnosticData" in firstboot
    assert "postinstall_log_tail" in firstboot
    assert "firstboot_log_tail" in firstboot
    assert "-Data (Get-CloudOSDFirstBootDiagnosticData" in firstboot


def test_cloudosd_build_manifest_records_component_content_hashes():
    build = _read("autopilot-proxmox/tools/cloudosd-build/build-cloudosd.ps1")

    assert "Get-CloudOSDComponentHashes" in build
    assert "(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()" in build
    assert "component_sha256 = $componentHashes" in build
    assert '$inputs += "${file}:$($componentHashes[$file])"' in build


def test_ninja_preinstall_validates_admin_arch_and_reachability():
    pre = _read("autopilot-proxmox/files/ninja/autopilotagent-preinstall.ps1")

    assert "WindowsPrincipal" in pre
    assert "OSArchitecture" in pre
    assert "Invoke-WebRequest" in pre
    assert "AutopilotAgent" in pre
