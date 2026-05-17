from pathlib import Path

from fastapi.testclient import TestClient


def test_agent_vmid_is_inferred_from_pve_vm_name():
    from web import app as web_app

    vmid = web_app._infer_agent_vmid_from_pve(
        {
            "agent_id": "agent-gell-ec41e7eb",
            "computer_name": "GELL-EC41E7EB",
            "serial_number": "",
            "vmid": None,
        },
        [
            {"vmid": 106, "name": "Gell-E9C0C757", "serial": "Gell-E9C0C757"},
            {"vmid": 116, "name": "Gell-EC41E7EB", "serial": "Gell-EC41E7EB"},
        ],
    )

    assert vmid == 116


def test_agent_vmid_is_inferred_from_pve_ip_when_identity_is_missing():
    from web import app as web_app

    vmid = web_app._infer_agent_vmid_from_pve(
        {
            "agent_id": "agent-from-retrofit",
            "computer_name": "WIN-RENAMED",
            "serial_number": "",
            "primary_ipv4": "192.168.2.104",
            "vmid": None,
        },
        [
            {"vmid": 107, "name": "Gell-2C3BD243", "ip_address": "192.168.2.90"},
            {"vmid": 116, "name": "Gell-EC41E7EB", "ip_address": "192.168.2.104"},
        ],
    )

    assert vmid == 116


def test_agent_vmid_inference_ignores_ambiguous_pve_matches():
    from web import app as web_app

    vmid = web_app._infer_agent_vmid_from_pve(
        {
            "agent_id": "agent-gell-ec41e7eb",
            "computer_name": "GELL-EC41E7EB",
            "serial_number": "",
            "vmid": None,
        },
        [
            {"vmid": 116, "name": "Gell-EC41E7EB", "serial": ""},
            {"vmid": 216, "name": "GELL_EC41E7EB", "serial": ""},
        ],
    )

    assert vmid is None


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
    assert 'id="liveSocketIndicator"' in body
    assert "WebSocket: Connecting" in body
    assert "new WebSocket" in body
    assert "/api/live/ws" in body


def test_cloudosd_run_detail_renders_v2_plan_live_section():
    template = (
        Path(__file__).resolve().parents[1]
        / "web/templates/cloudosd_run_detail.html"
    ).read_text(encoding="utf-8")

    assert "AutopilotAgent v2 Plan" in template
    assert "Why Waiting" in template
    assert "Intune &amp; Autopilot Evidence" in template
    assert "Retry step" in template
    assert "data-cloudosd-v2-steps" in template
    assert "data-cloudosd-field=\"v2_wait_reason\"" in template
    assert "data-cloudosd-field=\"autopilot_upload\"" in template
    assert "v2_completion" in template
    assert "v2_operator_status" in template
    assert "intune_evidence" in template
    assert "renderV2Steps" in template


def test_cloudosd_run_detail_renders_autopilot_readiness_section():
    template = (
        Path(__file__).resolve().parents[1]
        / "web/templates/cloudosd_run_detail.html"
    ).read_text(encoding="utf-8")

    assert "Autopilot Readiness" in template
    assert "data-cloudosd-field=\"autopilot_readiness_state\"" in template
    assert "data-cloudosd-field=\"autopilot_readiness_hash\"" in template
    assert "data-cloudosd-field=\"autopilot_readiness_upload\"" in template
    assert "data-cloudosd-field=\"autopilot_readiness_assignment\"" in template
    assert "data-cloudosd-field=\"autopilot_readiness_errors\"" in template


def test_cloudosd_cockpit_renders_archive_history_controls():
    template = Path("autopilot-proxmox/web/templates/cloudosd.html").read_text(encoding="utf-8")

    assert "OSDCloud Run History" in template
    assert "Active Runs" in template
    assert "Stale Failed Runs" in template
    assert "data-cloudosd-archive" in template
    assert "data-cloudosd-unarchive" in template
    assert "data-cloudosd-bulk-archive=\"archive-stale-failed\"" in template
    assert "data-cloudosd-bulk-archive=\"archive-completed-old\"" in template
    assert "Hide completed old" in template


def test_cloudosd_cockpit_renders_cache_warming_surface():
    template = Path("autopilot-proxmox/web/templates/cloudosd.html").read_text(encoding="utf-8")

    assert 'href="/osdcloud/cache"' in template
    assert 'id="cloudosd-cache"' in template
    assert "OSDCloud Cache" in template
    assert "Warm Windows 11 feature images" in template
    assert "data-cloudosd-cache-action=\"refresh\"" in template
    assert "data-cloudosd-cache-action=\"warm-all\"" in template
    assert "data-cloudosd-cache-warm-feature" in template
    assert "data-cloudosd-cache-warm-quality" in template
    assert "data-cloudosd-cache-verify" in template
    assert "data-cloudosd-cache-delete" in template
    assert "/api/cloudosd/cache/catalog/refresh" in template
    assert "/api/cloudosd/cache/warm-all-windows11" in template


def test_cloudosd_run_detail_keeps_readiness_live_after_completion():
    template = Path("autopilot-proxmox/web/templates/cloudosd_run_detail.html").read_text(encoding="utf-8")

    assert "readinessTerminalStates" in template
    assert "Autopilot readiness" in template
    assert "keepReadinessLive" in template
    assert "data-cloudosd-autopilot-action=\"upload\"" in template
    assert "data-cloudosd-autopilot-action=\"sync\"" in template
    assert "renderAutopilotReadiness" in template
    assert "/api/cloudosd/runs/${encodeURIComponent(runId)}/autopilot/${action}" in template


def test_provision_page_defaults_to_cloudosd_for_desktop_clients():
    template = (
        Path(__file__).resolve().parents[1] / "web/templates/provision.html"
    ).read_text(encoding="utf-8")

    assert "primary Windows desktop client path" in template
    assert (
        '<option value="cloudosd" selected>OSDCloud (Windows desktop clients)</option>'
        in template
    )
    assert "OSDeploy v2 (Windows Server / advanced installs)" in template
    assert "Legacy WinPE (fallback image apply)" in template
    assert "Clone (Windows Server / template builds)" in template
    assert "Ubuntu v2 (Desktop / Linux clients)" in template
    assert 'data-boot-section="ubuntu"' in template
    assert "Ubuntu v2 sequence" in template
    assert "Ubuntu template VMID" in template
    assert 'name="ubuntu_template_vmid"' in template
    assert "OSDCloud base deployment (no legacy sequence)" in template
    assert "OSDeploy Server base deployment" in template
    assert "OSDCloud cache" in template
    assert "feature image cache hit/miss" in template
    assert "/osdcloud/cache" in template
    assert 'data-legacy-sequence-row' in template
    assert 'data-boot-modes="cloudosd"' in template
    assert "data-cloudosd-compatible" in template
    assert "not OSDCloud-compatible yet" not in template
    assert "syncSequenceOptions" in template
    assert template.index('<option value="cloudosd" selected>') < template.index(
        '<option value="osdeploy">'
    )
    assert template.index('<option value="osdeploy">') < template.index(
        '<option value="winpe">'
    )
    assert template.index('<option value="winpe">') < template.index(
        '<option value="clone">'
    )
    assert 'data-boot-section="cloudosd" hidden' not in template
    assert '<tbody data-boot-section="cloudosd">' in template


def test_v2_builder_supports_ubuntu_target_os_palette_and_phases():
    template = Path("autopilot-proxmox/web/templates/task_engine_builder.html").read_text(encoding="utf-8")

    assert 'id="v2-target-os"' in template
    assert '<option value="ubuntu"' in template
    assert "PHASES_BY_TARGET_OS" in template
    assert '"install", "Install"' in template
    assert '"first_boot", "First Boot"' in template
    assert "PINNED_UBUNTU_DESKTOP" in template
    assert "isTemplateCompatible" in template


def test_v2_sequence_library_surfaces_ubuntu_templates():
    from web import app as web_app

    names = {template["name"]: template for template in web_app._v2_flow_templates()}
    assert names["Ubuntu Desktop Plain"]["target_os"] == "ubuntu"
    assert names["Ubuntu Desktop Intune + Edge"]["target_os"] == "ubuntu"
    assert names["Ubuntu Desktop Intune + MDE"]["target_os"] == "ubuntu"
    assert names["Ubuntu Server Minimal"]["target_os"] == "ubuntu"
    assert names["Ubuntu apt-cache Server"]["target_os"] == "ubuntu"
    assert any(
        node["kind"] == "linux_agent_heartbeat"
        for node in names["Ubuntu Desktop Plain"]["nodes"]
    )
    create_user = next(
        node for node in names["Ubuntu Desktop Plain"]["nodes"]
        if node["kind"] == "create_ubuntu_user"
    )
    assert create_user["params"]["local_admin_credential_id"] == 1


def test_linux_agent_download_uses_exempt_v2_namespace():
    from web import app as web_app

    commands = web_app._linux_agent_bootstrap_commands(
        run_id="run-1",
        vmid=123,
        hostname="ubuntu-test",
    )

    assert any("/osd/v2/ubuntu/linux-agent.py" in command for command in commands)
    assert not any("/api/osd/v2/ubuntu/linux-agent.py" in command for command in commands)


def test_linux_agent_bootstrap_prefers_guest_reachable_base_url(monkeypatch):
    import base64
    import json
    import re

    from web import app as web_app

    monkeypatch.delenv("AUTOPILOT_BASE_URL", raising=False)
    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "web_base_url": "http://127.0.0.1:5000",
        "autopilot_base_url": "http://192.168.2.4:5000",
    })
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "web_base_url": "http://127.0.0.1:5000",
        "proxmox_host": "192.168.2.200",
    })

    commands = web_app._linux_agent_bootstrap_commands(
        run_id="run-1",
        vmid=123,
        hostname="ubuntu-test",
    )

    joined = "\n".join(commands)
    config_b64 = re.search(r"b64decode\('([^']+)'\)", joined).group(1)
    config = json.loads(base64.b64decode(config_b64))

    assert "http://192.168.2.4:5000/osd/v2/ubuntu/linux-agent.py" in joined
    assert config["server_url"] == "http://192.168.2.4:5000"


def test_ubuntu_v2_per_vm_seed_runs_runtime_installs_before_agent():
    from web import app as web_app

    template_user_data = """#cloud-config
package_update: true
runcmd:
- apt-get install -y intune-portal
- apt-get install -y microsoft-edge-stable
"""
    firstboot_user_data = """#cloud-config
hostname: ubuntu-test
runcmd:
- systemctl enable --now qemu-guest-agent
"""

    merged = web_app._merge_template_runtime_cloud_init_into_firstboot(
        template_user_data,
        firstboot_user_data,
    )
    merged = web_app._append_cloud_init_runcmd(
        merged,
        ["curl -fsSL http://192.168.2.4:5000/osd/v2/ubuntu/linux-agent.py -o /opt/proxmoxveautopilot/autopilot_linux_agent.py"],
    )

    qga_index = merged.index("systemctl enable --now qemu-guest-agent")
    intune_index = merged.index("apt-get install -y intune-portal")
    edge_index = merged.index("apt-get install -y microsoft-edge-stable")
    agent_index = merged.index("/osd/v2/ubuntu/linux-agent.py")
    assert qga_index < intune_index < edge_index < agent_index


def test_ubuntu_clone_waits_for_cloud_init_exec_status():
    wait_task = (
        Path(__file__).resolve().parents[1]
        / "roles/proxmox_vm_clone_linux/tasks/wait_cloud_init.yml"
    ).read_text(encoding="utf-8")

    assert "/agent/exec-status?pid={{ _ci_exec.json.data.pid }}" in wait_task
    assert "cloud_init_exec_status" in wait_task
    assert "Fail if cloud-init returned a non-zero exit code" in wait_task


def test_build_nav_prioritizes_cloudosd_desktop_cockpit():
    template = (
        Path(__file__).resolve().parents[1] / "web/templates/base.html"
    ).read_text(encoding="utf-8")

    dropdown = template[
        template.index('id="nav-dd-provision"') : template.index(
            "</ul>", template.index('id="nav-dd-provision"')
        )
    ]
    assert dropdown.index('href="/osdcloud">OSDCloud Desktop') < dropdown.index(
        'href="/osdeploy">OSDeploy Server'
    )
    assert dropdown.index('href="/osdeploy">OSDeploy Server') < dropdown.index(
        'href="/provision">Provision VMs'
    )

    drawer = template[
        template.index('<span class="nav-drawer-group-label">Provision</span>')
        : template.index('<span class="nav-drawer-group-label">Fleet</span>')
    ]
    assert drawer.index('href="/osdcloud"') < drawer.index('href="/provision"')
    assert drawer.index('href="/osdeploy"') < drawer.index('href="/provision"')
    assert "OSDCloud Desktop" in drawer
    assert "OSDeploy Server" in drawer

    rail = template[
        template.index('<span class="cockpit-rail-label">Build</span>')
        : template.index('<span class="cockpit-rail-label">Fleet</span>')
    ]
    assert '<span aria-hidden="true">05</span>OSDCloud Desktop' in rail
    assert '<span aria-hidden="true">06</span>OSDeploy Server' in rail
    assert '<span aria-hidden="true">07</span>Provision VMs' in rail
    assert rail.index('data-route="/osdcloud"') < rail.index('data-route="/provision"')
    assert rail.index('data-route="/osdeploy"') < rail.index('data-route="/provision"')


def test_cloudosd_cockpit_copy_positions_desktop_factory():
    template = (
        Path(__file__).resolve().parents[1] / "web/templates/cloudosd.html"
    ).read_text(encoding="utf-8")

    assert "Windows desktop deployment cockpit" in template
    assert "primary client deployment path" in template
    assert "WinPE and Clone stay available for Windows Server" in template
    assert "Operator Flow" in template
    assert 'aria-label="OSDCloud pages"' in template
    assert ">Builder</a>" in template
    assert ">Cache</a>" in template
    assert ">Artifacts</a>" in template
    assert "https://www.osdcloud.com/" in template
    assert 'href="/osdcloud/builder"' in template
    assert 'href="/osdcloud/cache"' in template
    assert 'href="/osdcloud/artifacts"' in template


def test_cockpit_shell_has_light_mode_tokens(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app.job_manager, "list_jobs", lambda: [])

    res = web_client.get("/")
    assert res.status_code == 200
    body = res.text
    assert 'html:not([data-theme="dark"]) .cockpit-shell' in body
    assert "color-scheme: light;" in body
    assert "--cockpit-bg: #f5f7fb;" in body
    assert "--cockpit-panel: rgba(255, 255, 255, 0.92);" in body
    assert "--cockpit-ink: #162033;" in body


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


def test_vms_agent_heartbeat_uses_local_timezone_markup(web_client: TestClient, monkeypatch):
    from web import app as web_app

    async def fake_vms_payload():
        return {
            "data": [],
            "devices": ([], ""),
            "hash_serials": set(),
        }, 0

    monkeypatch.setattr(web_app, "_load_vars", lambda: {"hypervisor_type": "proxmox"})
    monkeypatch.setattr(web_app, "_get_vms_payload", fake_vms_payload)
    monkeypatch.setattr(web_app, "_latest_monitor_sweep_status", lambda: None)
    monkeypatch.setattr(
        web_app,
        "_agent_inventory_rows",
        lambda: [
            {
                "agent_id": "agent-timezone-test",
                "approval_id": "",
                "approval_status": "active",
                "vmid": 106,
                "computer_name": "GELL-E9C0C757",
                "serial_number": "",
                "primary_ipv4": "10.211.55.106",
                "os_name": "Microsoft Windows 11 Enterprise",
                "os_build": "26100",
                "qga_state": "Running",
                "domain_joined": True,
                "entra_joined": False,
                "current_phase": "ninja",
                "last_heartbeat_at": "2026-05-09T00:45:38+00:00",
            }
        ],
    )

    res = web_client.get("/vms")

    assert res.status_code == 200
    body = res.text
    assert "agent-timezone-test" in body
    assert "data-agent-heartbeat-time" in body
    assert 'title="UTC: 2026-05-09T00:45:38+00:00"' in body
    assert "data-agent-heartbeat-zone" in body
    assert "function renderAgentHeartbeat" in body
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in body
    assert "function reconcileAgentInventory" in body
    assert "function updateAgentInventoryEmptyState" in body
    assert "Approval record no longer exists. Removed the stale row." in body


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


def test_runs_page_lists_winpe_runs_with_sequence_and_status(
    web_client: TestClient, test_db
):
    from web import sequences_db

    seq_id = sequences_db.create_sequence(
        test_db,
        name="WinPE OSD Demo",
        description="blank disk image apply",
        produces_autopilot_hash=True,
        hash_capture_phase="winpe",
    )
    run_id = sequences_db.create_provisioning_run(
        test_db, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        test_db,
        run_id=run_id,
        vmid=119,
        vm_uuid="ABCDEF12-3456-7890-ABCD-EF1234567890",
    )

    res = web_client.get("/runs")

    assert res.status_code == 200
    body = res.text
    assert "WinPE Runs" in body
    assert "WinPE OSD Demo" in body
    assert "awaiting_winpe" in body
    assert "119" in body
    assert f"/runs/{run_id}" in body


def test_run_detail_previews_all_winpe_tasks_before_agent_registers(
    web_client: TestClient, test_db
):
    from web import sequences_db

    seq_id = sequences_db.create_sequence(
        test_db,
        name="Hash in WinPE",
        description="capture before disk apply",
        produces_autopilot_hash=True,
        hash_capture_phase="winpe",
    )
    run_id = sequences_db.create_provisioning_run(
        test_db, sequence_id=seq_id, provision_path="winpe",
    )

    res = web_client.get(f"/runs/{run_id}")

    assert res.status_code == 200
    body = res.text
    assert "Hash in WinPE" in body
    assert "WinPE Task Plan" in body
    assert "capture_hash" in body
    assert "partition_disk" in body
    assert "apply_wim" in body
    assert "handoff_to_windows_setup" in body
    assert "planned" in body


def test_sequences_page_shows_winpe_plan_and_hash_phase(
    web_client: TestClient, test_db
):
    from web import sequences_db

    sequences_db.create_sequence(
        test_db,
        name="WinPE capable sequence",
        description="shows phase zero",
        produces_autopilot_hash=True,
        hash_capture_phase="winpe",
    )

    res = web_client.get("/sequences")

    assert res.status_code == 200
    body = res.text
    assert "WinPE capable sequence" in body
    assert "Hash phase" in body
    assert "winpe" in body
    assert "WinPE task plan" in body
    assert "capture_hash" in body
    assert "apply_wim" in body


def test_task_engine_page_shows_v2_sequences_runs_and_content(
    web_client: TestClient, pg_conn
):
    from web import ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="Install Apps v2",
        description="Task Sequence Engine v2 package flow",
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install Notepad++",
        kind="install_package",
        phase="full_os",
        position=0,
        content_refs=["notepad-plus-plus"],
        retry_count=1,
    )
    item_id = ts_engine_pg.create_content_item(
        pg_conn,
        name="notepad-plus-plus",
        content_type="package",
        description="Notepad++ installer",
    )
    ts_engine_pg.create_content_version(
        pg_conn,
        content_item_id=item_id,
        version="8.6.7",
        sha256="f" * 64,
        source_uri="https://content.local/npp.8.6.7.x64.msi",
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={"vmid": 121, "vm_uuid": "vm-121"},
        resolve_content=True,
    )

    res = web_client.get("/task-engine")

    assert res.status_code == 200
    body = res.text
    assert "Task Sequence Engine v2" in body
    assert "Install Apps v2" in body
    assert "Install Notepad++" in body
    assert "install_package" in body
    assert "full_os" in body
    assert run_id in body
    assert "queued" in body
    assert "notepad-plus-plus" in body
    assert "Content Manifest" in body
    assert "Smart V2 Builder" in body
    assert 'href="/task-engine/sequences/new"' in body
    assert "Edit builder" in body


def test_task_engine_page_shows_cloudosd_v2_osd_run_plan(
    web_client: TestClient, pg_conn
):
    from web import ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="OSDCloud deployment for GELL-119-AD",
        description="Generated OSDCloud deployment sequence",
        created_by="cloudosd",
    )
    for position, (name, kind, phase) in enumerate([
        ("OSDCloud PE preflight", "cloudosd_preflight", "pe"),
        ("Run OSDCloud workflow", "cloudosd_deploy_os", "pe"),
        ("Stage OSD client", "stage_osd_client", "pe"),
        ("Capture Autopilot hardware hash", "capture_autopilot_hash", "full_os"),
        ("Verify AD domain membership", "verify_ad_domain_join", "full_os"),
    ]):
        ts_engine_pg.add_step(
            pg_conn,
            sequence_id=sequence_id,
            parent_id=None,
            name=name,
            kind=kind,
            phase=phase,
            position=position,
        )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={
            "vmid": 119,
            "computer_name": "GELL-119-AD",
            "serial_number": "GELL-119-AD",
        },
    )

    res = web_client.get("/task-engine")

    assert res.status_code == 200
    body = res.text
    assert "OSDCloud V2 OSD Task Plans" in body
    assert 'class="cloudosd-v2-task-plan"' in body
    assert run_id in body
    assert "VMID 119" in body
    assert "GELL-119-AD" in body
    assert "Stage OSD client" in body
    assert "stage_osd_client" in body
    assert "Capture Autopilot hardware hash" in body
    assert "capture_autopilot_hash" in body
    assert "Verify AD domain membership" in body


def test_task_engine_builder_renders_smart_lanes_and_palette(
    web_client: TestClient, pg_conn
):
    from web import ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="OSDCloud desktop baseline",
        description="Desktop client sequence",
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Capture Autopilot hardware hash",
        kind="capture_autopilot_hash",
        phase="full_os",
        position=0,
        retry_count=2,
    )

    res = web_client.get(f"/task-engine/sequences/{sequence_id}/edit")

    assert res.status_code == 200
    body = res.text
    assert "Smart builder for OSDCloud desktop deployment" in body
    assert "Legacy Import" not in body
    assert "Start from v1 sequence" not in body
    assert "No legacy v1 sequences" not in body
    assert "draftFromLegacy" not in body
    assert "createFromLegacy" not in body
    assert "data-v2-builder" in body
    assert "v2-builder-scroll" in body
    assert "min-width:1440px" in body
    assert "Phase Timeline" in body
    assert "Step Palette" in body
    assert body.index("Phase Timeline") < body.index("Step Palette")
    left_sidebar = body.split('<main class="v2-stack">', 1)[0]
    assert "Step Palette" not in left_sidebar
    assert "Search full catalog" in body
    assert "Pinned OSDCloud Desktop" in body
    assert "PINNED_CLOUDOSD_DESKTOP" in body
    assert "v2-owner-chip" in body
    assert "v2-palette-count" in body
    assert "v2-palette-section-steps" in body
    assert "repeat(auto-fit, minmax(220px, 1fr))" in body
    assert "Add recommended baseline" in body
    assert "capture_autopilot_hash" in body
    for kind in [
        "capture_hash",
        "partition_disk",
        "apply_wim",
        "apply_driver_package",
        "prepare_windows_setup",
        "bake_boot_entry",
        "handoff_to_windows_setup",
        "cloudosd_preflight",
        "cloudosd_deploy_os",
        "stage_ad_domain_join_unattend",
        "verify_ad_domain_join",
        "cloudosd_validate_offline_os",
        "stage_osd_client",
        "stage_autopilot_agent",
        "rename_computer",
        "install_qga",
        "fix_recovery_partition",
        "verify_qga",
        "install_qga_watchdog",
        "handoff_to_oobe",
        "install_package",
        "run_script",
        "install_app",
        "wait_agent_heartbeat",
    ]:
        assert kind in body
    assert "addRecommendedBaseline" in body
    assert 'const STEP_TEMPLATES = [{"kind":' in body
    assert "&#34;kind&#34;" not in body
    assert 'draggable="true"' in body
    assert "dragTemplate(event" in body
    assert "dragStep(event" in body
    assert "startPointerDrag(event" in body
    assert "pointerMoveDrag" in body
    assert "paletteClick(event" in body
    assert "paletteButton(t, pinned)" in body
    assert "data-pinned=\"${pinned ? \"true\" : \"false\"}\"" in body
    assert "dropOnPhase(event" in body
    assert "sourceElement: event.currentTarget" in body
    assert 'drag.sourceElement.dataset.dragging = "false"' in body
    assert "data-enabled=\"${s.enabled !== false}\"" in body
    assert "v2-step-state" in body
    assert "v2-step-card-status" in body
    assert "<br><code>${esc(s.kind)}</code>" not in body
    assert "Drag steps here, or reorder inside this phase." in body
    assert "forEach(kind => addStepFromTemplate(kind));" in body


def test_task_engine_sequence_list_shows_editable_sequences_and_readonly_templates(
    web_client: TestClient, pg_conn
):
    from web import ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    sequence_id = ts_engine_pg.create_sequence(
        pg_conn,
        name="Operator OSDCloud Desktop",
        description="Editable production sequence",
    )
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Capture Autopilot hardware hash",
        kind="capture_autopilot_hash",
        phase="full_os",
        position=0,
    )

    res = web_client.get("/task-engine/sequences/list")

    assert res.status_code == 200
    body = res.text
    assert "V2 Sequence Library" in body
    assert "Operator OSDCloud Desktop" in body
    assert f"/task-engine/sequences/{sequence_id}/edit" in body
    assert "Read-only Flow Templates" in body
    assert "OSDCloud Desktop Client" in body
    assert "OSDCloud Desktop Client + AD Domain Join" in body
    assert "WinPE Desktop WIM Deployment" in body
    assert "WinPE Windows Server WIM Deployment" in body
    assert "Proxmox Clone Desktop from Template" in body
    assert "/task-engine/sequences/templates/cloudosd-desktop" in body
    assert "/task-engine/sequences/new?template_id=cloudosd-desktop" in body
    assert "Clone into builder" in body
    assert "read-only" in body


def test_task_engine_readonly_template_detail_and_clone_into_builder(
    web_client: TestClient, pg_conn
):
    from web import ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)

    detail = web_client.get(
        "/task-engine/sequences/templates/cloudosd-desktop-domain-join"
    )

    assert detail.status_code == 200
    detail_body = detail.text
    assert "OSDCloud Desktop Client + AD Domain Join" in detail_body
    assert "read-only template" in detail_body
    assert "Read-only Step Plan" in detail_body
    assert "stage_ad_domain_join_unattend" in detail_body
    assert "verify_ad_domain_join" in detail_body
    assert (
        "/task-engine/sequences/new?template_id=cloudosd-desktop-domain-join"
        in detail_body
    )

    clone = web_client.get(
        "/task-engine/sequences/new?template_id=cloudosd-desktop-domain-join"
    )

    assert clone.status_code == 200
    clone_body = clone.text
    assert "New v2 task sequence" in clone_body
    assert "Template source" in clone_body
    assert "read-only template cloned" in clone_body
    assert "OSDCloud Desktop Client + AD Domain Join copy" in clone_body
    assert "stage_ad_domain_join_unattend" in clone_body
    assert "verify_ad_domain_join" in clone_body
    assert "wait_agent_heartbeat" in clone_body
    assert "data-v2-builder" in clone_body


def test_task_engine_imports_legacy_sequence_into_v2(
    web_client: TestClient, pg_conn
):
    from web import sequences_db, ts_engine_pg
    from web.app import SEQUENCES_DB

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    legacy_id = sequences_db.create_sequence(
        SEQUENCES_DB,
        name="Legacy AD desktop",
        description="v1 domain join sequence",
        produces_autopilot_hash=True,
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, legacy_id, [
        {
            "step_type": "join_ad_domain",
            "params": {"credential_id": 7, "ou_path": "OU=Workstations,DC=example,DC=com"},
            "enabled": True,
        },
    ])

    res = web_client.post(f"/api/osd/v2/builder/import-legacy/{legacy_id}")

    assert res.status_code == 201
    sequence_id = res.json()["id"]
    nodes = ts_engine_pg.list_sequence_nodes(pg_conn, sequence_id)
    assert [node["kind"] for node in nodes] == [
        "stage_ad_domain_join_unattend",
        "capture_autopilot_hash",
        "verify_ad_domain_join",
    ]
    assert nodes[0]["params"]["ou_path"] == "OU=Workstations,DC=example,DC=com"
