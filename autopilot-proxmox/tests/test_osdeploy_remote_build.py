from __future__ import annotations

import json
import shutil
import subprocess

import pytest


def test_osdeploy_manifest_parser_accepts_windows_paths():
    from scripts import osdeploy_remote_build as remote_build

    lines = [
        "noise",
        r"F:\BuildRoot\outputs\osdeploy-server-amd64-1234abcd.json",
        r"F:\BuildRoot\outputs\osdeploy-server-amd64-1234abcd.wim",
        r"F:\BuildRoot\outputs\osdeploy-server-amd64-1234abcd.iso",
    ]

    assert remote_build.parse_manifest_path(lines) == (
        r"F:\BuildRoot\outputs\osdeploy-server-amd64-1234abcd.json"
    )


def test_osdeploy_remote_command_uses_server_builder_and_pinned_modules():
    from scripts import osdeploy_remote_build as remote_build

    script = remote_build.build_remote_script(
        job_id="job-1",
        remote_root=r"F:\BuildRoot",
        archive_name="osdeploy-src-job-1.tar.gz",
        arch="amd64",
        osdeploy_version="26.1.30.5",
        osdbuilder_version="24.10.8.1",
        adk_version="10.1.26100.1",
        controller_url="http://192.168.2.142:5000",
    )
    command = remote_build.build_remote_command(script)

    assert r"F:\BuildRoot\work\osdeploy-src-job-1" in script
    assert r"tools\osdeploy-build\build-osdeploy.ps1" in script
    assert "-OSDeployVersion '26.1.30.5'" in script
    assert "-OSDBuilderVersion '24.10.8.1'" in script
    assert "-ADKVersion '10.1.26100.1'" in script
    assert "-ControllerUrl 'http://192.168.2.142:5000'" in script
    assert "-Arch amd64" in script
    assert "-EncodedCommand" in command


def test_osdeploy_remote_command_carries_server_image_inputs():
    from scripts import osdeploy_remote_build as remote_build

    script = remote_build.build_remote_script(
        job_id="job-1",
        remote_root=r"F:\BuildRoot",
        archive_name="osdeploy-src-job-1.tar.gz",
        arch="amd64",
        osdeploy_version="26.1.30.5",
        osdbuilder_version="24.10.8.1",
        adk_version="10.1.26100.1",
        source_media_path=r"E:\ISOs\SERVER_EVAL_x64FRE_en-us.iso",
        image_name="Windows Server 2025 Datacenter Evaluation (Desktop Experience)",
        image_index=4,
        os_version="Windows Server 2025",
        os_edition="Datacenter",
        os_language="en-us",
    )

    assert "-SourceMediaPath 'E:\\ISOs\\SERVER_EVAL_x64FRE_en-us.iso'" in script
    assert "-ImageName 'Windows Server 2025 Datacenter Evaluation (Desktop Experience)'" in script
    assert "-ImageIndex 4" in script
    assert "-OSVersion 'Windows Server 2025'" in script
    assert "-OSEdition 'Datacenter'" in script
    assert "-OSLanguage 'en-us'" in script


def test_osdeploy_publish_job_uploads_iso_to_proxmox_storage(tmp_path, monkeypatch):
    from scripts import osdeploy_publish_job as publish_job
    from web import app as web_app

    iso = tmp_path / "osdeploy-server.iso"
    iso.write_bytes(b"iso")
    calls = []

    def fake_upload(path, file_path, *, data=None, field_name=None, content_type=None):
        calls.append({
            "path": path,
            "content": data["content"],
            "file_name": file_path.name,
            "field_name": field_name,
            "content_type": content_type,
        })

    monkeypatch.setattr(web_app, "_proxmox_upload_file", fake_upload)

    volid = publish_job.upload_iso_to_proxmox(
        iso_path=iso,
        node="pve",
        storage="local",
        iso_name=iso.name,
    )

    assert volid == "local:iso/osdeploy-server.iso"
    assert calls == [{
        "path": "/nodes/pve/storage/local/upload",
        "content": "iso",
        "file_name": "osdeploy-server.iso",
        "field_name": "filename",
        "content_type": "application/x-iso9660-image",
    }]


def test_osdeploy_publish_job_main_emits_json(monkeypatch, capsys):
    from scripts import osdeploy_publish_job as publish_job

    monkeypatch.setattr(
        publish_job,
        "publish_artifact",
        lambda **kwargs: {
            "ok": True,
            "artifact": {"id": kwargs["artifact_id"]},
            "target_volid": "local:iso/osdeploy-server.iso",
        },
    )

    code = publish_job.main([
        "publish",
        "--artifact-id",
        "artifact-1",
        "--node",
        "pve",
        "--storage",
        "local",
        "--job-id",
        "job-1",
    ])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["target_volid"] == (
        "local:iso/osdeploy-server.iso"
    )


def test_osdeploy_record_artifact_refuses_manifest_without_local_media(tmp_path, monkeypatch):
    from scripts import osdeploy_remote_build as remote_build
    from web import db_pg

    manifest = tmp_path / "osdeploy-server-amd64-test.json"
    manifest.write_text(json.dumps({
        "architecture": "amd64",
        "osdeploy_module_version": "26.1.30.5",
        "osdbuilder_module_version": "24.10.8.1",
        "adk_version": "10.1.26100.1",
        "build_sha": "abcdef",
        "output_iso": r"F:\BuildRoot\outputs\osdeploy-server-amd64-test.iso",
        "output_wim": r"F:\BuildRoot\outputs\osdeploy-server-amd64-test.wim",
        "iso_sha256": "a" * 64,
        "wim_sha256": "b" * 64,
        "source_media": "Windows Server 2025",
        "image_name": "Windows Server 2025 Datacenter",
        "image_index": 1,
        "built_by_host": "builder-01",
    }))

    def fail_connection():
        raise AssertionError("record_artifact touched PostgreSQL before validating media")

    monkeypatch.setattr(db_pg, "connection", fail_connection)

    with pytest.raises(FileNotFoundError, match="OSDeploy build output missing"):
        remote_build.record_artifact(manifest, tmp_path, build_job_id="job-1")


def test_osdeploy_tool_copy_stays_in_sync():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    repo_root = app_root.parent
    filenames = [
        "build-osdeploy.ps1",
        "Invoke-OSDeployBridge.ps1",
        "config.json",
        "startnet.cmd",
    ]

    for filename in filenames:
        assert (
            app_root / "tools" / "osdeploy-build" / filename
        ).read_bytes() == (
            repo_root / "tools" / "osdeploy-build" / filename
        ).read_bytes()


def test_osdeploy_build_script_has_native_osdbuilder_server_base_path():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )

    assert "Import-OSMedia" in text
    assert '@("OSMedia", "OSImport")' in text
    assert "Invoke-NativeServerMediaBuild" in text
    assert "dism.exe /Export-Image" in text
    assert "-InstallWimPath $WimPath" in text
    assert 'build_engine = $buildEngine' in text
    assert "'E:\\BuildRoot\\inputs\\virtio-win'" in text
    assert "[System.IO.FileAttributes]::ReadOnly" in text
    assert "Test-DismPackageNotApplicable" in text
    assert "was not applicable for boot.wim" in text
    assert "Reset-OSDeployBuilderState -Root $OSDBuilderPath" in text
    assert "dism.exe /Cleanup-Wim" in text
    assert "[string]$ControllerUrl = $env:AUTOPILOT_BASE_URL" in text
    assert "Resolve-OSDeployControllerUrl" in text
    assert "OSDeploy ControllerUrl is required" in text
    assert "$config.flask_base_url = Resolve-OSDeployControllerUrl -Value $ControllerUrl" in text
    assert "$config.fallback_base_url = \"\"" in text
    assert "controller_url = (Resolve-OSDeployControllerUrl -Value $ControllerUrl)" in text
    assert "$_.Name -like 'Windows Server *' -or $_.Name -like 'build*'" in text
    assert "$previousErrorActionPreference = $ErrorActionPreference" in text
    assert "$ErrorActionPreference = 'Continue'" in text
    assert "VersionedModules" in text
    assert "Where-Object { $_.Version.ToString() -eq $ExpectedVersion }" in text
    assert "$moduleSpec = @{ ModuleName = $Name; RequiredVersion = $ExpectedVersion }" in text
    assert "Import-Module -FullyQualifiedName $moduleSpec -Force" in text
    assert '$importParams["ImageName"]' not in text
    assert "Update-OSMedia" in text
    assert "New-OSBuild" in text
    assert "-CreateISO" in text
    assert "OSDEPLOY_SERVER_BUILD_COMMAND must be set" not in text
    assert "ServerDatacenter" in text


def test_osdeploy_build_script_accepts_mounted_source_media_root():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )

    assert "Resolve-OSDeploySourceMedia" in text
    assert "Test-Path -LiteralPath $SourceMediaPath -PathType Container" in text
    assert "Join-Path $SourceRoot 'sources\\install.wim'" in text
    assert "Join-Path $SourceRoot 'sources\\install.esd'" in text
    assert '$importParams["Path"] = $sourceMedia.SourceRoot' in text
    assert "$sourceMediaType = $sourceMedia.SourceType" in text
    assert "$sourceInstallImage = $sourceMedia.InstallImagePath" in text
    assert "source_media_type = $sourceMediaType" in text
    assert "source_install_image = $sourceInstallImage" in text


def test_osdeploy_build_script_bakes_pe_bridge_assets():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )
    startnet = (app_root / "tools" / "osdeploy-build" / "startnet.cmd").read_text(
        encoding="utf-8",
    )
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "Invoke-OSDeployBridge.ps1" in text
    assert "startnet.cmd" in text
    assert "config.json" in text
    assert "192.168.2.4" not in (
        app_root / "tools" / "osdeploy-build" / "config.json"
    ).read_text(encoding="utf-8")
    assert "OSDEPLOY_BRIDGE_LIBRARY_ONLY" in bridge
    assert "/api/osdeploy/v1/pe/register" in bridge
    assert "/api/osdeploy/v1/pe/package/$runId" in bridge
    assert "/api/osdeploy/v1/runs/$RunId/events" in bridge
    assert "Invoke-OSDeployBridge.ps1" in startnet


def test_osdeploy_bridge_applies_server_image_and_stages_v2_agent_package():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "Find-OSDeployInstallImage" in bridge
    assert "Initialize-OSDeploySystemDisk" in bridge
    assert "dism.exe /Apply-Image" in bridge
    assert "/ImageFile:$InstallImage" in bridge
    assert "/Index:$ImageIndex" in bridge
    assert "bcdboot.exe" in bridge
    assert "Save-OSDeployOsdClientPackage" in bridge
    assert "SetupComplete.cmd" in bridge
    assert "osdeploy_image_applied" in bridge
    assert "osdeploy_boot_files_staged" in bridge
    assert "Stop-OSDeployWinPEForDiskBoot" in bridge
    assert "wpeutil.exe shutdown" in bridge
    assert "Restart-Computer" not in bridge


def test_osdeploy_bridge_has_fixed_pe_step_progress_and_heartbeat_contract():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "$script:OSDeployHeartbeatIntervalSeconds = 15" in bridge
    assert "$script:OSDeployPeSteps = @(" in bridge
    for step in (
        "register",
        "package",
        "locate_image",
        "guard_existing_windows",
        "partition",
        "apply_image",
        "inject_drivers",
        "stage_osd_client",
        "stage_unattend",
        "bcdboot",
        "handoff",
    ):
        assert f"'{step}'" in bridge
    assert "Invoke-OSDeployPeStep" in bridge
    assert "osdeploy_pe_step_starting" in bridge
    assert "osdeploy_pe_step_heartbeat" in bridge
    assert "osdeploy_pe_step_ok" in bridge
    assert "osdeploy_pe_step_error" in bridge
    assert "Invoke-Expression" not in bridge


def test_osdeploy_bridge_blocks_existing_windows_before_disk_clean():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "Test-OSDeployExistingWindowsInstall" in bridge
    assert "osdeploy_existing_windows_guard_blocked" in bridge
    assert "Windows\\System32\\ntoskrnl.exe" in bridge
    assert bridge.index("Test-OSDeployExistingWindowsInstall") < bridge.index(
        "Initialize-OSDeploySystemDisk -FirmwareType $firmwareType"
    )


def test_osdeploy_bridge_verifies_staged_osd_client_file_hashes():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "Verify-OSDeployStagedFileHash" in bridge
    assert "staged file SHA256 mismatch" in bridge
    assert "osdeploy_staged_file_verified" in bridge
    assert "Save-OSDeployOsdClientPackage" in bridge


def test_osdeploy_bridge_carries_agent_bootstrap_into_full_os_config():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "osdeploy_agent" in bridge
    assert "bootstrap_token" in bridge
    assert "bootstrap_url" in bridge
    assert "config_url" in bridge
    assert "agent_heartbeat_url" in bridge


def test_osd_client_posts_osdeploy_full_os_heartbeat_when_bootstrap_is_present():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    client = (
        app_root / "files" / "osd-client" / "OsdClient.ps1"
    ).read_text(encoding="utf-8")

    assert "Invoke-OsdeployAgentBootstrapHeartbeat" in client
    assert "/api/agent/v1/bootstrap" in client
    assert "/api/agent/v1/heartbeat" in client
    assert "qga_state" in client
    assert "osdeploy_full_os_heartbeat" in client
    assert "Invoke-OsdeployFirstBootReadiness" in client
    assert "Invoke-InstallQga -Required" in client
    assert "QEMU Guest Agent installer exited" in client
    assert "checking for a usable service before failing" in client
    assert "QEMU Guest Agent recovered after installer exit" in client
    assert client.index("Invoke-OsdeployFirstBootReadiness -Config $cfg") < client.index(
        "Invoke-OsdeployAgentBootstrapHeartbeat -Config $cfg"
    )
    assert "guest-agent\\qemu-ga-x86_64.msi" in client
    assert "Get-OsdObjectProperty -Value $Action.params -Name 'required'" in client


def test_osd_client_package_includes_staged_qga_msi_when_available(tmp_path):
    from web import osd_package

    files = [
        tmp_path / "SetupComplete.cmd",
        tmp_path / "osd-client" / "OsdClient.ps1",
        tmp_path / "FixRecoveryPartition.ps1",
        tmp_path / "Get-WindowsAutopilotInfo.ps1",
        tmp_path / "guest-agent" / "qemu-ga-x86_64.msi",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")

    package_files = osd_package.osd_client_files(tmp_path)
    targets = {item["path"] for item in package_files}

    assert r"V:\ProgramData\ProxmoxVEAutopilot\OSD\guest-agent\qemu-ga-x86_64.msi" in targets


def test_osdeploy_bridge_injects_virtio_drivers_before_first_boot():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "Find-OSDeployVirtIODriverRoot" in bridge
    assert "Resolve-OSDeployVirtIOInf" in bridge
    assert "Add-OSDeployVirtIODrivers" in bridge
    assert "viostor.inf" in bridge
    assert "netkvm.inf" in bridge
    assert "balloon.inf" in bridge
    assert "/Add-Driver" in bridge
    assert "Split-Path -Leaf $imageRoot" in bridge
    assert "dism.exe /Image:$imageRoot /Add-Driver" in bridge
    assert "@('\\2k22\\amd64\\', '\\w11\\amd64\\', '\\amd64\\')" in bridge
    assert "/Recurse" not in bridge
    assert "osdeploy_drivers_applied" in bridge


def test_osdeploy_bridge_stages_offline_unattend_for_server_first_boot():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = (
        app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    ).read_text(encoding="utf-8")

    assert "Add-OSDeployOfflineUnattend" in bridge
    assert "Panther\\Unattend.xml" in bridge
    assert "Microsoft-Windows-Shell-Setup" in bridge
    assert "Microsoft-Windows-International-Core" in bridge
    assert "Microsoft-Windows-Security-SPP-UX" in bridge
    assert "SkipAutoActivation" in bridge
    assert "ComputerName" in bridge
    assert "Resolve-OSDeployProductKey" in bridge
    assert "Remove-OSDeployUnattendElement" in bridge
    assert "ProductKey" in bridge
    assert "OSDEPLOY_UNATTEND_PRODUCT_KEY" in bridge
    assert "OSDEPLOY_ALLOW_DEFAULT_GVLK" in bridge
    assert "Evaluation" in bridge
    assert "NPPR9-FWDCX-D2C8J-H872K-2YT43" in bridge
    assert "WX4NM-KYWYW-QJJR4-XV3QB-6VM33" in bridge
    assert "D764K-2NDRG-47T6Q-P8T8W-YP6DF" in bridge
    assert "-ImageName (Get-OSDeployObjectProperty -Value $packageArtifact -Name 'image_name')" in bridge
    assert "InputLocale" in bridge
    assert "SystemLocale" in bridge
    assert "UILanguage" in bridge
    assert "UserLocale" in bridge
    assert "en-US" in bridge
    assert "OOBE" in bridge
    assert "HideEULAPage" in bridge
    assert "HideLocalAccountScreen" in bridge
    assert "SkipMachineOOBE" in bridge
    assert "SkipUserOOBE" in bridge
    assert "LocalAccount" in bridge
    assert "AutoLogon" in bridge
    assert "osdeploy_unattend_staged" in bridge


def test_osdeploy_unattend_omits_product_key_by_default(tmp_path):
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh is required for PowerShell unattend generation contract test")

    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    windows_root = tmp_path / "Windows"
    script = f"""
        $ErrorActionPreference = 'Stop'
        $env:OSDEPLOY_BRIDGE_LIBRARY_ONLY = '1'
        $env:OSDEPLOY_ALLOW_DEFAULT_GVLK = $null
        $env:OSDEPLOY_UNATTEND_PRODUCT_KEY = $null
        . '{bridge.as_posix()}'
        $path = Add-OSDeployOfflineUnattend `
            -WindowsRoot '{windows_root.as_posix()}' `
            -ComputerName 'EvalClient001' `
            -Version 'Windows Server 2022' `
            -Edition 'Datacenter' `
            -ImageName 'Windows Server 2022 Datacenter'
        [xml]$xml = Get-Content -LiteralPath $path -Raw
        $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
        $ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
        $productKey = $xml.SelectSingleNode("//u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:ProductKey", $ns)
        if ($null -ne $productKey) {{
            throw "ProductKey should be omitted by default but was $($productKey.InnerText)"
        }}
        $deployment = $xml.SelectSingleNode("//u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-Deployment']", $ns)
        if ($null -ne $deployment) {{
            throw "Microsoft-Windows-Deployment should not be emitted when there are no specialize commands"
        }}
        $spp = $xml.SelectSingleNode("//u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-Security-SPP-UX']/u:SkipAutoActivation", $ns)
        if ($null -eq $spp -or $spp.InnerText -ne 'true') {{
            throw "SkipAutoActivation should be emitted for unattended role first boot"
        }}
        $skipMachine = $xml.SelectSingleNode("//u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:OOBE/u:SkipMachineOOBE", $ns)
        $skipUser = $xml.SelectSingleNode("//u:settings[@pass='oobeSystem']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:OOBE/u:SkipUserOOBE", $ns)
        if ($null -eq $skipMachine -or $skipMachine.InnerText -ne 'true' -or $null -eq $skipUser -or $skipUser.InnerText -ne 'true') {{
            throw "OOBE skip flags should be emitted for unattended role first boot"
        }}
    """
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_osdeploy_unattend_uses_packaged_local_admin(tmp_path):
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh is required for PowerShell unattend generation contract test")

    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    windows_root = tmp_path / "Windows"
    script = f"""
        $ErrorActionPreference = 'Stop'
        $env:OSDEPLOY_BRIDGE_LIBRARY_ONLY = '1'
        . '{bridge.as_posix()}'
        $path = Add-OSDeployOfflineUnattend `
            -WindowsRoot '{windows_root.as_posix()}' `
            -ComputerName 'ServerLab001' `
            -Version 'Windows Server 2025' `
            -Edition 'Datacenter' `
            -ImageName 'Windows Server 2025 Datacenter' `
            -LocalAdmin ([pscustomobject]@{{
                username = 'localadmin'
                password = 'Ab7!cDef9'
            }})
        $content = Get-Content -LiteralPath $path -Raw
        foreach ($expected in @(
            '<Name>localadmin</Name>',
            '<Username>localadmin</Username>',
            '<Value>Ab7!cDef9</Value>',
            '<Group>Administrators</Group>'
        )) {{
            if ($content -notmatch [regex]::Escape($expected)) {{
                throw "missing $expected"
            }}
        }}
    """
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_osdeploy_unattend_can_emit_explicit_product_key(tmp_path):
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh is required for PowerShell unattend generation contract test")

    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    bridge = app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1"
    windows_root = tmp_path / "Windows"
    explicit_key = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"
    script = f"""
        $ErrorActionPreference = 'Stop'
        $env:OSDEPLOY_BRIDGE_LIBRARY_ONLY = '1'
        $env:OSDEPLOY_UNATTEND_PRODUCT_KEY = '{explicit_key}'
        . '{bridge.as_posix()}'
        $path = Add-OSDeployOfflineUnattend `
            -WindowsRoot '{windows_root.as_posix()}' `
            -ComputerName 'EvalClient001' `
            -Version 'Windows Server 2025' `
            -Edition 'Datacenter' `
            -ImageName 'Windows Server 2025 Datacenter'
        [xml]$xml = Get-Content -LiteralPath $path -Raw
        $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
        $ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
        $productKey = $xml.SelectSingleNode("//u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-Shell-Setup']/u:ProductKey", $ns)
        if ($null -eq $productKey -or $productKey.InnerText -ne '{explicit_key}') {{
            throw "Explicit ProductKey was not emitted"
        }}
    """
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_osdeploy_build_script_injects_bridge_into_boot_wim_and_rebuilds_iso():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )

    assert "Inject-OSDeployWinPEBridge" in text
    assert "/Mount-Image" in text
    assert "Windows\\System32\\startnet.cmd" in text
    assert "Windows\\System32\\winpeshl.ini" in text
    assert "%SYSTEMROOT%\\System32\\cmd.exe, /c X:\\Windows\\System32\\startnet.cmd" in text
    assert "Invoke-OSDeployBridge.ps1" in text
    assert "WinPE-PowerShell" in text
    assert "WinPE-DismCmdlets" in text
    assert "oscdimg.exe" in text


def test_osdeploy_build_script_uses_adk_winpe_boot_image_for_bridge():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )

    assert "Resolve-CopypePath" in text
    assert "copype.cmd" in text
    assert "Copy-Item -LiteralPath $adkBootWim -Destination $bootWim -Force" in text
    assert "Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" in text
    assert "vioscsi.inf" in text
    assert "netkvm.inf" in text


def test_osdeploy_bridge_prefers_apply_image_index_from_package():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1").read_text(
        encoding="utf-8",
    )

    assert "apply_image_index" in text
    assert "output_image_index" in text
    assert "image_index" in text


def test_osdeploy_bridge_uses_bios_partitioning_for_legacy_boot():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1").read_text(
        encoding="utf-8",
    )

    assert "Get-OSDeployFirmwareType" in text
    assert "PEFirmwareType" in text
    assert "convert mbr" in text
    assert "active" in text
    assert "$bootMode = $(if ($FirmwareType -eq 'BIOS') { 'BIOS' } else { 'UEFI' })" in text


def test_osdeploy_bridge_injects_qga_virtio_serial_driver():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "Invoke-OSDeployBridge.ps1").read_text(
        encoding="utf-8",
    )

    assert "'vioser'" in text
    assert "'vioser.inf'" in text


def test_osdeploy_build_manifest_preserves_native_server_source_index():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )

    assert "Resolve-OSDeployOutputImageIndex" in text
    assert "Get-WindowsImage -ImagePath $WimPath" in text
    assert "$images.Count -eq 1" in text
    assert 'if ($buildEngine -eq "native-server-media")' in text
    assert "$effectiveImageIndex = $ImageIndex" in text
    assert "image_index = $effectiveImageIndex" in text
    assert "output_image_index = $outputImageIndex" in text


def test_osdeploy_custom_build_command_still_injects_pe_bridge():
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "tools" / "osdeploy-build" / "build-osdeploy.ps1").read_text(
        encoding="utf-8",
    )

    assert "$customIsoPath" in text
    assert "Move-Item -LiteralPath $isoPath -Destination $customIsoPath" in text
    assert "Inject-OSDeployWinPEBridge `" in text
    assert "-IsoPath $customIsoPath" in text
