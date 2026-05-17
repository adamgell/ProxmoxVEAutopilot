import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient


def test_setup_routes_are_public_without_broad_auth_exemption():
    from web import auth

    assert auth.is_exempt_path("/setup")
    assert auth.is_exempt_path("/api/setup/v1/state")
    assert not auth.is_exempt_path("/setup-anything")


def test_setup_source_bundle_excludes_generated_dotnet_build_trees(tmp_path, monkeypatch):
    from web import app as web_app

    source_root = tmp_path / "repo"
    generated = source_root / "autopilot-agent" / "src" / "AutopilotAgent"
    generated_bin = generated / "bin" / "Release"
    generated_obj = generated / "obj"
    generated_bin.mkdir(parents=True)
    generated_obj.mkdir(parents=True)
    (generated / "Program.cs").write_text("namespace AutopilotAgent;\n", encoding="utf-8")
    (generated_bin / "AutopilotAgent.dll").write_bytes(b"compiled")
    (generated_obj / "project.assets.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AUTOPILOT_SOURCE_BUNDLE_ROOT", str(source_root))

    body = web_app._create_source_bundle_zip()

    with ZipFile(BytesIO(body)) as zf:
        names = set(zf.namelist())
    assert "autopilot-agent/src/AutopilotAgent/Program.cs" in names
    assert "autopilot-agent/src/AutopilotAgent/bin/Release/AutopilotAgent.dll" not in names
    assert "autopilot-agent/src/AutopilotAgent/obj/project.assets.json" not in names


def test_setup_source_bundle_excludes_cached_os_media(tmp_path, monkeypatch):
    from web import app as web_app

    source_root = tmp_path / "repo"
    script = source_root / "autopilot-proxmox" / "tools" / "osdeploy-build"
    cache = source_root / "autopilot-proxmox" / "cache" / "cloudosd"
    script.mkdir(parents=True)
    cache.mkdir(parents=True)
    (script / "build-osdeploy.ps1").write_text("Write-Host build\n", encoding="utf-8")
    (cache / "windows.esd").write_bytes(b"cached media")
    monkeypatch.setenv("AUTOPILOT_SOURCE_BUNDLE_ROOT", str(source_root))

    body = web_app._create_source_bundle_zip()

    with ZipFile(BytesIO(body)) as zf:
        names = set(zf.namelist())
    assert "autopilot-proxmox/tools/osdeploy-build/build-osdeploy.ps1" in names
    assert "autopilot-proxmox/cache/cloudosd/windows.esd" not in names


def test_first_run_setup_api_redacts_state_and_reports_media_gate(tmp_path, monkeypatch):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    state_path.write_text(json.dumps({
        "phase": "foundation",
        "base_url": "http://192.168.2.181:5000",
        "controller_name": "autopilot-controller-01",
        "controller_ip": "192.168.2.181",
        "controller_url": "http://192.168.2.181:5000",
        "controller_vm_ready": True,
        "controller_runtime_ready": True,
        "controller_auth_mode": "local",
        "controller_migration_bundle": "/opt/ProxmoxVEAutopilot/migration/autopilot-pve-migration-20260515T120000Z.tar.gz",
        "controller_migration_bundle_restored": True,
        "pve_node": "pvetest",
        "pve_iso_storage": "local",
        "pve_foundation_ready": True,
        "pve_permissions_ready": True,
        "seed_agent_ready": True,
        "console_health_ready": True,
        "virtio_iso_ready": True,
        "virtio_iso_volid": "local:iso/virtio-win.iso",
        "windows_iso_ready": False,
        "windows_iso_download_attempted": True,
        "windows_iso_download_source": "microsoft-evaluation-center",
        "windows_iso_download_language": "English (United States)",
        "windows_iso_download_product": "Windows 11 Enterprise Evaluation",
        "windows_iso_download_sku": "evaluation-center",
        "windows_iso_download_expires_at": "",
        "windows_iso_download_error": "ErrorSettings.SentinelReject",
        "vault_proxmox_api_token_secret": "must-not-leak",
    }), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "producer": "build_seed_agent_container.sh",
        "git_sha": "abc123",
        "sdk_image": "mcr.microsoft.com/dotnet/sdk:8.0",
        "runtime_identifiers": ["win-x64", "win-arm64"],
        "files": [{"path": "win-x64/AutopilotAgent.exe"}],
    }), encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "AGENT_SEED_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(web_app, "_auth_config", lambda: {
        "mode": "local",
        "requested_mode": "local",
        "entra_configured": False,
        "local_enabled": True,
        "tenant_id": "",
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "admin_group_id": None,
    })

    client = TestClient(web_app.app)
    res = client.get("/api/setup/v1/state")

    assert res.status_code == 200
    data = res.json()
    assert data["phase"] == "media-gated"
    assert data["controller"]["name"] == "autopilot-controller-01"
    assert data["controller"]["ip"] == "192.168.2.181"
    assert data["controller"]["url"] == "http://192.168.2.181:5000"
    assert data["controller"]["auth_mode"] == "local"
    assert data["controller"]["migration_bundle_restored"] is True
    assert data["media"]["windows_iso_ready"] is False
    assert data["media"]["windows_iso_download_attempted"] is True
    assert data["media"]["windows_iso_download_source"] == "microsoft-evaluation-center"
    assert data["media"]["windows_iso_download_product"] == "Windows 11 Enterprise Evaluation"
    assert data["media"]["windows_iso_download_error"] == "ErrorSettings.SentinelReject"
    assert data["state"]["windows_iso_download_source"] == "microsoft-evaluation-center"
    assert data["state"]["windows_iso_download_error"] == "ErrorSettings.SentinelReject"
    assert data["media"]["virtio_iso_volid"] == "local:iso/virtio-win.iso"
    assert data["seed_agent"]["producer"] == "build_seed_agent_container.sh"
    assert "/api/setup/v1/build-host/seed-iso" in data["commands"]["generate_build_host_seed_iso"]
    assert "/api/setup/v1/build-host/vm" in data["commands"]["create_build_host_vm"]
    assert "must-not-leak" not in res.text


def test_setup_state_normalizes_legacy_buildhost_keys(tmp_path, monkeypatch):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    state_path.write_text(json.dumps({
        "buildhost_vm_ready": True,
        "buildhost_vmid": "100",
        "buildhost_node": "pvetest",
        "buildhost_seed_iso_volid": "local:iso/autopilot-buildhost-seed-100.iso",
        "build_host_unattend_ready": True,
        "build_host_agent_auto_approve": True,
        "build_host_expected_agent_id": "buildhost-100",
        "build_host_expected_computer_name": "AUTOPILOT-BLD",
        "build_host_admin_user": "autopilotbuilder",
    }), encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)

    state = web_app._public_setup_state()

    assert state["build_host_vm_ready"] is True
    assert state["build_host_vmid"] == "100"
    assert state["build_host_node"] == "pvetest"
    assert state["seed_iso_ready"] is True
    assert state["seed_iso_volid"] == "local:iso/autopilot-buildhost-seed-100.iso"
    assert state["build_host_unattend_ready"] is True
    assert state["build_host_agent_auto_approve"] is True
    assert state["build_host_expected_agent_id"] == "buildhost-100"
    assert state["build_host_admin_user"] == "autopilotbuilder"


def test_setup_build_host_status_discovers_existing_vm_when_state_was_recreated(monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_proxmox_api", lambda path, *args, **kwargs: [
        {
            "type": "qemu",
            "vmid": 101,
            "name": "autopilot-buildhost-01",
            "node": "pvetest",
            "status": "running",
        }
    ] if path == "/cluster/resources?type=vm" else [])

    status = web_app._setup_build_host_status({
        "pve_node": "pvetest",
        "build_host_name": "autopilot-buildhost-01",
    })

    assert status["vmid"] == "101"
    assert status["node"] == "pvetest"
    assert status["expected_agent_id"] == "buildhost-101"
    assert status["agent_state"] == "missing"


def test_setup_page_and_unconfigured_login_render_first_run_path(tmp_path, monkeypatch):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    state_path.write_text(json.dumps({
        "base_url": "http://192.168.2.181:5000",
        "controller_name": "autopilot-controller-01",
        "controller_ip": "192.168.2.181",
        "controller_url": "http://192.168.2.181:5000",
        "controller_vmid": "101",
        "controller_vm_ready": True,
        "controller_runtime_ready": True,
        "controller_auth_mode": "local",
        "controller_migration_bundle": "/opt/ProxmoxVEAutopilot/migration/autopilot-pve-migration-20260515T120000Z.tar.gz",
        "controller_migration_bundle_restored": True,
        "pve_node": "pvetest",
        "pve_iso_storage": "local",
        "console_health_ready": True,
        "seed_agent_ready": True,
        "virtio_iso_ready": True,
        "windows_iso_ready": False,
        "windows_iso_download_error": "ErrorSettings.SentinelReject",
        "build_host_vmid": "100",
        "build_host_unattend_ready": True,
        "build_host_agent_auto_approve": True,
        "build_host_expected_agent_id": "buildhost-100",
        "build_host_admin_user": "autopilotbuilder",
        "seed_iso_volid": "local:iso/autopilot-buildhost-seed-100.iso",
    }), encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "AGENT_SEED_MANIFEST_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(web_app, "_auth_config", lambda: {
        "mode": "local",
        "requested_mode": "local",
        "entra_configured": False,
        "local_enabled": True,
        "tenant_id": "",
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "admin_group_id": None,
    })

    client = TestClient(web_app.app)
    setup = client.get("/setup")
    login = client.get("/auth/login")

    assert setup.status_code == 200
    assert "Proxmox VE Autopilot first-run setup" in setup.text
    assert "Controller VM" in setup.text
    assert "autopilot-controller-01" in setup.text
    assert "192.168.2.181" in setup.text
    assert "http://192.168.2.181:5000" in setup.text
    assert "local auth active" in setup.text
    assert "migration restored" in setup.text
    assert "Windows ISO" in setup.text
    assert "ErrorSettings.SentinelReject" in setup.text
    assert "https://www.microsoft.com/en-us/software-download/windows11" in setup.text
    assert "https://www.microsoft.com/en-us/evalcenter/evaluate-windows-11-enterprise" in setup.text
    assert "virtio-win.iso" in setup.text
    assert "Build Host" in setup.text
    assert "autopilot-buildhost-01" in setup.text
    assert "buildhost-100" in setup.text
    assert "auto for expected identity" in setup.text
    assert "autopilotbuilder" in setup.text
    assert login.status_code == 200
    assert "Continue locally" in login.text
    assert "local operator" in login.text
    assert "Sign in with Microsoft" not in login.text


def test_setup_build_host_seed_iso_endpoint_generates_agent_bootstrap_media(
    tmp_path,
    monkeypatch,
):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "fleet-bootstrap-token").write_text("fleet-token-123", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secret_dir)
    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve1",
        "proxmox_iso_storage": "local",
    })
    monkeypatch.setattr(web_app, "_auth_config", lambda: {
        "mode": "local",
        "requested_mode": "local",
        "entra_configured": False,
        "local_enabled": True,
        "tenant_id": "",
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "admin_group_id": None,
    })

    staged_files = {}

    def fake_build_iso(stage_dir: Path, iso_path: Path):
        staged_files["autounattend"] = (stage_dir / "Autounattend.xml").read_text(encoding="utf-8")
        staged_files["bootstrap"] = (stage_dir / "bootstrap-build-host.ps1").read_text(encoding="utf-8")
        staged_files["agent_config"] = json.loads((stage_dir / "agent.json").read_text(encoding="utf-8"))
        iso_path.write_bytes(b"seed iso")

    uploads = []

    def fake_proxmox_api(path, *, method="GET", data=None, files=None, **kwargs):
        uploads.append({
            "path": path,
            "method": method,
            "data": data,
            "filename": files["filename"][0] if files else None,
        })
        return {"data": "local:iso/autopilot-buildhost-seed-100.iso"}

    monkeypatch.setattr(web_app, "_build_oemdrv_iso", fake_build_iso)
    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)
    monkeypatch.setattr(web_app, "_sleep", lambda _seconds: None)

    client = TestClient(web_app.app)
    response = client.post(
        "/api/setup/v1/build-host/seed-iso",
        json={
            "vmid": 100,
            "controller_url": "http://controller:5000",
            "node": "pve1",
            "storage": "local",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["seed_iso_volid"] == "local:iso/autopilot-buildhost-seed-100.iso"
    assert body["expected_agent_id"] == "buildhost-100"
    assert "bootstrap-build-host.ps1" in staged_files["autounattend"]
    assert "<Key>/IMAGE/INDEX</Key>" in staged_files["autounattend"]
    assert "<ProductKey>" in staged_files["autounattend"]
    assert "<Key></Key>" in staged_files["autounattend"]
    assert "<WillShowUI>Never</WillShowUI>" in staged_files["autounattend"]
    assert "Windows Server 2022 SERVERDATACENTER" not in staged_files["autounattend"]
    assert "WX4NM-KYWYW-QJJR4-XV3QB-6VM33" not in staged_files["autounattend"]
    assert r"G:\vioscsi\w11\amd64" in staged_files["autounattend"]
    assert r"G:\NetKVM\w11\amd64" in staged_files["autounattend"]
    assert r"vioserial\w11\amd64\vioser.inf" in staged_files["autounattend"]
    assert r"Balloon\w11\amd64\balloon.inf" in staged_files["autounattend"]
    assert "<HideLocalAccountScreen>true</HideLocalAccountScreen>" in staged_files["autounattend"]
    assert "<HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>" in staged_files["autounattend"]
    assert "<NetworkLocation>Work</NetworkLocation>" in staged_files["autounattend"]
    assert '<component name="Microsoft-Windows-International-Core" processorArchitecture="amd64"' in staged_files["autounattend"]
    assert "<InputLocale>en-US</InputLocale>" in staged_files["autounattend"]
    assert "<SystemLocale>en-US</SystemLocale>" in staged_files["autounattend"]
    assert "<UILanguage>en-US</UILanguage>" in staged_files["autounattend"]
    assert "<UserLocale>en-US</UserLocale>" in staged_files["autounattend"]
    assert "<Order>3</Order>" in staged_files["autounattend"]
    assert "AutopilotAgent.exe" in staged_files["bootstrap"]
    assert "Set-ItemProperty -LiteralPath $configPath -Name IsReadOnly -Value $false" in staged_files["bootstrap"]
    assert staged_files["agent_config"]["serverUrl"] == "http://controller:5000"
    assert staged_files["agent_config"]["bootstrapToken"] == "fleet-token-123"
    assert staged_files["agent_config"]["agentId"] == "buildhost-100"
    assert staged_files["agent_config"]["role"] == "build-host"
    assert "build_osdeploy" in staged_files["agent_config"]["capabilities"]
    assert uploads == [{
        "path": "/nodes/pve1/storage/local/upload",
        "method": "POST",
        "data": {"content": "iso"},
        "filename": "autopilot-buildhost-seed-100.iso",
    }]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["seed_iso_ready"] is True
    assert state["build_host_unattend_ready"] is True
    assert state["build_host_agent_auto_approve"] is True
    assert state["build_host_expected_agent_id"] == "buildhost-100"
    assert state["seed_iso_volid"] == "local:iso/autopilot-buildhost-seed-100.iso"


def test_setup_build_host_seed_iso_recovers_missing_fleet_bootstrap_token(
    tmp_path,
    monkeypatch,
):
    from web import app as web_app

    app_dir = tmp_path / "app"
    state_path = tmp_path / "foundation_state.json"
    secret_dir = tmp_path / "secrets"
    app_dir.mkdir()
    secret_dir.mkdir()
    (app_dir / ".env").write_text(
        "AUTOPILOT_BASE_URL=http://controller:5000\n"
        "AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256=old-hash\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "BASE_DIR", app_dir)
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secret_dir)
    monkeypatch.setenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256", "old-hash")
    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve1",
        "proxmox_iso_storage": "local",
    })

    staged_files = {}

    def fake_build_iso(stage_dir: Path, iso_path: Path):
        staged_files["agent_config"] = json.loads((stage_dir / "agent.json").read_text(encoding="utf-8"))
        iso_path.write_bytes(b"seed iso")

    monkeypatch.setattr(web_app, "_build_oemdrv_iso", fake_build_iso)
    monkeypatch.setattr(
        web_app,
        "_proxmox_api",
        lambda path, *, method="GET", data=None, files=None, **kwargs: {"data": "ok"},
    )

    client = TestClient(web_app.app)
    response = client.post(
        "/api/setup/v1/build-host/seed-iso",
        json={
            "vmid": 100,
            "controller_url": "http://controller:5000",
            "node": "pve1",
            "storage": "local",
        },
    )

    assert response.status_code == 202, response.text
    generated_token = (secret_dir / "fleet-bootstrap-token").read_text(encoding="utf-8").strip()
    generated_hash = sha256(generated_token.encode("utf-8")).hexdigest()
    assert generated_token
    assert staged_files["agent_config"]["bootstrapToken"] == generated_token
    assert staged_files["agent_config"]["agentId"] == "buildhost-100"
    assert web_app.os.environ["AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256"] == generated_hash
    env_text = (app_dir / ".env").read_text(encoding="utf-8")
    assert f"AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256={generated_hash}" in env_text
    assert generated_token not in env_text


def test_setup_build_host_vm_endpoint_creates_windows_build_host(
    tmp_path,
    monkeypatch,
):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "fleet-bootstrap-token").write_text("fleet-token-123", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secret_dir)
    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve1",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })
    monkeypatch.setattr(web_app, "_auth_config", lambda: {
        "mode": "local",
        "requested_mode": "local",
        "entra_configured": False,
        "local_enabled": True,
        "tenant_id": "",
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "admin_group_id": None,
    })
    monkeypatch.setattr(
        web_app,
        "_build_oemdrv_iso",
        lambda stage_dir, iso_path: iso_path.write_bytes(b"seed iso"),
    )

    calls = []

    def fake_proxmox_api(path, *, method="GET", data=None, files=None, **kwargs):
        calls.append({"path": path, "method": method, "data": data, "files": bool(files)})
        if path == "/cluster/nextid":
            return 100
        if path.endswith("/upload"):
            return {"data": "local:iso/autopilot-buildhost-seed-100.iso"}
        return {"data": "UPID:test"}

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)
    monkeypatch.setattr(web_app, "_sleep", lambda _seconds: None)

    client = TestClient(web_app.app)
    response = client.post(
        "/api/setup/v1/build-host/vm",
        json={
            "windows_iso_volid": "local:iso/en-us_windows_server_2022.iso",
            "virtio_iso_volid": "local:iso/virtio-win.iso",
            "controller_url": "http://controller:5000",
        },
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["vmid"] == 100
    assert body["seed_iso_volid"] == "local:iso/autopilot-buildhost-seed-100.iso"
    create_call = next(item for item in calls if item["path"] == "/nodes/pve1/qemu")
    assert create_call["method"] == "POST"
    assert create_call["data"]["name"] == "autopilot-buildhost-01"
    assert create_call["data"]["ide0"] == "local:iso/autopilot-buildhost-seed-100.iso,media=cdrom"
    assert create_call["data"]["ide2"] == "local:iso/en-us_windows_server_2022.iso,media=cdrom"
    assert create_call["data"]["ide3"] == "local:iso/virtio-win.iso,media=cdrom"
    assert create_call["data"]["tpmstate0"] == "local-lvm:1,version=v2.0"
    assert create_call["data"]["boot"] == "order=ide2;scsi0;ide0"
    assert create_call["data"]["net0"] == "virtio,bridge=vmbr0"
    assert create_call["data"]["agent"] == "enabled=1"
    assert any(item["path"] == "/nodes/pve1/qemu/100/status/start" for item in calls)
    sendkey_calls = [item for item in calls if item["path"] == "/nodes/pve1/qemu/100/sendkey"]
    assert len(sendkey_calls) == 3
    assert all(item["method"] == "PUT" for item in sendkey_calls)
    assert all(item["data"] == {"key": "spc"} for item in sendkey_calls)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["build_host_vm_ready"] is True
    assert state["build_host_vmid"] == "100"
    assert state["build_host_expected_agent_id"] == "buildhost-100"


def test_setup_build_host_repair_clears_readonly_agent_config(tmp_path, monkeypatch):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "fleet-bootstrap-token").write_text("fleet-token-123", encoding="utf-8")
    state_path.write_text(json.dumps({
        "controller_url": "http://controller:5000",
        "build_host_vmid": "100",
        "build_host_node": "pve1",
        "build_host_expected_agent_id": "buildhost-100",
        "build_host_expected_computer_name": "AUTOPILOT-BLD",
    }), encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secret_dir)

    captured = {}

    def fake_guest_exec_ps_status(node, vmid, script, *, timeout_s):
        captured["node"] = node
        captured["vmid"] = vmid
        captured["script"] = script
        return {
            "ok": True,
            "out": json.dumps({
                "ok": True,
                "vmid": vmid,
                "serverUrl": "http://controller:5000",
                "expectedAgentId": "buildhost-100",
                "expectedComputerName": "AUTOPILOT-BLD",
            }),
        }

    monkeypatch.setattr(web_app, "_guest_exec_ps_status", fake_guest_exec_ps_status)

    client = TestClient(web_app.app)
    response = client.post("/api/setup/v1/build-host/repair-agent")

    assert response.status_code == 200, response.text
    assert captured["node"] == "pve1"
    assert captured["vmid"] == 100
    assert "Set-ItemProperty -LiteralPath $configPath -Name IsReadOnly -Value $false" in captured["script"]
    assert "agentToken -NotePropertyValue $null" in captured["script"]
    assert "$programDataExe = Join-Path (Split-Path -Parent $configPath) 'AutopilotAgent.exe'" in captured["script"]
    assert "$targetExePaths = @($exe, $programDataExe)" in captured["script"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["build_host_agent_auto_approve"] is True
    assert state["build_host_vmid"] == "100"
    assert state["build_host_expected_agent_id"] == "buildhost-100"
    assert state["build_host_expected_computer_name"] == "AUTOPILOT-BLD"


def test_setup_build_host_repair_reboots_when_qga_is_unavailable(tmp_path, monkeypatch):
    from web import app as web_app

    state_path = tmp_path / "foundation_state.json"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "fleet-bootstrap-token").write_text("fleet-token-123", encoding="utf-8")
    state_path.write_text(json.dumps({
        "controller_url": "http://controller:5000",
        "build_host_vmid": "100",
        "build_host_node": "pve1",
        "build_host_expected_agent_id": "buildhost-100",
        "build_host_expected_computer_name": "AUTOPILOT-BLD",
    }), encoding="utf-8")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", state_path)
    monkeypatch.setattr(web_app, "SECRETS_DIR", secret_dir)
    monkeypatch.setattr(web_app, "_guest_exec_ps_status", lambda *args, **kwargs: {
        "ok": False,
        "error": "guest exec rejected: QEMU guest agent is not running",
        "out": "",
        "err": "",
    })
    calls = []

    def fake_proxmox_api(path, method="GET", data=None, files=None):
        calls.append({"path": path, "method": method, "data": data})
        return {"upid": "UPID:pve1:reboot"}

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)

    client = TestClient(web_app.app)
    response = client.post("/api/setup/v1/build-host/repair-agent")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["rebooted"] is True
    assert body["result"]["next_expected_state"] == "wait_for_qga_then_rerun_repair"
    assert calls == [{
        "path": "/nodes/pve1/qemu/100/status/reset",
        "method": "POST",
        "data": {},
    }]


def test_local_operator_session_unlocks_cockpit_without_entra(monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_AUTH_BYPASS", False)
    monkeypatch.setattr(web_app.job_manager, "list_jobs", lambda: [])
    monkeypatch.setattr(web_app, "_load_vars", lambda: {"hypervisor_type": "proxmox"})
    monkeypatch.setattr(web_app, "_auth_config", lambda: {
        "mode": "local",
        "requested_mode": "local",
        "entra_configured": False,
        "local_enabled": True,
        "tenant_id": "",
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "",
        "admin_group_id": None,
    })

    client = TestClient(web_app.app)

    blocked = client.get("/", follow_redirects=False)
    assert blocked.status_code == 302
    assert blocked.headers["location"].startswith("/auth/login?next=/")

    login = client.get("/auth/login?next=/")
    assert login.status_code == 200
    assert "Continue locally" in login.text

    started = client.post("/auth/local/start?next=/", follow_redirects=False)
    assert started.status_code == 303
    assert started.headers["location"] == "/"

    cockpit = client.get("/")
    assert cockpit.status_code == 200
    assert "Proxmox VE Autopilot" in cockpit.text
    assert "Local Operator" in cockpit.text
