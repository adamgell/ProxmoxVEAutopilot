from fastapi.testclient import TestClient


def test_cockpit_shell_renders_on_dashboard(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app.job_manager, "list_jobs", lambda: [])

    res = web_client.get("/")
    assert res.status_code == 200
    body = res.text
    assert 'class="cockpit-shell ' in body
    assert 'id="cockpitCommand"' in body
    assert 'class="cockpit-rail"' in body
    assert 'href="/monitoring/settings"' in body


def test_cloud_retire_flow_requires_typed_confirmation(web_client: TestClient, monkeypatch):
    from web import app as web_app

    devices_db = web_app.devices_db

    monkeypatch.setattr(web_app, "_pve_autopilot_vms_by_serial", lambda: {})
    monkeypatch.setattr(
        devices_db,
        "list_grouped",
        lambda *a, **kw: (
            [],
            {
                "unmatched": [],
                "meta": {
                    "counts": {"autopilot": 0, "intune": 0, "entra": 0},
                    "counts_filtered": {"autopilot": 0, "intune": 0, "entra": 0},
                    "synced_at": None,
                },
            },
        ),
    )
    monkeypatch.setattr(devices_db, "list_deletions", lambda *a, **kw: [])

    res = web_client.get("/cloud")
    assert res.status_code == 200
    assert "function nukeSelected" in res.text
    assert "Type RETIRE" in res.text


def test_login_keeps_microsoft_start_link(monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(
        web_app,
        "_auth_config",
        lambda: {
            "tenant_id": "tenant-123",
            "client_id": "client-123",
            "authority": "https://login.microsoftonline.com/tenant-123",
            "redirect_uri": "http://testserver/auth/callback",
        },
    )
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {"ad_realm": "EXAMPLE.LOCAL"})

    client = TestClient(web_app.app)
    res = client.get("/auth/login?next=/monitoring/settings")
    assert res.status_code == 200
    assert "Sign in with Microsoft" in res.text
    assert "/auth/login/start?next=%2Fmonitoring%2Fsettings" in res.text


def test_console_page_preserves_existing_api_contracts(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_proxmox_api", lambda path: {"name": "SERIAL-001"})
    res = web_client.get("/vms/101/console")
    assert res.status_code == 200
    body = res.text
    assert "'/api/vms/' + VMID + '/vnc-init'" in body
    assert "'/api/vms/' + VMID + '/vnc-ws'" in body
    assert "/api/vms/${VMID}/status-json" in body
    assert "/api/vms/${VMID}/action/${action}" in body
    assert "/api/vms/${VMID}/type" in body
    assert "/api/vms/${VMID}/key" in body
    assert "/api/live/ws" in body
    assert "screenshot.request" in body
    assert "requestScreenshot" in body
    assert 'download="vm-${VMID}-screenshot.png"' in body
    assert "width:max-content" in body


def test_console_resolves_actual_vm_node_for_serial(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {"proxmox_node": "pve2"})

    def fake_proxmox_api(path):
        if path == "/cluster/resources?type=vm":
            return [{"type": "qemu", "vmid": 100, "node": "pve1"}]
        assert path == "/nodes/pve1/qemu/100/config"
        return {"name": "SERIAL-100"}

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)

    res = web_client.get("/vms/100/console")

    assert res.status_code == 200
    assert "SERIAL-100" in res.text


def test_vnc_init_uses_actual_vm_node(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {"proxmox_node": "pve2"})
    monkeypatch.setattr(
        web_app,
        "_proxmox_api",
        lambda path: [{"type": "qemu", "vmid": 100, "node": "pve1"}],
    )

    def fake_post(path, data=None):
        assert path == "/nodes/pve1/qemu/100/vncproxy"
        return {"port": 5900, "ticket": "ticket-100", "user": "root@pam"}

    monkeypatch.setattr(web_app, "_proxmox_api_post", fake_post)

    res = web_client.get("/api/vms/100/vnc-init")

    assert res.status_code == 200
    assert res.json()["node"] == "pve1"


def test_home_page_uses_live_jobs_websocket(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app.job_manager, "list_jobs", lambda: [])

    res = web_client.get("/")
    assert res.status_code == 200
    body = res.text
    assert "/api/live/ws" in body
    assert 'topics: ["jobs"]' in body
    assert "applyLiveJobs" in body
