import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from web import auth


def test_react_shell_auth_boundary_is_narrow():
    assert auth.is_exempt_path("/static/react/assets/app.js")
    assert not auth.is_exempt_path("/react-shell")
    assert not auth.is_exempt_path("/react/dashboard")
    assert not auth.is_exempt_path("/react/jobs")
    assert not auth.is_exempt_path("/react/monitoring")
    assert not auth.is_exempt_path("/react/vms")
    assert not auth.is_exempt_path("/react/vms/108")
    assert not auth.is_exempt_path("/react/agent-download")
    assert not auth.is_exempt_path("/react/devices")
    assert not auth.is_exempt_path("/react/legacy-vms")
    assert not auth.is_exempt_path("/react/hashes")
    assert not auth.is_exempt_path("/react/files")
    assert not auth.is_exempt_path("/react/settings")
    assert not auth.is_exempt_path("/react/credentials")
    assert not auth.is_exempt_path("/react/credentials/new")
    assert not auth.is_exempt_path("/react/credentials/7/edit")
    assert not auth.is_exempt_path("/react/monitoring/settings")
    assert not auth.is_exempt_path("/react/install-tracking")
    assert not auth.is_exempt_path("/react/provision")
    assert not auth.is_exempt_path("/react/cloudosd")
    assert not auth.is_exempt_path("/react/cloudosd/runs/run-1")
    assert not auth.is_exempt_path("/react/osdeploy")
    assert not auth.is_exempt_path("/react/osdeploy/runs/run-1")
    assert not auth.is_exempt_path("/react/template")
    assert not auth.is_exempt_path("/react/jobs/job-1")
    assert not auth.is_exempt_path("/react/runs")
    assert not auth.is_exempt_path("/react/runs/1")
    assert not auth.is_exempt_path("/react/task-engine")
    assert not auth.is_exempt_path("/react/task-engine/sequences/list")
    assert not auth.is_exempt_path("/react/task-engine/sequences/new")
    assert not auth.is_exempt_path("/react/task-engine/sequences/templates/windows-baseline")
    assert not auth.is_exempt_path("/react/task-engine/sequences/seq-1/edit")
    assert not auth.is_exempt_path("/react/answer-isos")
    assert not auth.is_exempt_path("/react/sequences")
    assert not auth.is_exempt_path("/react/sequences/new")
    assert not auth.is_exempt_path("/react/sequences/1/edit")
    assert not auth.is_exempt_path("/react/utm-vms")
    assert not auth.is_exempt_path("/api/react/agent-download/bootstrap-token")
    assert not auth.is_exempt_path("/react")
    assert not auth.is_exempt_path("/legacy/dashboard")
    assert not auth.is_exempt_path("/legacy/jobs")
    assert not auth.is_exempt_path("/legacy/vms")
    assert not auth.is_exempt_path("/legacy/cloud")
    assert not auth.is_exempt_path("/legacy/hashes")
    assert not auth.is_exempt_path("/legacy/files")
    assert not auth.is_exempt_path("/legacy/settings")
    assert not auth.is_exempt_path("/legacy/credentials")
    assert not auth.is_exempt_path("/legacy/monitoring/settings")
    assert not auth.is_exempt_path("/legacy/devices/108")
    assert not auth.is_exempt_path("/app")
    assert not auth.is_exempt_path("/app/jobs")
    assert not auth.is_exempt_path("/openapi.json")


@pytest.mark.parametrize("path", [
    "/react-shell",
    "/react/dashboard",
    "/react/jobs",
    "/react/monitoring",
    "/react/vms",
    "/react/vms/108",
    "/react/agent-download",
    "/react/devices",
    "/react/legacy-vms",
    "/react/hashes",
    "/react/files",
    "/react/settings",
    "/react/credentials",
    "/react/credentials/new",
    "/react/credentials/7/edit",
    "/react/monitoring/settings",
    "/react/install-tracking",
    "/react/provision",
    "/react/cloudosd",
    "/react/cloudosd/runs/run-1",
    "/react/osdeploy",
    "/react/osdeploy/runs/run-1",
    "/react/template",
    "/react/jobs/job-1",
    "/react/runs",
    "/react/runs/1",
    "/react/task-engine",
    "/react/task-engine/sequences/list",
    "/react/task-engine/sequences/new",
    "/react/task-engine/sequences/templates/windows-baseline",
    "/react/task-engine/sequences/seq-1/edit",
    "/react/answer-isos",
    "/react/utm-vms",
])
def test_react_shell_routes_render_authenticated_bootstrap(web_client, path):
    response = web_client.get(path)

    assert response.status_code == 200
    assert 'id="react-root"' in response.text
    assert 'data-react-shell="protected"' in response.text
    assert "Proxmox VE Autopilot" in response.text


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/react/sequences", "/react/task-engine/sequences/list"),
        ("/react/sequences/new", "/react/task-engine/sequences/new"),
        ("/react/sequences/1/edit", "/react/task-engine/sequences/list"),
    ],
)
def test_retired_v1_react_sequence_routes_redirect_to_v2(web_client, path, target):
    response = web_client.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == target


@pytest.mark.parametrize("path", ["/auth/login", "/setup"])
def test_public_react_shell_routes_render_public_bootstrap(web_client, path):
    response = web_client.get(path)

    assert response.status_code == 200
    assert 'id="react-root"' in response.text
    assert 'data-react-shell="public"' in response.text
    assert "Proxmox VE Autopilot" in response.text


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/", "/react/dashboard"),
        ("/jobs", "/react/jobs"),
        ("/vms", "/react/vms"),
        ("/cloud", "/react/devices"),
        ("/hashes", "/react/hashes"),
        ("/files", "/react/files"),
        ("/settings", "/react/settings"),
        ("/credentials", "/react/credentials"),
        ("/credentials/new", "/react/credentials/new"),
        ("/credentials/7/edit", "/react/credentials/7/edit"),
        ("/devices/108", "/react/vms/108"),
        ("/monitoring/settings", "/react/monitoring/settings"),
        ("/install-tracking", "/react/install-tracking"),
        ("/provision", "/react/provision"),
        ("/cloudosd", "/react/cloudosd"),
        ("/cloudosd/builder", "/react/cloudosd?view=builder"),
        ("/cloudosd/cache", "/react/cloudosd?view=cache"),
        ("/cloudosd/artifacts", "/react/cloudosd?view=artifacts"),
        ("/osdcloud", "/react/cloudosd"),
        ("/osdcloud/builder", "/react/cloudosd?view=builder"),
        ("/osdcloud/cache", "/react/cloudosd?view=cache"),
        ("/osdcloud/artifacts", "/react/cloudosd?view=artifacts"),
        ("/cloudosd/runs/run-1", "/react/cloudosd/runs/run-1"),
        ("/osdcloud/runs/run-1", "/react/cloudosd/runs/run-1"),
        ("/osdeploy", "/react/osdeploy"),
        ("/osdeploy/builder", "/react/osdeploy?view=builder"),
        ("/osdeploy/cache", "/react/osdeploy?view=cache"),
        ("/osdeploy/artifacts", "/react/osdeploy?view=artifacts"),
        ("/osdeploy/runs/run-1", "/react/osdeploy/runs/run-1"),
        ("/template", "/react/template"),
        ("/jobs/job-1", "/react/jobs/job-1"),
        ("/runs", "/react/runs"),
        ("/runs/1", "/react/runs/1"),
        ("/task-engine", "/react/task-engine"),
        ("/task-engine/sequences/list", "/react/task-engine/sequences/list"),
        ("/task-engine/sequences/new", "/react/task-engine/sequences/new"),
        ("/task-engine/sequences/templates/windows-baseline", "/react/task-engine/sequences/templates/windows-baseline"),
        ("/task-engine/sequences/seq-1/edit", "/react/task-engine/sequences/seq-1/edit"),
        ("/answer-isos", "/react/answer-isos"),
        ("/sequences", "/react/task-engine/sequences/list"),
        ("/sequences/new", "/react/task-engine/sequences/new"),
        ("/sequences/1/edit", "/react/task-engine/sequences/list"),
        ("/monitoring", "/react/monitoring"),
        ("/vms/108/console", "/react/vms/108?action=console"),
    ],
)
def test_primary_operator_paths_redirect_to_react(web_client, path, target):
    response = web_client.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == target


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/legacy/dashboard", "/react/dashboard"),
        ("/legacy/jobs", "/react/jobs"),
        ("/legacy/cloud", "/react/devices"),
        ("/legacy/hashes", "/react/hashes"),
        ("/legacy/files", "/react/files"),
        ("/legacy/settings", "/react/settings"),
        ("/legacy/credentials", "/react/credentials"),
        ("/legacy/credentials/new", "/react/credentials/new"),
        ("/legacy/credentials/7/edit", "/react/credentials/7/edit"),
        ("/legacy/vms", "/react/legacy-vms"),
        ("/legacy/devices/108", "/react/vms/108"),
        ("/legacy/monitoring/settings", "/react/monitoring/settings"),
    ],
)
def test_legacy_operator_pages_redirect_to_react(web_client, path, target):
    response = web_client.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == target


def test_remaining_react_read_apis_return_stable_shapes(web_client):
    cloud = web_client.get("/api/cloud/devices")
    assert cloud.status_code == 200
    assert set(cloud.json()) >= {"groups", "unmatched", "meta", "windows_only", "deletions"}

    hashes = web_client.get("/api/hashes")
    assert hashes.status_code == 200
    assert set(hashes.json()) >= {"hash_files"}

    files = web_client.get("/api/files")
    assert files.status_code == 200
    assert set(files.json()) >= {"files"}

    settings = web_client.get("/api/settings")
    assert settings.status_code == 200
    assert set(settings.json()) >= {"sections", "hypervisor_type", "proxmox_bootstrap"}

    monitoring = web_client.get("/api/monitoring/settings/full")
    assert monitoring.status_code == 200
    assert set(monitoring.json()) >= {"settings", "search_ous", "domain_creds", "keytab"}


@pytest.mark.parametrize(
    ("path", "keys"),
    [
        ("/api/install-tracking/page", {"tracking"}),
        ("/api/provision/page", {"defaults", "sequences", "cloudosd_catalog", "osdeploy_catalog"}),
        ("/api/cloudosd/page", {"runs", "artifacts", "cloudosd_cache", "view"}),
        ("/api/osdeploy/page", {"runs", "artifacts", "osdeploy_cache", "view"}),
        ("/api/template/page", {"profiles", "ubuntu_sequences", "hypervisor_type", "utm_iso_dir"}),
        ("/api/jobs/missing-job/page", {"job", "log", "stream_url"}),
        ("/api/runs/page", {"runs"}),
        ("/api/task-engine/page", {"sequences", "runs", "flow_templates"}),
        ("/api/answer-isos/page", {"rows", "error"}),
        ("/api/utm-vms/page", {"vms", "host_summary", "isos"}),
    ],
)
def test_remaining_react_page_payload_apis_return_stable_shapes(web_client, path, keys):
    response = web_client.get(path)

    assert response.status_code == 200
    assert set(response.json()) >= keys


def test_react_agent_download_bootstrap_token_returns_configured_proof(web_client, monkeypatch, tmp_path):
    from web import app as web_app

    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "fleet-bootstrap-token").write_text("fleet-token-123", encoding="utf-8")
    monkeypatch.setattr(web_app, "SECRETS_DIR", secret_dir)

    response = web_client.get("/api/react/agent-download/bootstrap-token")

    assert response.status_code == 200
    body = response.json()
    assert body["token_kind"] == "sha256_proof"
    assert body["bootstrap_token"] == "d20bc64cba4139ea51d02624d775cc28c27c937021a92464c3171438ac7ce6b0"
    assert body["bootstrap_token"] != "fleet-token-123"


def test_react_vms_fleet_api_response_shape(web_client, monkeypatch):
    from web import app as web_app, db_pg, lab_bubbles_pg

    async def fake_vms_payload():
        return ({
            "data": [{
                "vmid": 108,
                "name": "WrkGrp-525570B6",
                "hostname": "WRKGRP-525570B6",
                "serial": "WrkGrp-525570B6",
                "status": "running",
                "ip_address": "192.168.2.49",
                "in_intune": False,
                "in_autopilot": False,
                "aad_joined": True,
                "part_of_domain": False,
            }],
            "devices": ([{
                "id": "device-1",
                "serial": "WrkGrp-525570B6",
                "display_name": "",
                "group_tag": "Lab",
                "profile_status": "assigned",
                "profile_ok": True,
                "enrollment_state": "enrolled",
                "manufacturer": "Proxmox",
                "model": "QEMU",
                "last_contact": "2026-05-19T00:00:00Z",
            }], ""),
            "hash_serials": {"WrkGrp-525570B6"},
            "fetched_at": 1.0,
            "refreshing": False,
        }, 12.4)

    monkeypatch.setattr(web_app, "_get_vms_payload", fake_vms_payload)
    monkeypatch.setattr(web_app, "_proxmox_api", lambda path: [
        {"type": "qemu", "vmid": 108, "name": "WrkGrp-525570B6", "status": "running", "node": "pve2"},
        {"type": "qemu", "vmid": 130, "name": "ACME-DC01", "status": "running", "node": "pve2"},
        {"type": "qemu", "vmid": 400, "name": "Dev1", "status": "stopped", "node": "pve1"},
        {"type": "lxc", "vmid": 500, "name": "autopilot-docker", "status": "running", "node": "pve2"},
    ] if path == "/cluster/resources?type=vm" else [])
    monkeypatch.setattr(web_app, "_latest_monitor_sweep_status", lambda: {"running": False, "vm_count": 1})
    monkeypatch.setattr(web_app, "_agent_inventory_rows", lambda: [{
        "agent_id": "agent-wrkgrp-525570b6",
        "approval_status": "active",
        "vmid": 108,
        "computer_name": "WRKGRP-525570B6",
        "primary_ipv4": "192.168.2.49",
        "qga_state": "Running",
        "last_heartbeat_at": "2026-05-19T00:00:00Z",
        "hash_capture_supported": True,
    }])
    monkeypatch.setattr(web_app.machine_lifecycle_pg, "current_by_vmids", lambda _vmids: {
        108: {
            "state": "workgroup_unenrolled",
            "label": "unenrolled",
            "source": "agent_heartbeat",
            "last_observed_at": "2026-05-19T00:00:00Z",
            "domain_joined": False,
            "entra_joined": False,
            "intune_enrolled": False,
            "autopilot_registered": True,
        }
    })
    monkeypatch.setattr(web_app.sequences_db, "get_vm_provisioning", lambda _path, vmid: None)
    with db_pg.connection(web_app._database_url()) as conn:
        lab_bubbles_pg.reset_for_tests(conn)
        lab_bubbles_pg.init(conn)
        bubble = lab_bubbles_pg.create_bubble(
            conn,
            name="ACME Lab",
            domain_name="lab.gell.one",
            cidr="10.42.12.0/24",
            dhcp_scope="10.42.12.0",
        )
        workstation = lab_bubbles_pg.add_asset(
            conn,
            bubble["id"],
            asset_type="vm",
            asset_role="workstation",
            vmid=108,
            membership_state="active",
        )
        dc = lab_bubbles_pg.add_asset(
            conn,
            bubble["id"],
            asset_type="vm",
            asset_role="domain_controller",
            vmid=130,
            agent_id="dc01-agent",
            membership_state="active",
        )
        lab_bubbles_pg.add_service(
            conn,
            bubble["id"],
            service_kind="entra",
            service_name="Entra ID",
            scope="external",
            provider_asset_id=dc["id"],
            readiness_state="ready",
        )
        assert workstation["vmid"] == 108

    response = web_client.get("/api/vms/fleet")

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "vms",
        "missing_vms",
        "agents",
        "autopilot_devices",
        "bubble_topology",
        "ap_error",
        "cache_age_seconds",
        "cache_refreshing",
        "monitor_sweep",
        "generated_at",
    }
    assert body["vms"][0]["vmid"] == 108
    assert body["vms"][0]["in_autopilot"] is True
    assert body["vms"][0]["has_hash"] is True
    assert body["vms"][0]["lifecycle_state"] == "workgroup_unenrolled"
    assert body["vms"][0]["lifecycle_label"] == "unenrolled"
    assert body["vms"][0]["lifecycle_autopilot_registered"] is True
    assert [vm["vmid"] for vm in body["proxmox_vms"]] == [108, 130, 400]
    assert body["proxmox_vms"][2]["node"] == "pve1"
    assert body["agents"][0]["agent_id"] == "agent-wrkgrp-525570b6"
    assert body["autopilot_devices"][0]["display_name"] == "WRKGRP-525570B6"
    assert body["bubble_topology"]["workstation_fleets"][0]["bubble"]["name"] == "ACME Lab"
    assert body["bubble_topology"]["workstation_fleets"][0]["workstation_count"] == 1
    assert body["bubble_topology"]["critical_infrastructure"][0]["role"] == "domain_controller"
    assert body["bubble_topology"]["critical_infrastructure"][0]["vm"]["name"] == "ACME-DC01"
    assert body["bubble_topology"]["connected_services"][0]["service_name"] == "Entra ID"


def test_react_vms_fleet_purges_agents_without_current_vm(web_client, monkeypatch, tmp_path):
    from web import app as web_app

    deleted: list[str] = []
    setup_state_path = tmp_path / "foundation_state.json"
    setup_state_path.write_text(
        json.dumps({
            "build_host_expected_agent_id": "buildhost-100",
            "build_host_vmid": "100",
        }),
        encoding="utf-8",
    )

    async def fake_vms_payload():
        return ({
            "data": [{
                "vmid": 108,
                "name": "WrkGrp-525570B6",
                "hostname": "WRKGRP-525570B6",
                "serial": "WrkGrp-525570B6",
                "status": "running",
                "ip_address": "192.168.2.49",
            }],
            "devices": ([], ""),
            "hash_serials": set(),
            "fetched_at": 1.0,
            "refreshing": False,
        }, 0.0)

    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", setup_state_path)
    monkeypatch.setattr(web_app, "_get_vms_payload", fake_vms_payload)
    monkeypatch.setattr(web_app, "_proxmox_cluster_vm_rows", lambda: [])
    monkeypatch.setattr(web_app, "_latest_monitor_sweep_status", lambda: {"running": False, "vm_count": 1})
    monkeypatch.setattr(web_app, "_hard_delete_agent_by_id", lambda agent_id: deleted.append(agent_id) or True)
    monkeypatch.setattr(web_app.machine_lifecycle_pg, "current_by_vmids", lambda _vmids: {})
    monkeypatch.setattr(web_app.sequences_db, "get_vm_provisioning", lambda _path, vmid: None)
    monkeypatch.setattr(web_app, "_agent_inventory_rows", lambda: [
        {
            "agent_id": "agent-attached",
            "approval_status": "active",
            "vmid": 108,
            "computer_name": "WRKGRP-525570B6",
        },
        {
            "agent_id": "agent-no-vm",
            "approval_status": "active",
            "computer_name": "AGENT-ONLY",
        },
        {
            "agent_id": "buildhost-100",
            "approval_status": "active",
            "vmid": 100,
            "computer_name": "AUTOPILOT-BLD",
        },
        {
            "agent_id": "agent-deleted-vm",
            "approval_status": "active",
            "vmid": 999,
            "computer_name": "OLD-VM",
        },
    ])

    response = web_client.get("/api/vms/fleet")

    assert response.status_code == 200
    body = response.json()
    assert [agent["agent_id"] for agent in body["agents"]] == [
        "agent-attached",
        "buildhost-100",
    ]
    assert deleted == ["agent-no-vm", "agent-deleted-vm"]


def test_react_vms_fleet_keeps_pending_approval_without_current_vm(web_client, monkeypatch, tmp_path):
    from web import app as web_app

    deleted: list[str] = []
    setup_state_path = tmp_path / "foundation_state.json"
    setup_state_path.write_text("{}", encoding="utf-8")

    async def fake_vms_payload():
        return ({
            "data": [],
            "devices": ([], ""),
            "hash_serials": set(),
            "fetched_at": 1.0,
            "refreshing": False,
        }, 0.0)

    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", setup_state_path)
    monkeypatch.setattr(web_app, "_get_vms_payload", fake_vms_payload)
    monkeypatch.setattr(web_app, "_latest_monitor_sweep_status", lambda: {"running": False, "vm_count": 0})
    monkeypatch.setattr(web_app, "_hard_delete_agent_by_id", lambda agent_id: deleted.append(agent_id) or True)
    monkeypatch.setattr(web_app.machine_lifecycle_pg, "current_by_vmids", lambda _vmids: {})
    monkeypatch.setattr(web_app.sequences_db, "get_vm_provisioning", lambda _path, vmid: None)
    monkeypatch.setattr(web_app, "_agent_inventory_rows", lambda: [
        {
            "agent_id": "agent-vm-110",
            "approval_id": "approval-110",
            "approval_status": "pending",
            "vmid": 110,
            "computer_name": "DNS3",
        },
    ])

    response = web_client.get("/api/vms/fleet")

    assert response.status_code == 200
    body = response.json()
    assert [agent["agent_id"] for agent in body["agents"]] == ["agent-vm-110"]
    assert deleted == []


def test_react_vms_fleet_keeps_run_scoped_agent_during_vm_cache_lag(web_client, monkeypatch, tmp_path):
    from web import app as web_app

    deleted: list[str] = []
    setup_state_path = tmp_path / "foundation_state.json"
    setup_state_path.write_text("{}", encoding="utf-8")

    async def fake_vms_payload():
        return ({
            "data": [{
                "vmid": 108,
                "name": "WRKGRP-525570B6",
                "hostname": "WRKGRP-525570B6",
                "serial": "WRKGRP-525570B6",
                "status": "running",
                "ip_address": "192.168.2.49",
            }],
            "devices": ([], ""),
            "hash_serials": set(),
            "fetched_at": 1.0,
            "refreshing": False,
        }, 0.0)

    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", setup_state_path)
    monkeypatch.setattr(web_app, "_get_vms_payload", fake_vms_payload)
    monkeypatch.setattr(web_app, "_proxmox_cluster_vm_rows", lambda: [])
    monkeypatch.setattr(web_app, "_latest_monitor_sweep_status", lambda: {"running": False, "vm_count": 1})
    monkeypatch.setattr(web_app, "_hard_delete_agent_by_id", lambda agent_id: deleted.append(agent_id) or True)
    monkeypatch.setattr(web_app.machine_lifecycle_pg, "current_by_vmids", lambda _vmids: {})
    monkeypatch.setattr(web_app.sequences_db, "get_vm_provisioning", lambda _path, vmid: None)
    monkeypatch.setattr(web_app, "_agent_inventory_rows", lambda: [
        {
            "agent_id": "agent-e2e40-wk-04",
            "approval_status": "active",
            "vmid": 128,
            "computer_name": "E2E40-WK-04",
            "current_run_id": "490dbcb1-cd33-4b0c-b4e3-3a6fa5c0f8de",
            "last_seen_at": "2026-05-26T09:00:00+00:00",
        },
    ])

    response = web_client.get("/api/vms/fleet")

    assert response.status_code == 200
    body = response.json()
    assert [agent["agent_id"] for agent in body["agents"]] == ["agent-e2e40-wk-04"]
    assert deleted == []


def test_agent_inventory_uses_device_run_id_before_first_heartbeat(monkeypatch):
    from web import app as web_app

    monkeypatch.setitem(web_app._VMS_CACHE, "data", [])
    monkeypatch.setattr(web_app, "_latest_agent_release_for_inventory", lambda: None)
    monkeypatch.setattr(web_app.agent_telemetry_pg, "latest_agents", lambda: [
        {
            "agent_id": "agent-e2e40-wk-04",
            "created_from_run_id": "490dbcb1-cd33-4b0c-b4e3-3a6fa5c0f8de",
            "device_vmid": 128,
            "device_computer_name": "E2E40-WK-04",
            "device_agent_version": "1.0.0",
        },
    ])
    monkeypatch.setattr(web_app.agent_telemetry_pg, "pending_bootstrap_approvals", lambda: [])
    monkeypatch.setattr(web_app.machine_lifecycle_pg, "current_by_agents", lambda _agent_ids: {})

    rows = web_app._agent_inventory_rows()

    assert rows[0]["current_run_id"] == "490dbcb1-cd33-4b0c-b4e3-3a6fa5c0f8de"


def test_vm_power_endpoint_returns_json_for_react_callers(web_client, monkeypatch):
    from web import app as web_app

    posted = []
    monkeypatch.setattr(web_app, "_resolve_vm_node", lambda vmid: "pve2")
    monkeypatch.setattr(web_app, "_proxmox_api_post", lambda path, data=None: posted.append((path, data)))

    response = web_client.post("/api/vms/108/start", headers={"Accept": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "vmid": 108, "action": "start"}
    assert posted == [("/nodes/pve2/qemu/108/status/start", None)]


def test_react_read_api_response_shapes(web_client):
    jobs = web_client.get("/api/jobs")
    assert jobs.status_code == 200
    assert isinstance(jobs.json(), list)

    running = web_client.get("/api/jobs/running")
    assert running.status_code == 200
    assert set(running.json()) >= {"running", "running_count", "queued_count"}
    assert isinstance(running.json()["running"], list)

    recent = web_client.get("/api/jobs/recent?limit=5")
    assert recent.status_code == 200
    assert set(recent.json()) >= {"jobs"}
    assert isinstance(recent.json()["jobs"], list)

    services = web_client.get("/api/services")
    assert services.status_code == 200
    assert set(services.json()) >= {"services", "available"}
    assert isinstance(services.json()["services"], list)

    fleet = web_client.get("/api/fleet/summary")
    assert fleet.status_code == 200
    assert "total" in fleet.json()

    summary = web_client.get("/api/cockpit/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert set(body) >= {
        "readiness_score",
        "jobs",
        "recent_jobs",
        "services",
        "fleet",
        "monitoring",
    }
    assert set(body["jobs"]) >= {"running", "running_count", "queued_count"}


def test_live_jobs_payload_contract(web_client):
    from web import app as web_app

    payload = web_app._live_jobs_payload()

    assert set(payload) >= {"running", "recent", "table", "generated_at"}
    assert set(payload["running"]) >= {"running", "running_count", "queued_count"}
    assert "jobs" in payload["recent"]
    assert "jobs" in payload["table"]


def test_observe_monitoring_api_response_shapes(web_client):
    runtime = web_client.get("/api/monitoring/runtime-services")
    assert runtime.status_code == 200
    runtime_body = runtime.json()
    assert set(runtime_body) >= {"available", "error", "containers"}
    assert isinstance(runtime_body["containers"], list)

    deployments = web_client.get("/api/monitoring/deployments/summary")
    assert deployments.status_code == 200
    assert set(deployments.json()) >= {"total", "running", "succeeded", "failed"}

    signals = web_client.get("/api/monitoring/signals")
    assert signals.status_code == 200
    signals_body = signals.json()
    assert set(signals_body) >= {
        "generated_at",
        "build",
        "metrics",
        "signals",
        "operator_paths",
        "lifecycle_lanes",
        "deployment_health",
        "services",
        "runtime",
        "fleet_attention",
    }
    assert isinstance(signals_body["metrics"], list)
    assert isinstance(signals_body["signals"], list)
    assert isinstance(signals_body["operator_paths"], list)
    assert isinstance(signals_body["lifecycle_lanes"], list)
    assert set(signals_body["deployment_health"]) >= {"summary", "active", "recent_completions", "bottlenecks"}
    assert isinstance(signals_body["services"], list)
    assert set(signals_body["runtime"]) >= {"available", "error", "containers"}
    assert isinstance(signals_body["fleet_attention"], list)
    assert {
        "runtime",
        "service_health",
        "jobs",
        "build_host",
        "artifacts",
        "deploy_readiness",
        "deployment_speed",
        "agent",
        "lifecycle",
        "identity",
        "fleet_evidence",
    }.issubset({item["family"] for item in signals_body["signals"]})
    assert any(path["href"].startswith(("/react/", "/cloudosd", "/osdeploy", "/setup", "/vms", "/devices", "/hashes")) for path in signals_body["operator_paths"])
    assert "Open legacy monitoring" not in {path.get("action_label") for path in signals_body["operator_paths"]}


def test_openapi_export_script_uses_local_app_import(tmp_path):
    output = tmp_path / "openapi.json"
    env = os.environ.copy()
    env["AUTOPILOT_AUTH_BYPASS"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/export_openapi_schema.py",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    schema = json.loads(output.read_text(encoding="utf-8"))
    assert schema["info"]["title"] == "Proxmox VE Autopilot"
    assert "/api/version" in schema["paths"]
