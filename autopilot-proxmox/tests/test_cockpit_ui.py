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

    res = web_client.get("/legacy/dashboard", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/dashboard"

    res = web_client.get("/react/dashboard")
    assert res.status_code == 200
    assert 'id="react-root"' in res.text
    assert 'data-react-shell="protected"' in res.text


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


def test_cloudosd_run_detail_shows_local_admin_credentials():
    template = (
        Path(__file__).resolve().parents[1]
        / "web/templates/cloudosd_run_detail.html"
    ).read_text(encoding="utf-8")

    assert "Local credentials" in template
    assert 'data-cloudosd-field="local_admin_username"' in template
    assert 'data-cloudosd-field="local_admin_password"' in template
    assert "renderLocalAdmin" in template


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

    res = web_client.get("/react/dashboard")
    assert res.status_code == 200
    assert 'id="react-root"' in res.text
    assert 'data-react-shell="protected"' in res.text


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

    res = web_client.get("/legacy/cloud", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/devices"

    res = web_client.get("/api/cloud/devices")
    assert res.status_code == 200
    assert set(res.json()) >= {"groups", "unmatched", "meta", "deletions"}


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
    assert 'id="react-root"' in res.text
    assert 'data-react-shell="public"' in res.text


def test_console_page_preserves_existing_api_contracts(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_proxmox_api", lambda path: {"name": "SERIAL-001"})
    res = web_client.get("/vms/101/console", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/vms/101?action=console"


def test_vms_agent_heartbeat_uses_local_timezone_markup(web_client: TestClient, monkeypatch):
    from web import app as web_app

    async def fake_vms_payload():
        return {
            "data": [{
                "vmid": 106,
                "name": "GELL-E9C0C757",
                "status": "running",
                "serial": "",
                "hostname": "GELL-E9C0C757",
            }],
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

    res = web_client.get("/api/vms/fleet")

    assert res.status_code == 200
    agents = {agent["agent_id"]: agent for agent in res.json()["agents"]}
    assert agents["agent-timezone-test"]["last_heartbeat_at"] == "2026-05-09T00:45:38+00:00"


def test_console_resolves_actual_vm_node_for_serial(web_client: TestClient, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {"proxmox_node": "pve2"})

    def fake_proxmox_api(path):
        if path == "/cluster/resources?type=vm":
            return [{"type": "qemu", "vmid": 100, "node": "pve1"}]
        assert path == "/nodes/pve1/qemu/100/config"
        return {"name": "SERIAL-100"}

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)

    res = web_client.get("/vms/100/console", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"] == "/react/vms/100?action=console"


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

    res = web_client.get("/react/dashboard")
    assert res.status_code == 200
    assert 'id="react-root"' in res.text
    assert 'data-react-shell="protected"' in res.text


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

    res = web_client.get("/runs", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/runs"

    res = web_client.get("/api/runs/page")

    assert res.status_code == 200
    runs = {row["id"]: row for row in res.json()["runs"]}
    assert runs[run_id]["sequence_name"] == "WinPE OSD Demo"
    assert runs[run_id]["state"] == "awaiting_winpe"
    assert runs[run_id]["vmid"] == 119


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

    res = web_client.get(f"/runs/{run_id}", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == f"/react/runs/{run_id}"

    res = web_client.get(f"/api/runs/{run_id}/page")

    assert res.status_code == 200
    body = res.json()
    assert body["run"]["sequence_name"] == "Hash in WinPE"
    assert {
        step.get("name") or step.get("step_type") or step.get("kind")
        for step in body["steps"]
    } >= {
        "capture_hash",
        "partition_disk",
        "apply_wim",
        "handoff_to_windows_setup",
    }


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

    res = web_client.get("/sequences", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/sequences"

    res = web_client.get("/api/sequences/page")

    assert res.status_code == 200
    rows = {row["name"]: row for row in res.json()["sequences"]}
    assert rows["WinPE capable sequence"]["produces_autopilot_hash"] is True
    assert rows["WinPE capable sequence"]["hash_capture_phase"] == "winpe"


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

    res = web_client.get("/task-engine", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/task-engine"

    res = web_client.get("/api/task-engine/page")

    assert res.status_code == 200
    body = res.json()
    sequences = {row["id"]: row for row in body["sequences"]}
    runs = {row["id"]: row for row in body["runs"]}
    assert sequences[sequence_id]["name"] == "Install Apps v2"
    assert sequences[sequence_id]["steps"][0]["name"] == "Install Notepad++"
    assert sequences[sequence_id]["steps"][0]["kind"] == "install_package"
    assert runs[run_id]["state"] == "queued"
    assert body["content_items"][0]["name"] == "notepad-plus-plus"


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

    res = web_client.get("/api/task-engine/page")

    assert res.status_code == 200
    body = res.json()
    runs = {row["id"]: row for row in body["cloudosd_runs"]}
    assert run_id in runs
    sequence = next(row for row in body["sequences"] if row["id"] == sequence_id)
    assert {step["kind"] for step in sequence["steps"]} >= {
        "stage_osd_client",
        "capture_autopilot_hash",
        "verify_ad_domain_join",
    }


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

    res = web_client.get(f"/task-engine/sequences/{sequence_id}/edit", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == f"/react/task-engine/sequences/{sequence_id}/edit"

    res = web_client.get(f"/api/task-engine/sequences/{sequence_id}/edit/page")

    assert res.status_code == 200
    body = res.json()
    assert body["sequence"]["name"] == "OSDCloud desktop baseline"
    assert body["nodes"][0]["kind"] == "capture_autopilot_hash"
    assert body["step_templates"]
    assert body["flow_templates"]
    template_kinds = {template["kind"] for template in body["step_templates"]}
    assert "capture_autopilot_hash" in template_kinds
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
        assert kind in template_kinds


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

    res = web_client.get("/task-engine/sequences/list", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/react/task-engine/sequences/list"

    res = web_client.get("/api/task-engine/sequences/list/page")

    assert res.status_code == 200
    body = res.json()
    assert any(row["name"] == "Operator OSDCloud Desktop" for row in body["sequences"])
    template_names = {template["name"] for template in body["flow_templates"]}
    assert "OSDCloud Desktop Client" in template_names
    assert "OSDCloud Desktop Client + AD Domain Join" in template_names
    assert "WinPE Desktop WIM Deployment" in template_names
    assert "WinPE Windows Server WIM Deployment" in template_names
    assert "Proxmox Clone Desktop from Template" in template_names


def test_task_engine_readonly_template_detail_and_clone_into_builder(
    web_client: TestClient, pg_conn
):
    from web import ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)

    detail = web_client.get(
        "/task-engine/sequences/templates/cloudosd-desktop-domain-join",
        follow_redirects=False,
    )

    assert detail.status_code == 302
    assert detail.headers["location"] == "/react/task-engine/sequences/templates/cloudosd-desktop-domain-join"

    detail = web_client.get(
        "/api/task-engine/sequences/templates/cloudosd-desktop-domain-join/page"
    )
    assert detail.status_code == 200
    template = detail.json()["template"]
    assert template["name"] == "OSDCloud Desktop Client + AD Domain Join"
    assert {node["kind"] for node in template["nodes"]} >= {
        "stage_ad_domain_join_unattend",
        "verify_ad_domain_join",
    }

    clone = web_client.get(
        "/task-engine/sequences/new?template_id=cloudosd-desktop-domain-join",
        follow_redirects=False,
    )

    assert clone.status_code == 302
    assert clone.headers["location"] == "/react/task-engine/sequences/new?template_id=cloudosd-desktop-domain-join"

    clone = web_client.get(
        "/api/task-engine/sequences/new/page?template_id=cloudosd-desktop-domain-join"
    )
    assert clone.status_code == 200
    clone_body = clone.json()
    assert clone_body["template_source"]["name"] == "OSDCloud Desktop Client + AD Domain Join"
    assert clone_body["sequence"]["name"] == "OSDCloud Desktop Client + AD Domain Join copy"
    assert {node["kind"] for node in clone_body["nodes"]} >= {
        "stage_ad_domain_join_unattend",
        "verify_ad_domain_join",
        "wait_agent_heartbeat",
    }


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


def test_bubble_api_create_assets_services_and_audit(web_client: TestClient, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)

    created = web_client.post(
        "/api/bubbles",
        json={
            "name": "ACME Lab",
            "domain_name": "lab.acme.test",
            "netbios_name": "ACME",
            "cidr": "10.42.12.0/24",
            "gateway_ip": "10.42.12.1",
        },
    )
    assert created.status_code == 201
    bubble_id = created.json()["id"]

    asset = web_client.post(
        f"/api/bubbles/{bubble_id}/assets",
        json={
            "asset_type": "vm",
            "asset_role": "domain_controller",
            "vmid": 130,
            "agent_id": "dc01",
        },
    )
    assert asset.status_code == 201
    asset_id = asset.json()["id"]

    service = web_client.post(
        f"/api/bubbles/{bubble_id}/services",
        json={
            "service_kind": "dhcp",
            "service_name": "ACME DHCP",
            "scope": "bubble_local",
            "provider_asset_id": asset_id,
        },
    )
    assert service.status_code == 201
    service_id = service.json()["id"]

    patched_service = web_client.patch(
        f"/api/bubbles/{bubble_id}/services/{service_id}",
        json={"readiness_state": "ready", "evidence_summary": {"leases": 3, "credential_ids": [7]}},
    )
    assert patched_service.status_code == 200
    assert patched_service.json()["readiness_state"] == "ready"
    assert patched_service.json()["evidence_summary"]["credential_ids"] == [7]

    patched_asset = web_client.patch(
        f"/api/bubbles/{bubble_id}/assets/{asset_id}",
        json={"evidence_state": "confirmed", "notes": "agent matched"},
    )
    assert patched_asset.status_code == 200
    assert patched_asset.json()["evidence_state"] == "confirmed"

    moved = web_client.post(
        f"/api/bubbles/{bubble_id}/assets/{asset_id}/move",
        json={"target_bubble_id": bubble_id, "reason": "self move audit check"},
    )
    assert moved.status_code == 200

    readiness = web_client.get(f"/api/bubbles/{bubble_id}/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["bubble"]["id"] == bubble_id

    audit = web_client.get(f"/api/bubbles/{bubble_id}/audit-events")
    assert audit.status_code == 200
    assert any(row["action"] == "asset_added" for row in audit.json()["events"])
    assert any(row["action"] == "asset_moved" for row in audit.json()["events"])

    deleted_service = web_client.delete(f"/api/bubbles/{bubble_id}/services/{service_id}")
    assert deleted_service.status_code == 204
    assert web_client.get(f"/api/bubbles/{bubble_id}/services").json()["services"] == []
    audit_after_delete = web_client.get(f"/api/bubbles/{bubble_id}/audit-events")
    assert any(row["action"] == "service_deleted" for row in audit_after_delete.json()["events"])


def test_bubble_api_rejects_cross_bubble_asset_and_service_mutation(
    web_client: TestClient,
    pg_conn,
):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    source = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    target = lab_bubbles_pg.create_bubble(pg_conn, name="Contoso Lab")
    source_asset = lab_bubbles_pg.add_asset(
        pg_conn,
        source["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
    )
    target_asset = lab_bubbles_pg.add_asset(
        pg_conn,
        target["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=230,
    )
    source_service = lab_bubbles_pg.add_service(
        pg_conn,
        source["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=source_asset["id"],
    )

    wrong_asset_patch = web_client.patch(
        f"/api/bubbles/{target['id']}/assets/{source_asset['id']}",
        json={"notes": "wrong bubble"},
    )
    assert wrong_asset_patch.status_code == 404
    assert lab_bubbles_pg.list_assets(pg_conn, source["id"])[0]["notes"] == ""

    wrong_asset_move = web_client.post(
        f"/api/bubbles/{target['id']}/assets/{source_asset['id']}/move",
        json={"target_bubble_id": target["id"], "reason": "wrong source"},
    )
    assert wrong_asset_move.status_code == 404
    assert lab_bubbles_pg.list_assets(pg_conn, source["id"])[0]["bubble_id"] == source["id"]

    wrong_service_patch = web_client.patch(
        f"/api/bubbles/{target['id']}/services/{source_service['id']}",
        json={"readiness_state": "ready"},
    )
    assert wrong_service_patch.status_code == 404
    assert lab_bubbles_pg.list_services(pg_conn, source["id"])[0]["readiness_state"] == "unknown"

    wrong_service_delete = web_client.delete(
        f"/api/bubbles/{target['id']}/services/{source_service['id']}",
    )
    assert wrong_service_delete.status_code == 404
    assert len(lab_bubbles_pg.list_services(pg_conn, source["id"])) == 1

    wrong_provider = web_client.post(
        f"/api/bubbles/{target['id']}/services",
        json={
            "service_kind": "dns",
            "service_name": "Wrong DNS",
            "provider_asset_id": source_asset["id"],
        },
    )
    assert wrong_provider.status_code == 400

    wrong_provider_patch = web_client.patch(
        f"/api/bubbles/{source['id']}/services/{source_service['id']}",
        json={"provider_asset_id": target_asset["id"]},
    )
    assert wrong_provider_patch.status_code == 400


def test_bubble_api_returns_404_for_missing_nested_bubble(web_client: TestClient, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)

    missing_bubble = "00000000-0000-0000-0000-000000000001"
    assert web_client.delete(f"/api/bubbles/{missing_bubble}").status_code == 404
    assert web_client.get(f"/api/bubbles/{missing_bubble}/assets").status_code == 404
    assert web_client.get(f"/api/bubbles/{missing_bubble}/services").status_code == 404
    assert web_client.get(f"/api/bubbles/{missing_bubble}/audit-events").status_code == 404


def test_bubble_api_delete_removes_bubble_assets_and_services(web_client: TestClient, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    asset = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
    )
    lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=asset["id"],
    )

    deleted = web_client.delete(f"/api/bubbles/{bubble['id']}")

    assert deleted.status_code == 204
    assert lab_bubbles_pg.get_bubble(pg_conn, bubble["id"]) is None
    assert lab_bubbles_pg.list_assets(pg_conn, bubble["id"]) == []


def test_bubble_api_patch_nulls_are_explicit(web_client: TestClient, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(
        pg_conn,
        name="ACME Lab",
        planned_vlan=42,
    )
    asset = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
        agent_id="dc01",
    )
    service = lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=asset["id"],
    )

    cleared_bubble = web_client.patch(
        f"/api/bubbles/{bubble['id']}",
        json={"planned_vlan": None},
    )
    assert cleared_bubble.status_code == 200
    assert cleared_bubble.json()["planned_vlan"] is None

    cleared_asset = web_client.patch(
        f"/api/bubbles/{bubble['id']}/assets/{asset['id']}",
        json={"vmid": None, "agent_id": None},
    )
    assert cleared_asset.status_code == 200
    assert cleared_asset.json()["vmid"] is None
    assert cleared_asset.json()["agent_id"] is None

    cleared_service = web_client.patch(
        f"/api/bubbles/{bubble['id']}/services/{service['id']}",
        json={"provider_asset_id": None},
    )
    assert cleared_service.status_code == 200
    assert cleared_service.json()["provider_asset_id"] is None

    bad_asset_null = web_client.patch(
        f"/api/bubbles/{bubble['id']}/assets/{asset['id']}",
        json={"asset_role": None},
    )
    assert bad_asset_null.status_code == 400
    assert "asset_role" in bad_asset_null.json()["detail"]

    bad_service_null = web_client.patch(
        f"/api/bubbles/{bubble['id']}/services/{service['id']}",
        json={"service_name": None},
    )
    assert bad_service_null.status_code == 400
    assert "service_name" in bad_service_null.json()["detail"]
