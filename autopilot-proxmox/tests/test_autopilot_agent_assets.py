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


def test_wix_installer_creates_delayed_auto_localsystem_service():
    wxs = _read("autopilot-agent/installer/AutopilotAgent.wxs")

    assert 'Name="AutopilotAgent"' in wxs
    assert 'DisplayName="AutopilotAgent"' in wxs
    assert 'Account="LocalSystem"' in wxs
    assert 'Start="auto"' in wxs
    assert 'Name="DelayedAutoStart"' in wxs
    assert 'Value="1"' in wxs
    assert "MajorUpgrade" in wxs


def test_signing_scripts_use_artifact_signing_without_storing_credentials():
    build_script = _read("autopilot-agent/scripts/Build-AutopilotAgent.ps1")
    env_script = _read("autopilot-agent/scripts/AutopilotAgent.Signing.env.ps1")
    sign_script = _read("autopilot-agent/scripts/Sign-AutopilotAgent.ps1")

    assert "Remove-Item -Recurse -Force" in build_script
    assert "az account show" in env_script
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
    assert "AutopilotAgent" in post
    assert "guest-network-get-interfaces" in post
    assert "Invoke-CimMethod" in post
    assert "Restart-Service -Name QEMU-GA" in post


def test_ninja_preinstall_validates_admin_arch_and_reachability():
    pre = _read("autopilot-proxmox/files/ninja/autopilotagent-preinstall.ps1")

    assert "WindowsPrincipal" in pre
    assert "OSArchitecture" in pre
    assert "Invoke-WebRequest" in pre
    assert "AutopilotAgent" in pre
