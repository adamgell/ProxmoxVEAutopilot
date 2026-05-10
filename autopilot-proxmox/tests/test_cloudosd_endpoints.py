from __future__ import annotations

import shutil

import pytest
from pathlib import Path

from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed CloudOSD endpoint tests",
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def cloudosd_client(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, cloudosd_pg, ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn)
    cloudosd_pg.init(pg_conn)
    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://autopilot.test:5000")

    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
        }
        if path not in values:
            raise RuntimeError(path)
        return values[path]

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)

    return TestClient(web_app.app)


def _create_artifact(pg_conn, **overrides):
    from web import cloudosd_pg

    values = {
        "architecture": "amd64",
        "osdcloud_module_version": "26.4.17.1",
        "build_sha": "cloudosdtest",
        "iso_path": "/app/output/cloudosd-autopilot-amd64-cloudosdtest.iso",
        "wim_path": "/app/output/cloudosd-autopilot-amd64-cloudosdtest.wim",
        "manifest_path": "/app/output/cloudosd-autopilot-amd64-cloudosdtest.json",
        "iso_sha256": "a" * 64,
        "wim_sha256": "b" * 64,
        "built_by_host": "Adam.Gell@10.211.55.6",
        "proxmox_volid": "local:iso/cloudosd-autopilot-amd64-cloudosdtest.iso",
    }
    values.update(overrides)
    return cloudosd_pg.create_artifact(pg_conn, **values)


def _run_payload(artifact_id: str, **overrides):
    values = {
        "artifact_id": artifact_id,
        "vm_name": "Cloud OSD Lab 001",
        "node": "pve",
        "iso_storage": "local",
        "storage": "local-lvm",
        "network_bridge": "vmbr0",
        "architecture": "amd64",
        "os_version": "Windows 11 25H2",
        "os_activation": "Volume",
        "os_edition": "Enterprise",
        "os_language": "en-us",
        "vm_cores": 4,
        "vm_memory_mb": 8192,
        "vm_disk_size_gb": 80,
        "tpm_enabled": True,
        "secure_boot": True,
        "driver_pack_policy": "None",
        "firmware_updates_enabled": False,
        "analytics_enabled": False,
        "outbound_policy": {"mode": "blocked"},
    }
    values.update(overrides)
    return values


def _assert_blocking(body: dict, check_id: str):
    ids = {check["id"] for check in body["blocking_checks"]}
    assert check_id in ids, body


def _assert_warning(body: dict, check_id: str):
    ids = {check["id"] for check in body["warnings"]}
    assert check_id in ids, body


def test_cloudosd_base_url_honors_forwarded_https_headers(monkeypatch):
    from web import cloudosd_endpoints

    class RequestStub:
        headers = {
            "x-forwarded-proto": "https",
            "x-forwarded-host": "autopilot.gell.one",
            "host": "autopilot.gell.one",
        }
        base_url = "http://autopilot.gell.one/"

        class url:
            scheme = "http"
            netloc = "autopilot.gell.one"

    monkeypatch.delenv("AUTOPILOT_BASE_URL", raising=False)

    assert cloudosd_endpoints._base_url(RequestStub()) == "https://autopilot.gell.one"


def test_cloudosd_name_comparison_treats_windows_name_case_as_aligned():
    from web import cloudosd_pg

    comparison = cloudosd_pg.name_comparison(
        requested_name="Gell-OSD1",
        pve_name="Gell-OSD1",
        heartbeat_name="GELL-OSD1",
    )

    assert comparison["mismatch"] is False
    assert comparison["heartbeat_mismatch"] is False


def test_cloudosd_run_registers_by_identity_and_returns_workflow_package(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "CLOUDOSD-001",
            "node": "pve",
            "architecture": "amd64",
            "os_version": "Windows 11 25H2",
            "os_activation": "Volume",
            "os_edition": "Enterprise",
            "os_language": "en-us",
            "vm_cores": 4,
            "vm_memory_mb": 8192,
            "vm_disk_size_gb": 80,
        },
    )
    assert run.status_code == 201, run.text
    run_body = run.json()
    assert run_body["workflow_name"] == f"pveautopilot-{run_body['run_id']}"
    assert run_body["state"] == "created"

    identity = cloudosd_client.post(
        f"/api/cloudosd/runs/{run_body['run_id']}/identity",
        json={
            "vmid": 221,
            "vm_uuid": "ABCDEF12-3456-7890-ABCD-EF1234567890",
            "mac": "52:54:00:12:34:56",
            "node": "pve",
            "computer_name": "LAB-2452956F",
        },
    )
    assert identity.status_code == 200, identity.text
    assert identity.json()["state"] == "awaiting_pe"
    assert identity.json()["requested_vm_name"] == "CLOUDOSD-001"
    assert identity.json()["pve_vm_name"] == "LAB-2452956F"

    registered = cloudosd_client.post(
        "/api/cloudosd/pe/register",
        json={
            "vm_uuid": "abcdef12-3456-7890-abcd-ef1234567890",
            "mac": "52:54:00:12:34:56",
            "architecture": "amd64",
            "build_sha": "cloudosdtest",
        },
    )
    assert registered.status_code == 200, registered.text
    token = registered.json()["bearer_token"]
    assert token

    package = cloudosd_client.get(
        f"/api/cloudosd/pe/package/{run_body['run_id']}",
        headers=_bearer(token),
    )
    assert package.status_code == 200, package.text
    body = package.json()
    assert body["schema_version"] == 1
    assert body["run_id"] == run_body["run_id"]
    assert body["workflow_name"] == run_body["workflow_name"]
    assert body["identity"]["computer_name"] == "CLOUDOSD-001"
    assert body["server_base_url"] == "http://autopilot.test:5000"
    assert body["artifact"]["osdcloud_module_version"] == "26.4.17.1"
    assert body["os_settings"]["OperatingSystem"]["default"] == "Windows 11 25H2"
    assert body["os_settings"]["OSActivation"]["default"] == "Volume"
    assert body["os_settings"]["OSEdition"]["default"] == "Enterprise"
    assert body["os_settings"]["OSEdition"]["values"] == [
        {"Edition": "Enterprise", "EditionId": "Enterprise"}
    ]
    assert body["os_settings"]["OSLanguageCode"]["default"] == "en-us"
    assert body["user_settings"]["DriverPacks"]["Default"] == "None"
    assert body["user_settings"]["UpdateDiskDrivers"] is False
    assert body["user_settings"]["UpdateNetworkDrivers"] is False
    assert body["user_settings"]["UpdateScsiDrivers"] is False
    assert body["user_settings"]["UpdateSystemFirmware"] is False
    assert body["task"]["name"] == "osdcloud-nofirmware"
    assert body["agent"]["phase"] == "cloudosd"
    assert body["agent"]["bootstrap_token"]
    before_heartbeat = cloudosd_client.get(f"/api/cloudosd/runs/{run_body['run_id']}")
    assert before_heartbeat.status_code == 200
    assert before_heartbeat.json()["run"]["state"] != "complete"

    bootstrap = cloudosd_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer(body["agent"]["bootstrap_token"]),
        json={
            "agent_id": "cloudosd-agent-1",
            "run_id": run_body["run_id"],
            "phase": "cloudosd",
            "vmid": 221,
        },
    )
    assert bootstrap.status_code == 200, bootstrap.text
    heartbeat = cloudosd_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(bootstrap.json()["agent_token"]),
        json={
            "agent_id": "cloudosd-agent-1",
            "vmid": 221,
            "current_run_id": run_body["run_id"],
            "current_phase": "cloudosd",
            "os_name": "Windows 11 Enterprise",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    after_heartbeat = cloudosd_client.get(f"/api/cloudosd/runs/{run_body['run_id']}")
    assert after_heartbeat.status_code == 200
    assert after_heartbeat.json()["run"]["state"] == "complete"
    assert after_heartbeat.json()["run"]["first_heartbeat_at"]

    from web import ts_engine_pg

    steps = ts_engine_pg.list_run_steps(pg_conn, run_body["run_id"])
    assert [step["kind"] for step in steps][-1] == "wait_agent_heartbeat"
    assert steps[-1]["phase"] == "full_os"


def test_cloudosd_pe_register_rejects_wrong_identity(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "CLOUDOSD-002",
            "architecture": "amd64",
            "vm_memory_mb": 8192,
        },
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/identity",
        json={
            "vmid": 222,
            "vm_uuid": "11111111-2222-3333-4444-555555555555",
            "mac": "52:54:00:aa:bb:cc",
            "node": "pve",
        },
    ).status_code == 200

    registered = cloudosd_client.post(
        "/api/cloudosd/pe/register",
        json={
            "vm_uuid": "11111111-2222-3333-4444-555555555555",
            "mac": "52:54:00:00:00:00",
            "architecture": "amd64",
        },
    )

    assert registered.status_code == 404


def test_cloudosd_error_event_marks_run_failed(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "CLOUDOSD-FAIL",
            "architecture": "amd64",
            "vm_memory_mb": 8192,
        },
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/identity",
        json={
            "vmid": 223,
            "vm_uuid": "22222222-3333-4444-5555-666666666666",
            "mac": "52:54:00:aa:bb:dd",
            "node": "pve",
        },
    ).status_code == 200
    registered = cloudosd_client.post(
        "/api/cloudosd/pe/register",
        json={
            "vm_uuid": "22222222-3333-4444-5555-666666666666",
            "mac": "52:54:00:aa:bb:dd",
            "architecture": "amd64",
            "build_sha": "cloudosdtest",
        },
    )
    assert registered.status_code == 200, registered.text

    start_event = cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/events",
        headers=_bearer(registered.json()["bearer_token"]),
        json={
            "phase": "pe",
            "event_type": "osdcloud_start",
            "severity": "info",
            "message": "Starting OSDCloud deploy",
        },
    )
    assert start_event.status_code == 200, start_event.text
    started = cloudosd_client.get(f"/api/cloudosd/runs/{run_id}")
    assert started.status_code == 200
    assert started.json()["run"]["osdcloud_started_at"]

    complete_event = cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/events",
        headers=_bearer(registered.json()["bearer_token"]),
        json={
            "phase": "pe",
            "event_type": "cloudosd_pe_complete",
            "severity": "info",
            "message": "CloudOSD PE phase complete",
        },
    )
    assert complete_event.status_code == 200, complete_event.text
    finished = cloudosd_client.get(f"/api/cloudosd/runs/{run_id}")
    assert finished.status_code == 200
    assert finished.json()["run"]["osdcloud_finished_at"]

    event = cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/events",
        headers=_bearer(registered.json()["bearer_token"]),
        json={
            "phase": "pe",
            "event_type": "cloudosd_failed",
            "severity": "error",
            "message": "Deploy-OSDCloud failed",
        },
    )
    assert event.status_code == 200, event.text
    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["state"] == "failed"

    row = pg_conn.execute(
        "SELECT state, last_error FROM ts_provisioning_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row["state"] == "failed"
    assert row["last_error"] == "Deploy-OSDCloud failed"


def test_cloudosd_run_rejects_too_little_memory(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "CLOUDOSD-LOWMEM",
            "architecture": "amd64",
            "vm_memory_mb": 4096,
        },
    )

    assert response.status_code == 400
    assert "at least 6144 MB" in response.text


def test_cloudosd_preflight_returns_blocking_warning_structure(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(
            artifact["id"],
            vm_name="Cloud OSD Lab 001",
            vm_memory_mb=7168,
            vm_disk_size_gb=80,
            firmware_updates_enabled=True,
            driver_pack_policy="OSDCloud",
            analytics_enabled=True,
        ),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["ok"] is True
    assert body["launch_allowed"] is True
    assert body["blocking_checks"] == []
    assert body["normalized_computer_name"] == "CloudOSDLab001"
    assert body["artifact"]["readiness"] == "ready"
    assert body["asset_status"]["ready"] is True
    _assert_warning(body, "memory_recommended")
    _assert_warning(body, "firmware_updates")
    _assert_warning(body, "driver_pack_policy")
    _assert_warning(body, "analytics_allowed")


def test_cloudosd_preflight_warns_for_supported_non_default_os_choice(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(
            artifact["id"],
            os_version="Windows 10 22H2",
            os_edition="Home",
            os_language="de-de",
        ),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["launch_allowed"] is True
    _assert_warning(body, "os_outside_default")


def test_cloudosd_preflight_blocks_os_choices_outside_pinned_catalog(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(
            artifact["id"],
            os_version="Windows 12 99H9",
            os_activation="KMS",
            os_edition="Datacenter",
            os_language="zz-zz",
        ),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["launch_allowed"] is False
    _assert_blocking(body, "os_version_unsupported")
    _assert_blocking(body, "os_activation_unsupported")
    _assert_blocking(body, "os_edition_unsupported")
    _assert_blocking(body, "os_language_unsupported")


def test_cloudosd_preflight_blocks_not_uploaded_artifact(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn, proxmox_volid=None)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(artifact["id"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["launch_allowed"] is False
    assert body["artifact"]["readiness"] == "not_uploaded"
    _assert_blocking(body, "artifact_not_uploaded")


def test_cloudosd_preflight_blocks_low_ram_and_disk(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(artifact["id"], vm_memory_mb=4096, vm_disk_size_gb=60),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    _assert_blocking(body, "memory_minimum")
    _assert_blocking(body, "disk_minimum")


def test_cloudosd_preflight_blocks_invalid_computer_name(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(artifact["id"], vm_name="!!!"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["normalized_computer_name"] == ""
    _assert_blocking(body, "computer_name_invalid")


def test_cloudosd_preflight_blocks_missing_agent_assets(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    artifact = _create_artifact(pg_conn)
    monkeypatch.setenv("AUTOPILOT_AGENT_MSI_PATH", "/no/such/AutopilotAgent.msi")

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(artifact["id"]),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["asset_status"]["ready"] is False
    _assert_blocking(body, "asset_autopilotagent_msi_missing")


def test_cloudosd_preflight_blocks_unavailable_proxmox_target(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    artifact = _create_artifact(pg_conn)

    from web import app as web_app

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [{"vmid": 101, "name": "EXISTING-VM"}],
        }
        if path not in values:
            raise AssertionError(path)
        return values[path]

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })
    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)

    response = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(
            artifact["id"],
            node="missing-node",
            iso_storage="missing-iso",
            storage="missing-disk",
            network_bridge="missing-bridge",
            vm_name="EXISTING-VM",
            vmid=101,
        ),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    _assert_blocking(body, "proxmox_node_unavailable")
    _assert_blocking(body, "proxmox_iso_storage_unavailable")
    _assert_blocking(body, "proxmox_disk_storage_unavailable")
    _assert_blocking(body, "proxmox_bridge_unavailable")
    _assert_blocking(body, "vmid_collision")
    _assert_blocking(body, "vm_name_collision")


def test_cloudosd_assets_status_reports_required_payloads(cloudosd_client):
    response = cloudosd_client.get("/api/cloudosd/assets/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["ready"] is True
    for key in (
        "autopilotagent_msi",
        "first_boot_script",
        "bridge_script",
        "osd_client_package",
    ):
        assert body["assets"][key]["available"] is True
        assert body["assets"][key]["sha256"]


def test_cloudosd_run_creation_enforces_preflight_blockers(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn, proxmox_volid=None)

    response = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(artifact["id"]),
    )

    assert response.status_code == 409
    assert "blocking preflight" in response.text


def test_cloudosd_runs_events_endpoint_lists_grouped_evidence(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg

    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(artifact["id"], vm_name="CLOUDOSD-EVENTS"),
    ).json()
    cloudosd_pg.append_event(
        pg_conn,
        run_id=run["run_id"],
        phase="offline_validation",
        event_type="offline_validation_ok",
        message="Offline validation passed",
        data={"setupcomplete": "scheduled"},
    )

    response = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}/events")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1
    assert body["groups"]["offline_validation"][0]["event_type"] == "offline_validation_ok"
    assert body["milestone_groups"]["offline validation"][0]["event_type"] == "offline_validation_ok"


def test_cloudosd_run_detail_page_live_refreshes_run_evidence_and_milestones(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg

    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(artifact["id"], vm_name="CLOUDOSD-LIVE"),
    ).json()
    cloudosd_pg.append_event(
        pg_conn,
        run_id=run["run_id"],
        phase="pe",
        event_type="pe_registered",
        message="CloudOSD PE bridge registered",
    )
    cloudosd_pg.append_event(
        pg_conn,
        run_id=run["run_id"],
        phase="pe",
        event_type="osdcloud_start",
        message="Starting OSDCloud deploy",
    )

    response = cloudosd_client.get(f"/cloudosd/runs/{run['run_id']}")

    assert response.status_code == 200, response.text
    assert 'id="cloudosdRunDetail"' in response.text
    assert 'data-cloudosd-field="pe_registered_at"' in response.text
    assert f"/api/cloudosd/runs/${{encodeURIComponent(runId)}}" in response.text
    assert "window.setTimeout(refresh, 5000)" in response.text
    assert "CloudOSD PE bridge registered" in response.text
    assert "Starting OSDCloud deploy" in response.text


def test_cloudosd_proxmox_options_fallback_to_configured_defaults(
    cloudosd_client,
    monkeypatch,
):
    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })

    def failing_proxmox_api(*args, **kwargs):
        raise RuntimeError("no live pve")

    monkeypatch.setattr(web_app, "_proxmox_api", failing_proxmox_api)

    response = cloudosd_client.get("/api/cloudosd/proxmox/options")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["defaults"]["node"] == "pve"
    assert body["nodes"] == ["pve"]
    assert body["storages"]["iso"] == ["local"]
    assert body["storages"]["disk"] == ["local-lvm"]
    assert body["bridges"] == ["vmbr0"]
    assert body["source"] == "configured"


def test_cloudosd_proxmox_options_exposes_windows_catalog_choices(
    cloudosd_client,
):
    response = cloudosd_client.get("/api/cloudosd/proxmox/options")

    assert response.status_code == 200, response.text
    catalog = response.json()["catalog"]
    assert catalog["defaults"]["os_version"] == "Windows 11 25H2"
    assert catalog["defaults"]["os_edition"] == "Enterprise"
    assert "Windows 11 24H2" in catalog["os_versions"]
    assert "Windows 11 23H2" in catalog["os_versions"]
    assert "Windows 11 22H2" in catalog["os_versions"]
    assert "Windows 11 21H2" in catalog["os_versions"]
    assert "Windows 10 22H2" in catalog["os_versions"]
    assert "Home" in catalog["os_editions"]
    assert "Home N" in catalog["os_editions"]
    assert "Enterprise N" in catalog["os_editions"]
    assert "Education" in catalog["os_editions"]
    assert "Pro N" in catalog["os_editions"]
    assert "de-de" in catalog["os_languages"]
    assert "fr-fr" in catalog["os_languages"]
    assert "zh-tw" in catalog["os_languages"]


def test_cloudosd_job_caps_and_public_bridge_routes_are_additive(pg_conn):
    from web import auth, jobs_pg

    caps = {r["job_type"]: r["max_concurrent"] for r in jobs_pg.list_job_type_limits()}
    assert caps["cloudosd_build_iso"] == 1
    assert caps["provision_cloudosd"] == 2
    assert caps["provision_clone"] == 3

    assert auth.is_exempt_path("/api/cloudosd/pe/register")
    assert auth.is_exempt_path("/api/cloudosd/pe/package/run-1")
    assert auth.is_exempt_path("/api/cloudosd/assets/PVEAutopilot-FirstBoot.ps1")
    assert auth.is_exempt_path("/api/cloudosd/runs/run-1")
    assert auth.is_exempt_path("/api/cloudosd/runs/run-1/identity")
    assert auth.is_exempt_path("/api/cloudosd/runs/run-1/events")
    assert not auth.is_exempt_path("/api/cloudosd/runs")
    assert not auth.is_exempt_path("/api/cloudosd/runs/run-1/provision")


def test_cloudosd_static_first_boot_assets_are_served(cloudosd_client):
    first_boot = cloudosd_client.get("/api/cloudosd/assets/PVEAutopilot-FirstBoot.ps1")
    assert first_boot.status_code == 200
    assert "Invoke-PVEAutopilotFirstBoot" in first_boot.text

    postinstall = cloudosd_client.get(
        "/api/cloudosd/assets/autopilotagent-postinstall.ps1",
    )
    assert postinstall.status_code == 200
    assert "BootstrapToken" in postinstall.text


def test_cloudosd_agent_msi_asset_can_come_from_app_output(
    cloudosd_client,
    monkeypatch,
):
    from web import cloudosd_endpoints

    msi_path = (
        Path(cloudosd_endpoints._APP_ROOT)
        / "output"
        / "cloudosd"
        / "AutopilotAgent.msi"
    )
    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    msi_path.parent.mkdir(parents=True, exist_ok=True)
    msi_path.write_bytes(b"fake-msi")
    try:
        response = cloudosd_client.get("/api/cloudosd/assets/autopilotagent.msi")
        assert response.status_code == 200
        assert response.content == b"fake-msi"
    finally:
        msi_path.unlink(missing_ok=True)


def test_cloudosd_build_iso_endpoint_enqueues_ssh_wrapper(cloudosd_client):
    from web import jobs_pg

    response = cloudosd_client.post(
        "/api/cloudosd/artifacts/build",
        json={
            "remote": "Adam.Gell@10.211.55.6",
            "remote_root": r"F:\BuildRoot",
            "architecture": "amd64",
            "osdcloud_module_version": "26.4.17.1",
        },
    )

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["job_type"] == "cloudosd_build_iso"
    assert "cloudosd_remote_build.py" in " ".join(job["cmd"])
    repo_arg = job["cmd"][job["cmd"].index("--repo-root") + 1]
    assert (Path(repo_arg) / "tools" / "cloudosd-build" / "build-cloudosd.ps1").exists()
    assert job["args"]["remote"] == "Adam.Gell@10.211.55.6"


def test_cloudosd_provision_endpoint_enqueues_dedicated_playbook(
    cloudosd_client,
    pg_conn,
):
    from web import jobs_pg

    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "CLOUDOSD-PROVISION",
            "architecture": "amd64",
            "vm_memory_mb": 8192,
        },
    ).json()

    response = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/provision")

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["job_type"] == "provision_cloudosd"
    assert "provision_proxmox_cloudosd.yml" in " ".join(job["cmd"])
    assert job["args"]["cloudosd_run_id"] == run["run_id"]
    assert job["args"]["cloudosd_artifact_volid"] == artifact["proxmox_volid"]
    assert job["args"]["proxmox_node"] == "pve"
    assert job["args"]["proxmox_storage"] == "local-lvm"
    assert job["args"]["proxmox_bridge"] == "vmbr0"
    assert job["args"]["vm_name"] == "CLOUDOSD-PROVISION"


def test_cloudosd_wizard_page_lists_artifacts_and_policy(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.get("/cloudosd")

    assert response.status_code == 200
    assert "CloudOSD" in response.text
    assert artifact["iso_sha256"] in response.text
    assert "6144" in response.text
    assert "OSDCloud analytics blocked" in response.text
    assert "/api/cloudosd/artifacts/build" in response.text
    assert "Windows 11 24H2" in response.text
    assert "Windows 11 21H2" in response.text
    assert "Windows 10 22H2" in response.text
    assert "Home N" in response.text
    assert "Enterprise N" in response.text
    assert "Education" in response.text
    assert "de-de" in response.text
    assert "Single-VM Deployment" in response.text
    assert "Review &amp; Launch" in response.text
    assert "Blocking Checks" in response.text
    assert "Recent CloudOSD Runs" in response.text
    assert "Requested name" in response.text
    assert "Heartbeat computer name" in response.text
    assert "cloudosd-toggle-line" in response.text
    assert response.text.index("Single-VM Deployment") < response.text.index("OSDCloud Module Pin")
    assert response.text.index("Recent CloudOSD Runs") < response.text.index("<h2>Artifacts</h2>")


def test_cloudosd_run_detail_page_shows_identity_and_heartbeat_evidence(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(
            artifact["id"],
            vm_name="Cloud OSD Lab 001",
        ),
    ).json()
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run['run_id']}/identity",
        json={
            "vmid": 242,
            "vm_uuid": "33333333-4444-5555-6666-777777777777",
            "mac": "52:54:00:aa:bb:ee",
            "node": "pve",
            "computer_name": "Cloud OSD Lab 001",
        },
    ).status_code == 200

    response = cloudosd_client.get(f"/cloudosd/runs/{run['run_id']}")

    assert response.status_code == 200
    body = response.text
    assert "CloudOSD Run" in body
    assert "Cloud OSD Lab 001" in body
    assert "CloudOSDLab001" in body
    assert "Heartbeat gate" in body
    assert "AutopilotAgent" in body
    assert "offline validation" in body.lower()
