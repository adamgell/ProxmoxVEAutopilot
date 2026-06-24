from __future__ import annotations

import os
import re
import shutil
import urllib.parse
from hashlib import sha1

import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from cryptography.fernet import Fernet


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed CloudOSD endpoint tests",
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write_fake_msi(path: Path, *, size: int = 4096, payload: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        path.write_bytes(payload)
        return path
    path.write_bytes(b"MZ" + (b"\0" * (size - 2)))
    return path


@pytest.fixture
def cloudosd_client(pg_conn, monkeypatch, tmp_path):
    from web import agent_telemetry_pg, cloudosd_cache, cloudosd_pg, devices_pg, jobs_pg, lab_bubbles_pg, sequences_pg, ts_engine_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    jobs_pg.reset_for_tests(pg_conn)
    jobs_pg.init(pg_conn)
    cloudosd_cache.reset_for_tests(pg_conn)
    cloudosd_cache.init(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn)
    cloudosd_pg.init(pg_conn)
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://autopilot.test:5000")
    agent_msi = _write_fake_msi(tmp_path / "artifacts" / "AutopilotAgent.msi")
    monkeypatch.setenv("AUTOPILOT_AGENT_MSI_PATH", str(agent_msi))

    from web import app as web_app

    monkeypatch.setattr(web_app, "_load_vars", lambda: {
        "proxmox_node": "pve",
        "proxmox_iso_storage": "local",
        "proxmox_storage": "local-lvm",
        "proxmox_bridge": "vmbr0",
    })

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/cluster/nextid": 100,
            "/cluster/resources?type=vm": [],
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


def test_auto_select_cloudosd_artifact_picks_ready_or_409(pg_conn):
    from fastapi import HTTPException
    from web import app as web_app, cloudosd_pg

    cloudosd_pg.reset_for_tests(pg_conn)
    cloudosd_pg.init(pg_conn)

    # Operators no longer choose the OSDCloud artifact. With nothing ready,
    # provisioning fails clearly instead of guessing.
    with pytest.raises(HTTPException) as exc_info:
        web_app._auto_select_cloudosd_artifact_id(pg_conn)
    assert exc_info.value.status_code == 409

    artifact = _create_artifact(pg_conn)
    pg_conn.commit()

    assert web_app._auto_select_cloudosd_artifact_id(pg_conn) == str(artifact["id"])


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


def _assert_local_admin(local_admin: dict):
    assert local_admin["username"] == "localadmin"
    password = local_admin["password"]
    assert 8 <= len(password) <= 12
    assert re.search(r"[A-Z]", password)
    assert re.search(r"[a-z]", password)
    assert re.search(r"[0-9]", password)
    assert re.search(r"[!#%+?]", password)
    assert not re.search(r"[O0Il1\"'`\s]", password)


def _create_feature_cache_entry(pg_conn, **overrides):
    from web import cloudosd_cache

    values = {
        "entry_type": "feature_image",
        "status": "ready",
        "osdcloud_module_version": "26.4.17.1",
        "windows_version": "Windows 11 25H2",
        "release_id": "25H2",
        "build": "26200.8246",
        "architecture": "amd64",
        "language": "en-us",
        "activation": "Volume",
        "edition": "Enterprise",
        "catalog_file": "catalogs/operatingsystem/26200.8246-win11-25h2.xml",
        "file_name": "cached-win11-25h2-enterprise-volume-en-us.esd",
        "source_url": "https://download.example.test/cached-win11-25h2-enterprise-volume-en-us.esd",
        "expected_size_bytes": 7,
        "expected_sha256": "2bb80d537b1da3e38bd30361aa855686bde0ba99b64dc823580686d235f3f1c8",
        "sha256": "2bb80d537b1da3e38bd30361aa855686bde0ba99b64dc823580686d235f3f1c8",
        "size_bytes": 7,
        "local_path": "/tmp/cached-win11-25h2-enterprise-volume-en-us.esd",
    }
    values.update(overrides)
    entry = cloudosd_cache._upsert_entry(pg_conn, values)
    pg_conn.commit()
    return entry


def _create_cloudosd_run(cloudosd_client, pg_conn, **overrides):
    artifact = _create_artifact(pg_conn)
    payload = _run_payload(artifact["id"], **overrides)
    response = cloudosd_client.post("/api/cloudosd/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_cloudosd_run_records_bubble_membership(
    cloudosd_client,
    pg_conn,
):
    from web import lab_bubbles_pg

    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(
            artifact["id"],
            bubble_id=bubble["id"],
            asset_role="domain_controller",
            vmid=245,
        ),
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assets = lab_bubbles_pg.list_assets(pg_conn, bubble["id"])
    assert len(assets) == 1
    assert assets[0]["asset_type"] == "vm"
    assert assets[0]["asset_role"] == "domain_controller"
    assert assets[0]["vmid"] == 245
    assert assets[0]["run_id"] == run["run_id"]
    assert assets[0]["membership_state"] == "provisioning"


def test_cloudosd_run_rejects_missing_bubble_before_run_create(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg, lab_bubbles_pg

    artifact = _create_artifact(pg_conn)
    missing_bubble = "00000000-0000-0000-0000-000000000001"

    response = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(
            artifact["id"],
            bubble_id=missing_bubble,
            vmid=246,
        ),
    )

    assert response.status_code == 404
    assert cloudosd_pg.list_runs(pg_conn) == []
    assert lab_bubbles_pg.list_assets(pg_conn, missing_bubble) == []


def _patch_sequence_cipher(monkeypatch, tmp_path: Path):
    from web import app as web_app

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key_path = secrets / "credential_key"
    key_path.write_bytes(Fernet.generate_key())
    web_app._CIPHER = None
    monkeypatch.setattr(web_app, "SECRETS_DIR", secrets)
    monkeypatch.setattr(web_app, "CREDENTIAL_KEY", key_path)
    return web_app._cipher()


def _create_domain_join_sequence(
    pg_conn,
    cipher,
    *,
    unsupported_step: bool = False,
    domain_controller_ipv4: str = "",
):
    from web import sequences_pg

    cred_id = sequences_pg.create_credential(
        pg_conn,
        cipher,
        name="cloudosd-domain-join",
        type="domain_join",
        payload={
            "domain_fqdn": "home.gell.one",
            "username": "HOME\\svc-cloudjoin",
            "password": "join-secret-for-tests",
            "ou_hint": "OU=CloudOSD,DC=home,DC=gell,DC=one",
        },
    )
    steps = [
        {
            "step_type": "join_ad_domain",
            "params": {
                "credential_id": cred_id,
                "ou_path": "",
                **(
                    {"domain_controller_ipv4": domain_controller_ipv4}
                    if domain_controller_ipv4
                    else {}
                ),
            },
            "enabled": True,
        },
    ]
    if unsupported_step:
        steps.append({
            "step_type": "run_script",
            "params": {"name": "unsupported", "script": "Write-Host no"},
            "enabled": True,
        })
    return sequences_pg.create_sequence(
        pg_conn,
        name="CloudOSD AD Join",
        description="Join AD through CloudOSD specialize",
        produces_autopilot_hash=True,
        steps=steps,
    )


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


def test_cloudosd_provision_base_url_uses_guest_reachable_url(monkeypatch):
    from web import app as web_app
    from web import cloudosd_endpoints

    monkeypatch.setenv("AUTOPILOT_BASE_URL", "http://127.0.0.1:5000")
    monkeypatch.setattr(
        web_app,
        "_derive_guest_reachable_base_url",
        lambda config: "http://controller:5000",
    )

    assert cloudosd_endpoints._base_url(None) == "http://controller:5000"


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
    _assert_local_admin(body["local_admin"])
    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run_body['run_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["local_admin"] == body["local_admin"]
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
    assert after_heartbeat.json()["run"]["state"] == "full_os_waiting_v2"
    assert after_heartbeat.json()["run"]["first_heartbeat_at"]

    from web import ts_engine_pg

    steps = ts_engine_pg.list_run_steps(pg_conn, run_body["run_id"])
    assert [step["kind"] for step in steps][-2:] == [
        "capture_autopilot_hash",
        "wait_agent_heartbeat",
    ]
    assert steps[-1]["phase"] == "full_os"
    assert steps[-1]["state"] == "done"
    assert steps[-2]["state"] == "pending"

    v2_reg = cloudosd_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run_body["run_id"],
            "agent_id": "cloudosd-agent-1",
            "phase": "full_os",
            "capabilities": ["capture_autopilot_hash"],
        },
    )
    assert v2_reg.status_code == 200, v2_reg.text
    v2_next = cloudosd_client.post(
        "/osd/v2/agent/next",
        headers=_bearer(v2_reg.json()["bearer_token"]),
        json={
            "run_id": run_body["run_id"],
            "agent_id": "cloudosd-agent-1",
            "phase": "full_os",
        },
    )
    assert v2_next.status_code == 200, v2_next.text
    action = v2_next.json()["actions"][0]
    assert action["kind"] == "capture_autopilot_hash"
    result = cloudosd_client.post(
        f"/osd/v2/agent/step/{action['step_id']}/result",
        headers=_bearer(v2_reg.json()["bearer_token"]),
        json={
            "run_id": run_body["run_id"],
            "agent_id": "cloudosd-agent-1",
            "phase": "full_os",
            "status": "success",
            "message": "hash uploaded through AutopilotAgent v2",
            "data": {"source": "autopilotagent-v2"},
        },
    )
    assert result.status_code == 200, result.text
    complete = cloudosd_client.get(f"/api/cloudosd/runs/{run_body['run_id']}")
    assert complete.status_code == 200, complete.text
    assert complete.json()["run"]["state"] == "complete"


def test_cloudosd_package_uses_ready_feature_cache_and_download_endpoint(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import cloudosd_cache

    cache_root = tmp_path / "cloudosd-cache"
    cache_root.mkdir()
    payload_path = cache_root / "feature.esd"
    payload_path.write_bytes(b"secret\n")
    monkeypatch.setenv("AUTOPILOT_CLOUDOSD_CACHE_ROOT", str(cache_root))
    entry = _create_feature_cache_entry(
        pg_conn,
        file_name="feature.esd",
        local_path=str(payload_path),
    )
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post("/api/cloudosd/runs", json=_run_payload(artifact["id"]))
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/identity",
        json={
            "vmid": 248,
            "vm_uuid": "abcdef12-3456-7890-abcd-ef1234567890",
            "mac": "52:54:00:12:34:56",
            "node": "pve",
            "computer_name": "CLOUDOSD-001",
        },
    ).status_code == 200
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
    package = cloudosd_client.get(
        f"/api/cloudosd/pe/package/{run_id}",
        headers=_bearer(token),
    )
    assert package.status_code == 200, package.text
    cache = package.json()["cache"]
    assert cache["policy"] == "direct_on_miss"
    assert cache["feature_image"]["hit"] is True
    assert cache["feature_image"]["entry_id"] == entry["id"]
    parsed = urllib.parse.urlparse(cache["feature_image"]["download_url"])
    download_path = parsed.path + "?" + parsed.query
    assert parsed.path.endswith("/download/feature.esd")
    assert urllib.parse.parse_qs(parsed.query)["run_id"] == [run_id]

    head = cloudosd_client.head(download_path)
    assert head.status_code == 200, head.text
    assert head.headers["content-length"] == "7"
    served = cloudosd_client.get(download_path)
    assert served.status_code == 200, served.text
    assert served.content == b"secret\n"
    updated = cloudosd_cache.get_entry(pg_conn, entry["id"])
    assert updated["served_count"] == 1


def test_cloudosd_preflight_reports_feature_cache_hit_and_quality_cache_ready(
    cloudosd_client,
    pg_conn,
    tmp_path,
):
    from web import cloudosd_cache

    artifact = _create_artifact(pg_conn)
    feature_path = tmp_path / "cached-win11-25h2-enterprise-volume-en-us.esd"
    feature_path.write_bytes(b"secret\n")
    quality_path = tmp_path / "windows11.0-kb5089549-x64.msu"
    quality_path.write_bytes(b"quality\n")
    _create_feature_cache_entry(pg_conn, local_path=str(feature_path))
    cloudosd_cache._upsert_entry(
        pg_conn,
        {
            "entry_type": "quality_update",
            "status": "ready",
            "windows_version": "Windows 11 25H2",
            "release_id": "25H2",
            "architecture": "amd64",
            "language": "neutral",
            "activation": "all",
            "edition": "all",
            "title": "2026-05 Cumulative Update for Windows 11, version 25H2 for x64-based Systems (KB5089549)",
            "kb": "KB5089549",
            "file_name": "windows11.0-kb5089549-x64.msu",
            "source_url": "https://download.windowsupdate.test/windows11.0-kb5089549-x64.msu",
            "local_path": str(quality_path),
            "sha256": "0" * 64,
            "size_bytes": 8,
        },
    )
    pg_conn.commit()
    preflight = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(artifact["id"]),
    )
    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["cache"]["policy"] == "direct_on_miss"
    assert body["cache"]["feature_image"]["status"] == "ready"
    assert len(body["cache"]["quality_updates"]) == 1
    _assert_warning(body, "cloudosd_feature_cache_hit")
    _assert_warning(body, "cloudosd_quality_cache_ready")


def test_cloudosd_preflight_demotes_stale_ready_cache_entries(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_cache

    artifact = _create_artifact(pg_conn)
    feature = _create_feature_cache_entry(pg_conn, local_path="/tmp/missing-feature.esd")
    quality = cloudosd_cache._upsert_entry(
        pg_conn,
        {
            "entry_type": "quality_update",
            "status": "ready",
            "windows_version": "Windows 11 25H2",
            "release_id": "25H2",
            "architecture": "amd64",
            "language": "neutral",
            "activation": "all",
            "edition": "all",
            "title": "2026-05 Cumulative Update for Windows 11, version 25H2 for x64-based Systems (KB5089549)",
            "kb": "KB5089549",
            "file_name": "windows11.0-kb5089549-x64.msu",
            "source_url": "https://download.windowsupdate.test/windows11.0-kb5089549-x64.msu",
            "local_path": "/tmp/missing-quality.msu",
            "sha256": "0" * 64,
            "size_bytes": 42,
        },
    )
    pg_conn.commit()

    preflight = cloudosd_client.post(
        "/api/cloudosd/preflight",
        json=_run_payload(artifact["id"]),
    )

    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["cache"]["feature_image"]["status"] == "missing"
    assert body["cache"]["quality_updates"] == []
    _assert_warning(body, "cloudosd_feature_cache_miss")
    _assert_warning(body, "cloudosd_quality_cache_missing")
    assert cloudosd_cache.get_entry(pg_conn, feature["id"])["status"] == "missing"
    assert cloudosd_cache.get_entry(pg_conn, quality["id"])["status"] == "missing"


def test_cloudosd_quality_cache_warm_verifies_expected_sha1(pg_conn, monkeypatch, tmp_path):
    from web import cloudosd_cache

    payload = b"quality update payload"
    expected_sha1 = sha1(payload).hexdigest()
    cache_root = tmp_path / "cloudosd-cache"
    cache_root.mkdir()
    monkeypatch.setenv("AUTOPILOT_CLOUDOSD_CACHE_ROOT", str(cache_root))

    entry = cloudosd_cache._upsert_entry(
        pg_conn,
        {
            "entry_type": "quality_update",
            "status": "missing",
            "windows_version": "Windows 11 25H2",
            "release_id": "25H2",
            "architecture": "amd64",
            "language": "neutral",
            "activation": "all",
            "edition": "all",
            "title": "2026-05 Cumulative Update for Windows 11 Version 25H2 for x64-based Systems",
            "kb": "KB5089549",
            "file_name": f"windows11.0-kb5089549-x64_{expected_sha1}.msu",
            "source_url": "https://download.example.test/windows11.0-kb5089549-x64.msu",
            "expected_sha1": expected_sha1,
        },
    )
    pg_conn.commit()

    class FakeResponse:
        def __init__(self, body: bytes):
            self._body = body
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                size = len(self._body) - self._offset
            chunk = self._body[self._offset:self._offset + size]
            self._offset += len(chunk)
            return chunk

    monkeypatch.setattr(cloudosd_cache.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse(payload))

    warmed = cloudosd_cache.warm_entry(pg_conn, entry["id"])
    assert warmed["status"] == "ready"
    assert warmed["sha1"] == expected_sha1
    assert warmed["size_bytes"] == len(payload)
    assert Path(warmed["local_path"]).is_file()


def test_cloudosd_provision_sequence_with_domain_join_adds_v2_steps_and_pe_only_secret(
    cloudosd_client,
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import app as web_app, cloudosd_pg, jobs_pg, ts_engine_pg

    cipher = _patch_sequence_cipher(monkeypatch, tmp_path)
    sequence_id = _create_domain_join_sequence(pg_conn, cipher)
    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
        },
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "group_tag": "GellNative",
            "profile": "",
            "hostname_pattern": "GELL-AD-{index}",
            "sequence_id": str(sequence_id),
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    jobs = [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ]
    assert len(jobs) == 1
    job_text = str(jobs[0]["args"])
    assert "join-secret-for-tests" not in job_text
    run_id = jobs[0]["args"]["cloudosd_run_id"]

    run = cloudosd_pg.get_run(pg_conn, run_id)
    assert run["domain_join"]["enabled"] is True
    assert run["domain_join"]["domain_fqdn"] == "home.gell.one"
    assert run["domain_join"]["credential_domain"] == "HOME"
    assert "username" not in run["domain_join"]
    assert "password" not in run["domain_join"]
    assert "join-secret-for-tests" not in str(run)

    steps = ts_engine_pg.list_run_steps(pg_conn, run_id)
    kinds = [step["kind"] for step in steps]
    assert "stage_ad_domain_join_unattend" in kinds
    assert "verify_ad_domain_join" in kinds
    assert "join-secret-for-tests" not in str(steps)

    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/identity",
        json={
            "vmid": 244,
            "vm_uuid": "44444444-5555-6666-7777-888888888888",
            "mac": "52:54:00:aa:bb:44",
            "node": "pve",
            "computer_name": "GELL-AD-01",
        },
    ).status_code == 200
    registered = cloudosd_client.post(
        "/api/cloudosd/pe/register",
        json={
            "vm_uuid": "44444444-5555-6666-7777-888888888888",
            "mac": "52:54:00:aa:bb:44",
            "architecture": "amd64",
            "build_sha": "cloudosdtest",
        },
    )
    assert registered.status_code == 200, registered.text
    package = cloudosd_client.get(
        f"/api/cloudosd/pe/package/{run_id}",
        headers=_bearer(registered.json()["bearer_token"]),
    )
    assert package.status_code == 200, package.text
    domain_join = package.json()["domain_join"]
    assert domain_join["enabled"] is True
    assert domain_join["password"] == "join-secret-for-tests"
    assert domain_join["username"] == "svc-cloudjoin"


def test_cloudosd_domain_join_with_dc_ip_uses_full_os_join_role(
    cloudosd_client,
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import app as web_app, cloudosd_pg, jobs_pg, ts_engine_pg

    cipher = _patch_sequence_cipher(monkeypatch, tmp_path)
    sequence_id = _create_domain_join_sequence(
        pg_conn,
        cipher,
        domain_controller_ipv4="192.168.2.210",
    )
    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
        },
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "group_tag": "GellNative",
            "profile": "",
            "hostname_pattern": "GELL-FULL-{index}",
            "sequence_id": str(sequence_id),
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    run_id = next(
        job["args"]["cloudosd_run_id"]
        for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    )
    run = cloudosd_pg.get_run(pg_conn, run_id)
    assert run["domain_join"]["domain_controller_ipv4"] == "192.168.2.210"

    steps = ts_engine_pg.list_run_steps(pg_conn, run_id)
    kinds = [step["kind"] for step in steps]
    assert "stage_ad_domain_join_unattend" not in kinds
    assert kinds[kinds.index("join_domain_role") + 1] == "verify_ad_domain_join"
    join_step = next(step for step in steps if step["kind"] == "join_domain_role")
    assert join_step["phase"] == "full_os"
    assert join_step["reboot_behavior"] == "required"
    assert join_step["resolved_params_json"]["domain_controller_ipv4"] == "192.168.2.210"

    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/identity",
        json={
            "vmid": 245,
            "vm_uuid": "55555555-6666-7777-8888-999999999999",
            "mac": "52:54:00:aa:bb:55",
            "node": "pve",
            "computer_name": "GELL-FULL-01",
        },
    ).status_code == 200
    registered = cloudosd_client.post(
        "/api/cloudosd/pe/register",
        json={
            "vm_uuid": "55555555-6666-7777-8888-999999999999",
            "mac": "52:54:00:aa:bb:55",
            "architecture": "amd64",
            "build_sha": "cloudosdtest",
        },
    )
    assert registered.status_code == 200, registered.text
    package = cloudosd_client.get(
        f"/api/cloudosd/pe/package/{run_id}",
        headers=_bearer(registered.json()["bearer_token"]),
    )
    assert package.status_code == 200, package.text
    assert "domain_join" not in package.json()
    assert "join-secret-for-tests" not in package.text


def test_cloudosd_workgroup_run_generates_visible_local_admin_credential(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)

    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(artifact["id"], vm_name="WRKGRP-CRED"),
    )

    assert run.status_code == 201, run.text
    local_admin = run.json()["local_admin"]
    _assert_local_admin(local_admin)
    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run.json()['run_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["local_admin"] == local_admin


def test_cloudosd_legacy_run_without_local_admin_does_not_invent_password(
    cloudosd_client,
    pg_conn,
):
    run = _create_cloudosd_run(cloudosd_client, pg_conn, vm_name="WRKGRP-LEGACY")
    pg_conn.execute(
        "UPDATE cloudosd_runs SET local_admin_json = '{}'::jsonb WHERE run_id = %s",
        (run["run_id"],),
    )
    pg_conn.commit()

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["run"]["local_admin"] == {
        "username": "localadmin",
        "password": "",
        "generated": False,
    }


def test_cloudosd_provision_rejects_unsupported_enabled_sequence_step(
    cloudosd_client,
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import jobs_pg

    cipher = _patch_sequence_cipher(monkeypatch, tmp_path)
    sequence_id = _create_domain_join_sequence(
        pg_conn,
        cipher,
        unsupported_step=True,
    )
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "profile": "",
            "hostname_pattern": "GELL-UNSUPPORTED-{index}",
            "sequence_id": str(sequence_id),
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "not OSDCloud-compatible" in response.json()["detail"]
    assert "run_script" in response.json()["detail"]
    assert [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ] == []


def test_cloudosd_provision_rejects_undecryptable_domain_join_credential(
    cloudosd_client,
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    old_cipher = _patch_sequence_cipher(monkeypatch, tmp_path)
    sequence_id = _create_domain_join_sequence(pg_conn, old_cipher)
    artifact = _create_artifact(pg_conn)
    web_app.CREDENTIAL_KEY.write_bytes(Fernet.generate_key())
    web_app._CIPHER = None

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "profile": "",
            "hostname_pattern": "GELL-BAD-CRED-{index}",
            "sequence_id": str(sequence_id),
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "OSDCloud domain join credential" in response.json()["detail"]
    assert "re-save the credential" in response.json()["detail"]
    assert [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ] == []
    assert cloudosd_pg.list_runs(pg_conn) == []


def test_cloudosd_domain_join_run_waits_for_matching_domain_heartbeat(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg, ts_engine_pg, winpe_token

    artifact = _create_artifact(pg_conn)
    run = cloudosd_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="GELL-AD-VERIFY",
        node="pve",
        storage="local-lvm",
        network_bridge="vmbr0",
        domain_join={
            "enabled": True,
            "source_sequence_id": 42,
            "credential_id": 7,
            "domain_fqdn": "home.gell.one",
            "credential_domain": "HOME",
            "ou_path": "OU=CloudOSD,DC=home,DC=gell,DC=one",
            "acceptable_domain_names": ["home.gell.one", "HOME"],
        },
    )
    bootstrap_token = winpe_token.sign(run_id=run["run_id"], ttl_seconds=300)
    bootstrap = cloudosd_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer(bootstrap_token),
        json={
            "agent_id": "cloudosd-domain-agent",
            "run_id": run["run_id"],
            "phase": "cloudosd",
            "vmid": 245,
        },
    )
    assert bootstrap.status_code == 200, bootstrap.text
    agent_token = bootstrap.json()["agent_token"]

    pending_hb = cloudosd_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(agent_token),
        json={
            "agent_id": "cloudosd-domain-agent",
            "vmid": 245,
            "computer_name": "GELL-AD-VERIFY",
            "current_run_id": run["run_id"],
            "current_phase": "cloudosd",
            "domain_joined": False,
            "domain_name": "WORKGROUP",
        },
    )
    assert pending_hb.status_code == 200, pending_hb.text
    pending = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert pending.status_code == 200, pending.text
    assert pending.json()["run"]["state"] == "full_os_waiting_domain_join"
    steps = ts_engine_pg.list_run_steps(pg_conn, run["run_id"])
    verify_step = next(step for step in steps if step["kind"] == "verify_ad_domain_join")
    assert verify_step["state"] == "pending"

    matched_hb = cloudosd_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(agent_token),
        json={
            "agent_id": "cloudosd-domain-agent",
            "vmid": 245,
            "computer_name": "GELL-AD-VERIFY",
            "current_run_id": run["run_id"],
            "current_phase": "cloudosd",
            "domain_joined": True,
            "domain_name": "home.gell.one",
        },
    )
    assert matched_hb.status_code == 200, matched_hb.text
    waiting_v2 = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert waiting_v2.status_code == 200, waiting_v2.text
    assert waiting_v2.json()["run"]["state"] == "full_os_waiting_v2"
    steps = ts_engine_pg.list_run_steps(pg_conn, run["run_id"])
    verify_step = next(step for step in steps if step["kind"] == "verify_ad_domain_join")
    capture_step = next(step for step in steps if step["kind"] == "capture_autopilot_hash")
    assert verify_step["state"] == "pending"
    assert capture_step["state"] == "pending"

    ts_engine_pg.complete_step(
        pg_conn,
        run_id=run["run_id"],
        step_id=capture_step["id"],
        agent_id="cloudosd-domain-agent",
        status="success",
        message="hash uploaded through AutopilotAgent v2",
    )
    v2_reg = cloudosd_client.post(
        "/osd/v2/agent/register",
        json={
            "run_id": run["run_id"],
            "agent_id": "cloudosd-domain-agent",
            "phase": "full_os",
            "capabilities": ["verify_ad_domain_join"],
        },
    )
    assert v2_reg.status_code == 200, v2_reg.text
    v2_next = cloudosd_client.post(
        "/osd/v2/agent/next",
        headers=_bearer(v2_reg.json()["bearer_token"]),
        json={
            "run_id": run["run_id"],
            "agent_id": "cloudosd-domain-agent",
            "phase": "full_os",
        },
    )
    assert v2_next.status_code == 200, v2_next.text
    action = v2_next.json()["actions"][0]
    assert action["kind"] == "verify_ad_domain_join"
    result = cloudosd_client.post(
        f"/osd/v2/agent/step/{action['step_id']}/result",
        headers=_bearer(v2_reg.json()["bearer_token"]),
        json={
            "run_id": run["run_id"],
            "agent_id": "cloudosd-domain-agent",
            "phase": "full_os",
            "status": "success",
            "message": "AD domain membership verified by AutopilotAgent v2",
            "data": {
                "domain_name": "home.gell.one",
                "domain_joined": True,
            },
        },
    )
    assert result.status_code == 200, result.text
    complete = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert complete.status_code == 200, complete.text
    assert complete.json()["run"]["state"] == "complete"
    steps = ts_engine_pg.list_run_steps(pg_conn, run["run_id"])
    verify_step = next(step for step in steps if step["kind"] == "verify_ad_domain_join")
    assert verify_step["state"] == "done"
    events = cloudosd_pg.list_events(pg_conn, run["run_id"])
    assert {event["event_type"] for event in events} >= {
        "domain_join_pending",
        "domain_join_verified",
    }


def test_cloudosd_run_explains_failed_v2_step_and_operator_retry(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg, ts_engine_pg

    run = _create_cloudosd_run(cloudosd_client, pg_conn, vm_name="GELL-RETRY-001")
    capture_step = next(
        step
        for step in ts_engine_pg.list_run_steps(pg_conn, run["run_id"])
        if step["kind"] == "capture_autopilot_hash"
    )
    pg_conn.execute(
        """
        UPDATE ts_run_plan_steps
        SET state = 'failed',
            attempt = 3,
            claimed_by = 'agent-gell-retry-001',
            last_error = 'hash script exited 1'
        WHERE id = %s
        """,
        (capture_step["id"],),
    )
    pg_conn.execute(
        """
        UPDATE ts_provisioning_runs
        SET state = 'failed',
            phase = 'full_os',
            cursor_step_id = %s,
            last_error = 'hash script exited 1'
        WHERE id = %s
        """,
        (capture_step["id"], run["run_id"]),
    )
    pg_conn.commit()

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    operator = body["v2_operator_status"]
    assert operator["state"] == "blocked"
    assert operator["failed_steps"][0]["kind"] == "capture_autopilot_hash"
    assert "Retry capture_autopilot_hash" in operator["next_actions"]
    enriched_step = next(
        step for step in body["v2_steps"]
        if step["kind"] == "capture_autopilot_hash"
    )
    assert enriched_step["retryable"] is True
    assert enriched_step["wait_reason"] == "failed: hash script exited 1"

    retry = cloudosd_client.post(
        f"/api/cloudosd/runs/{run['run_id']}/v2/steps/{capture_step['id']}/retry",
    )
    assert retry.status_code == 200, retry.text
    retried = retry.json()["step"]
    assert retried["state"] == "pending"
    assert retried["attempt"] == 0
    assert retried["last_error"] is None
    events = cloudosd_pg.list_events(pg_conn, run["run_id"])
    assert any(event["event_type"] == "cloudosd_v2_step_requeued" for event in events)


def test_cloudosd_run_surfaces_hash_upload_assignment_and_enrollment_evidence(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, devices_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "app-id",
        "vault_entra_tenant_id": "tenant-id",
        "vault_entra_app_secret": "secret",
    })
    (hash_dir / "20260514T010101Z-vm245-Gell-245-ABC-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-245-ABC,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    devices_pg.upsert_autopilot([
        {
            "id": "ap-245",
            "serialNumber": "Gell-245-ABC",
            "groupTag": "GellNative",
            "deploymentProfileAssignmentStatus": "pending",
            "enrollmentState": "notContacted",
            "manufacturer": "Dell Inc.",
            "model": "Latitude 7450",
            "displayName": "GELL-245-ABC",
            "lastContactedDateTime": "2026-05-14T01:05:00Z",
        }
    ])
    devices_pg.upsert_intune([
        {
            "id": "intune-245",
            "serialNumber": "Gell-245-ABC",
            "deviceName": "GELL-245-ABC",
            "operatingSystem": "Windows",
            "complianceState": "unknown",
            "managementState": "managed",
            "lastSyncDateTime": "2026-05-14T01:06:00Z",
            "enrolledDateTime": "2026-05-14T01:04:00Z",
        }
    ])
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-245-ABC",
        vm_group_tag="GellNative",
    )
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run['run_id']}/identity",
        json={
            "vmid": 245,
            "vm_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "mac": "52:54:00:aa:bb:ee",
            "node": "pve",
            "computer_name": "Gell-245-ABC",
        },
    ).status_code == 200

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail.status_code == 200, detail.text
    evidence = detail.json()["intune_evidence"]
    assert evidence["tracking"]["expected_group_tag"] == "GellNative"
    assert evidence["tracking"]["source_surface"] == "cloudosd"
    assert evidence["upload"]["status"] == "uploaded"
    assert evidence["upload"]["autopilot_device_id"] == "ap-245"
    assert evidence["hash"]["status"] == "captured"
    assert evidence["hash"]["files"][0]["serial"] == "Gell-245-ABC"
    assert evidence["autopilot"]["status"] == "uploaded"
    assert evidence["autopilot"]["id"] == "ap-245"
    assert evidence["assignment"]["status"] == "pending"
    assert evidence["assignment"]["expected_group_tag"] == "GellNative"
    assert evidence["assignment"]["group_tag"] == "GellNative"
    assert evidence["assignment"]["actual_group_tag"] == "GellNative"
    assert evidence["assignment"]["group_tag_match"] is True
    assert evidence["enrollment"]["status"] == "enrolled"
    assert evidence["enrollment"]["contact_state"] == "enrolled"
    assert evidence["enrollment"]["intune_device_id"] == "intune-245"
    assert evidence["errors"] == []


def test_cloudosd_run_flags_autopilot_group_tag_mismatch(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, devices_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    (hash_dir / "20260514T010101Z-vm246-Gell-246-ABC-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-246-ABC,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    devices_pg.upsert_autopilot([
        {
            "id": "ap-246",
            "serialNumber": "Gell-246-ABC",
            "groupTag": "WrongTag",
            "deploymentProfileAssignmentStatus": "notAssigned",
            "enrollmentState": "notContacted",
            "displayName": "GELL-246-ABC",
        }
    ])
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-246-ABC",
        vm_group_tag="GellNative",
    )
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run['run_id']}/identity",
        json={
            "vmid": 246,
            "vm_uuid": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
            "mac": "52:54:00:aa:bb:ef",
            "node": "pve",
            "computer_name": "Gell-246-ABC",
        },
    ).status_code == 200

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail.status_code == 200, detail.text
    evidence = detail.json()["intune_evidence"]
    assert evidence["upload"]["status"] == "uploaded"
    assert evidence["assignment"]["status"] == "group_tag_mismatch"
    assert evidence["assignment"]["expected_group_tag"] == "GellNative"
    assert evidence["assignment"]["actual_group_tag"] == "WrongTag"
    assert evidence["assignment"]["group_tag_match"] is False
    assert evidence["errors"] == [
        {
            "source": "assignment",
            "code": "group_tag_mismatch",
            "message": "Autopilot group tag WrongTag does not match expected GellNative",
        }
    ]


def test_cloudosd_run_without_expected_group_tag_does_not_mismatch(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, devices_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    (hash_dir / "20260514T020202Z-vm247-Gell-247-ABC-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-247-ABC,,hardware-hash,\n",
        encoding="utf-8",
    )
    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    devices_pg.upsert_autopilot([
        {
            "id": "ap-247",
            "serialNumber": "Gell-247-ABC",
            "groupTag": "None",
            "deploymentProfileAssignmentStatus": "notAssigned",
            "enrollmentState": "notContacted",
            "displayName": "GELL-247-ABC",
        }
    ])
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-247-ABC",
    )
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run['run_id']}/identity",
        json={
            "vmid": 247,
            "vm_uuid": "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
            "mac": "52:54:00:aa:bb:f0",
            "node": "pve",
            "computer_name": "Gell-247-ABC",
        },
    ).status_code == 200

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")

    assert detail.status_code == 200, detail.text
    evidence = detail.json()["intune_evidence"]
    readiness = detail.json()["autopilot_readiness"]
    assert evidence["assignment"]["status"] == "notAssigned"
    assert evidence["assignment"]["expected_group_tag"] == ""
    assert evidence["assignment"]["actual_group_tag"] == "None"
    assert evidence["assignment"]["group_tag_match"] is None
    assert evidence["errors"] == []
    assert readiness["state"] == "imported"
    assert readiness["assignment"]["group_tag_match"] is None


def test_cloudosd_autopilot_readiness_upload_is_run_bound(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "app-id",
        "vault_entra_tenant_id": "tenant-id",
        "vault_entra_app_secret": "secret",
    })
    (hash_dir / "20260514T030303Z-vm247-Gell-247-AP1-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-247-AP1,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-247-AP1",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=247,
        vm_uuid="dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f2",
        node="pve",
        computer_name="Gell-247-AP1",
    )

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail.status_code == 200, detail.text
    readiness = detail.json()["autopilot_readiness"]
    assert readiness["state"] == "hash_captured"
    assert readiness["hash"]["filename"] == "20260514T030303Z-vm247-Gell-247-AP1-osd-v2_hwid.csv"
    assert readiness["next_action"] == "upload_hash"
    assert readiness["upload"]["status"] == "not_started"

    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")
    assert upload.status_code == 202, upload.text
    body = upload.json()
    assert body["ok"] is True
    assert body["queued"] is True
    assert body["autopilot_readiness"]["state"] == "upload_queued"
    assert body["autopilot_readiness"]["upload"]["job_id"]

    job = jobs_pg.get_job(body["autopilot_readiness"]["upload"]["job_id"])
    assert job["job_type"] == "upload_hash"
    assert job["args"]["cloudosd_run_id"] == run["run_id"]
    assert job["args"]["vmid"] == 247
    assert job["args"]["file"] == "20260514T030303Z-vm247-Gell-247-AP1-osd-v2_hwid.csv"
    assert job["args"]["group_tag"] == "GellNative"
    assert job["args"]["credential_source"] == "controller_vault"
    assert job["args"]["credential_boundary"] == "controller"
    assert job["args"]["target_tenant_id"] == "tenant-id"
    assert job["args"]["target_entra_app_id"] == "app-id"
    assert "secret" not in str(job["args"])

    detail_after = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail_after.status_code == 200, detail_after.text
    assert detail_after.json()["autopilot_readiness"]["state"] == "upload_queued"


def test_cloudosd_autopilot_upload_uses_lab_entra_boundary_without_persisting_secret(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg, lab_bubbles_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "controller-app-id",
        "vault_entra_tenant_id": "controller-tenant-id",
        "vault_entra_app_secret": "controller-secret-value",
        "vault_labz1_entra_app_id": "lab-app-id",
        "vault_labz1_entra_tenant_id": "lab-tenant-id",
        "vault_labz1_entra_app_secret": "lab-secret-value",
    })
    (hash_dir / "20260514T031313Z-vm251-Lab-251-AP5-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Lab-251-AP5,,hardware-hash,LabZ1\n",
        encoding="utf-8",
    )
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="LabZ1")
    service = lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="entra",
        service_name="Ivy24 Entra",
        scope="external",
        readiness_state="ready",
        evidence_summary={
            "tenant_id": "lab-tenant-id",
            "client_id": "lab-app-id",
            "tenant_name": "Ivy24",
            "credential_ref": {
                "tenant_id_var": "vault_labz1_entra_tenant_id",
                "app_id_var": "vault_labz1_entra_app_id",
                "app_secret_var": "vault_labz1_entra_app_secret",
            },
        },
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Lab-251-AP5",
        vm_group_tag="LabZ1",
        bubble_id=bubble["id"],
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=251,
        vm_uuid="abababab-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f5",
        node="pve",
        computer_name="Lab-251-AP5",
    )

    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")

    assert upload.status_code == 202, upload.text
    body = upload.json()
    assert body["queued"] is True
    job = jobs_pg.get_job(body["autopilot_readiness"]["upload"]["job_id"])
    assert job["args"]["credential_source"] == "lab_bubble_service"
    assert job["args"]["credential_boundary"] == "lab_bubble"
    assert job["args"]["credential_bubble_id"] == bubble["id"]
    assert job["args"]["credential_service_id"] == service["id"]
    assert job["args"]["target_tenant_id"] == "lab-tenant-id"
    assert job["args"]["target_entra_app_id"] == "lab-app-id"
    assert job["args"]["target_tenant_name"] == "Ivy24"
    cmd_text = " ".join(job["cmd"])
    assert "upload_entra_tenant_id_var=vault_labz1_entra_tenant_id" in cmd_text
    assert "upload_entra_app_id_var=vault_labz1_entra_app_id" in cmd_text
    assert "upload_entra_app_secret_var=vault_labz1_entra_app_secret" in cmd_text
    assert "controller-tenant-id" not in cmd_text
    assert "controller-app-id" not in cmd_text
    assert "controller-secret-value" not in cmd_text
    assert "lab-secret-value" not in str(job)
    events = cloudosd_pg.list_events(pg_conn, run["run_id"])
    queued = next(event for event in events if event["event_type"] == "autopilot_hash_upload_queued")
    assert queued["data"]["target_tenant_id"] == "lab-tenant-id"
    assert queued["data"]["target_entra_app_id"] == "lab-app-id"
    assert "lab-secret-value" not in str(queued)


def test_cloudosd_autopilot_upload_lab_boundary_without_credential_ref_blocks_controller_fallback(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg, lab_bubbles_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "controller-app-id",
        "vault_entra_tenant_id": "controller-tenant-id",
        "vault_entra_app_secret": "controller-secret-value",
    })
    (hash_dir / "20260514T032020Z-vm254-Lab-254-AP8-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Lab-254-AP8,,hardware-hash,LabZ1\n",
        encoding="utf-8",
    )
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="LabZ1 Missing Credential Ref")
    service = lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="entra",
        service_name="Controller-Matching Entra",
        scope="external",
        readiness_state="ready",
        evidence_summary={
            "tenant_id": "controller-tenant-id",
            "client_id": "controller-app-id",
            "tenant_name": "Controller Tenant",
        },
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Lab-254-AP8",
        vm_group_tag="LabZ1",
        bubble_id=bubble["id"],
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=254,
        vm_uuid="dededede-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f8",
        node="pve",
        computer_name="Lab-254-AP8",
    )

    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")

    assert upload.status_code == 202, upload.text
    body = upload.json()
    assert body["queued"] is False
    assert body["reason"] == "entra_credentials_missing"
    assert body["missing"] == ["LAB_ENTRA_CREDENTIAL_REFERENCE"]
    assert body["credential_source"] == "lab_bubble_service"
    assert body["credential_boundary"] == "lab_bubble"
    assert body["credential_bubble_id"] == bubble["id"]
    assert body["credential_service_id"] == service["id"]
    assert body["target_tenant_id"] == "controller-tenant-id"
    assert body["target_entra_app_id"] == "controller-app-id"
    assert jobs_pg.list_jobs(limit=10) == []


def test_cloudosd_autopilot_upload_lab_boundary_missing_secret_does_not_fallback(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg, lab_bubbles_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "controller-app-id",
        "vault_entra_tenant_id": "controller-tenant-id",
        "vault_entra_app_secret": "controller-secret-value",
        "vault_labz1_entra_app_id": "lab-app-id",
        "vault_labz1_entra_tenant_id": "lab-tenant-id",
        "vault_labz1_entra_app_secret": "",
    })
    (hash_dir / "20260514T032323Z-vm252-Lab-252-AP6-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Lab-252-AP6,,hardware-hash,LabZ1\n",
        encoding="utf-8",
    )
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="LabZ1 Missing Secret")
    lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="entra",
        service_name="Ivy24 Entra",
        scope="external",
        readiness_state="ready",
        evidence_summary={
            "tenant_id": "lab-tenant-id",
            "client_id": "lab-app-id",
            "credential_ref": {
                "tenant_id_var": "vault_labz1_entra_tenant_id",
                "app_id_var": "vault_labz1_entra_app_id",
                "app_secret_var": "vault_labz1_entra_app_secret",
            },
        },
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Lab-252-AP6",
        vm_group_tag="LabZ1",
        bubble_id=bubble["id"],
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=252,
        vm_uuid="bcbcbcbc-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f6",
        node="pve",
        computer_name="Lab-252-AP6",
    )

    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")

    assert upload.status_code == 202, upload.text
    body = upload.json()
    assert body["queued"] is False
    assert body["reason"] == "entra_credentials_missing"
    assert body["missing"] == ["ENTRA_APP_SECRET"]
    assert body["credential_source"] == "lab_bubble_service"
    readiness = body["autopilot_readiness"]
    assert readiness["state"] == "upload_not_configured"
    assert jobs_pg.list_jobs(limit=10) == []


def test_cloudosd_autopilot_upload_lab_without_entra_boundary_is_not_queued(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg, lab_bubbles_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "controller-app-id",
        "vault_entra_tenant_id": "controller-tenant-id",
        "vault_entra_app_secret": "controller-secret-value",
    })
    (hash_dir / "20260514T033333Z-vm253-Lab-253-AP7-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Lab-253-AP7,,hardware-hash,LabZ1\n",
        encoding="utf-8",
    )
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="LabZ1 No Entra")
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Lab-253-AP7",
        vm_group_tag="LabZ1",
        bubble_id=bubble["id"],
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=253,
        vm_uuid="cdcdcdcd-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f7",
        node="pve",
        computer_name="Lab-253-AP7",
    )

    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")

    assert upload.status_code == 202, upload.text
    body = upload.json()
    assert body["queued"] is False
    assert body["reason"] == "entra_credentials_missing"
    assert body["missing"] == ["LAB_ENTRA_SERVICE"]
    assert body["credential_source"] == "lab_bubble"
    assert jobs_pg.list_jobs(limit=10) == []


def test_cloudosd_autopilot_readiness_surfaces_failed_upload(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "app-id",
        "vault_entra_tenant_id": "tenant-id",
        "vault_entra_app_secret": "secret",
    })
    (hash_dir / "20260514T040404Z-vm248-Gell-248-AP2-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-248-AP2,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-248-AP2",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=248,
        vm_uuid="eeeeeeee-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f3",
        node="pve",
        computer_name="Gell-248-AP2",
    )
    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")
    assert upload.status_code == 202, upload.text
    job_id = upload.json()["autopilot_readiness"]["upload"]["job_id"]
    (Path(web_app.job_manager.jobs_dir) / f"{job_id}.log").write_text(
        "FAILED: selected hash upload - invalid client secret\n",
        encoding="utf-8",
    )
    pg_conn.execute(
        """
        UPDATE jobs
        SET status = 'failed', exit_code = 1, ended_at = now()
        WHERE id = %s
        """,
        (job_id,),
    )
    pg_conn.commit()

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail.status_code == 200, detail.text
    readiness = detail.json()["autopilot_readiness"]
    assert readiness["state"] == "upload_failed"
    assert readiness["next_action"] == "retry_upload"
    assert readiness["upload"]["status"] == "failed"
    assert "invalid client secret" in readiness["upload"]["error"]
    assert readiness["errors"][0]["code"] == "upload_failed"


def test_cloudosd_autopilot_upload_without_entra_credentials_is_not_queued(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    monkeypatch.setattr(web_app, "_load_proxmox_config", lambda: {
        "vault_entra_app_id": "",
        "vault_entra_tenant_id": "",
        "vault_entra_app_secret": "",
    })
    (hash_dir / "20260514T041414Z-vm250-Gell-250-AP4-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-250-AP4,,hardware-hash,\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-250-AP4",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=250,
        vm_uuid="efefefef-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f4",
        node="pve",
        computer_name="Gell-250-AP4",
    )

    upload = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/upload")

    assert upload.status_code == 202, upload.text
    body = upload.json()
    assert body["ok"] is True
    assert body["queued"] is False
    assert body["reason"] == "entra_credentials_missing"
    readiness = body["autopilot_readiness"]
    assert readiness["state"] == "upload_not_configured"
    assert readiness["next_action"] == "configure_entra"
    assert readiness["upload"]["status"] == "not_configured"
    assert readiness["errors"] == []
    assert jobs_pg.list_jobs(limit=10) == []


def test_cloudosd_autopilot_readiness_sync_reconciles_import_and_assignment(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    (hash_dir / "20260514T050505Z-vm249-Gell-249-AP3-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-249-AP3,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-249-AP3",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=249,
        vm_uuid="ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f4",
        node="pve",
        computer_name="Gell-249-AP3",
    )

    def fake_graph_all(path):
        if path == "/deviceManagement/windowsAutopilotDeviceIdentities":
            return [{
                "id": "ap-249",
                "serialNumber": "Gell-249-AP3",
                "groupTag": "GellNative",
                "deploymentProfileAssignmentStatus": "assignedInSync",
                "enrollmentState": "enrolled",
                "lastContactedDateTime": "2026-05-14T05:10:00Z",
                "displayName": "GELL-249-AP3",
            }]
        if path == "/deviceManagement/managedDevices":
            return [{
                "id": "intune-249",
                "serialNumber": "Gell-249-AP3",
                "deviceName": "GELL-249-AP3",
                "operatingSystem": "Windows",
                "managementState": "managed",
                "lastSyncDateTime": "2026-05-14T05:11:00Z",
                "enrolledDateTime": "2026-05-14T05:09:00Z",
            }]
        if path == "/devices":
            return []
        raise AssertionError(path)

    graph_calls = []
    monkeypatch.setattr(
        web_app,
        "_graph_api",
        lambda path, method="GET", json_body=None: graph_calls.append((path, method)) or {},
    )
    monkeypatch.setattr(web_app, "_graph_api_all", fake_graph_all)

    synced = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/autopilot/sync")
    assert synced.status_code == 200, synced.text
    assert graph_calls == [("/deviceManagement/windowsAutopilotSettings/sync", "POST")]
    readiness = synced.json()["autopilot_readiness"]
    assert readiness["state"] == "enrolled"
    assert readiness["autopilot"]["device_id"] == "ap-249"
    assert readiness["assignment"]["status"] == "assignedInSync"
    assert readiness["assignment"]["group_tag_match"] is True
    assert readiness["enrollment"]["status"] == "enrolled"
    assert readiness["contact"]["state"] == "enrolled"
    assert readiness["errors"] == []


def test_cloudosd_autopilot_readiness_tracks_imported_before_assignment(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, devices_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    (hash_dir / "20260514T060606Z-vm250-Gell-250-AP4-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-250-AP4,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-250-AP4",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=250,
        vm_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f5",
        node="pve",
        computer_name="Gell-250-AP4",
    )
    devices_pg.upsert_autopilot([
        {
            "id": "ap-250",
            "serialNumber": "Gell-250-AP4",
            "groupTag": "GellNative",
            "deploymentProfileAssignmentStatus": "notAssigned",
            "enrollmentState": "notContacted",
            "displayName": "GELL-250-AP4",
        }
    ])

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")

    assert detail.status_code == 200, detail.text
    readiness = detail.json()["autopilot_readiness"]
    assert readiness["state"] == "imported"
    assert readiness["next_action"] == "wait_for_assignment"
    assert readiness["autopilot"]["device_id"] == "ap-250"
    assert readiness["assignment"]["status"] == "notAssigned"
    assert readiness["assignment"]["group_tag_match"] is True


def test_cloudosd_autopilot_readiness_ignores_stale_same_vmid_serial(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_endpoints, cloudosd_pg, devices_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    stale_hash = hash_dir / "20260513T070707Z-vm251-OLD251-osd-v2_hwid.csv"
    current_hash = hash_dir / "20260514T070707Z-vm251-NEW251-osd-v2_hwid.csv"
    stale_hash.write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "OLD251,,old-hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    current_hash.write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "NEW251,,new-hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    os.utime(stale_hash, (1_768_300_000, 1_768_300_000))
    os.utime(current_hash, (1_768_400_000, 1_768_400_000))
    devices_pg.upsert_autopilot([
        {
            "id": "ap-old-251",
            "serialNumber": "OLD251",
            "groupTag": "GellNative",
            "deploymentProfileAssignmentStatus": "assignedInSync",
            "enrollmentState": "notContacted",
            "displayName": "GELL-251-OLD",
        }
    ])
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-251-E2E",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=251,
        vm_uuid="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        mac="52:54:00:aa:bb:f6",
        node="pve",
        computer_name="Gell-251-E2E",
    )
    cloudosd_pg.record_autopilot_upload_attempt(
        pg_conn,
        run_id=run["run_id"],
        job_id="upload-251",
        hash_filename=current_hash.name,
        expected_group_tag="GellNative",
        status="complete",
    )

    run_row = cloudosd_pg.get_run(pg_conn, run["run_id"])
    readiness = cloudosd_endpoints.autopilot_readiness_for_run(
        pg_conn,
        run_row,
        allow_auto_sync=False,
    )

    assert readiness["hash"]["filename"] == current_hash.name
    assert readiness["hash"]["serial"] == "NEW251"
    assert readiness["state"] == "upload_submitted"
    assert readiness["next_action"] == "sync_intune"
    assert readiness["autopilot"]["device_id"] is None
    assert readiness["autopilot"]["serial"] is None
    assert readiness["assignment"]["status"] == "waiting_for_upload"


def test_cloudosd_autopilot_auto_sync_429_backs_off(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    import requests
    from web import app as web_app, cloudosd_endpoints, cloudosd_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    hash_file = hash_dir / "20260514T080808Z-vm252-Gell-252-AP5-osd-v2_hwid.csv"
    hash_file.write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-252-AP5,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-252-AP5",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=252,
        vm_uuid="cccccccc-dddd-eeee-ffff-aaaaaaaaaaaa",
        mac="52:54:00:aa:bb:f7",
        node="pve",
        computer_name="Gell-252-AP5",
    )
    cloudosd_pg.record_autopilot_upload_attempt(
        pg_conn,
        run_id=run["run_id"],
        job_id="upload-252",
        hash_filename=hash_file.name,
        expected_group_tag="GellNative",
        status="complete",
    )

    calls = []

    def throttled_graph(path, method="GET", json_body=None):
        calls.append((path, method))
        response = requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "120"
        response.url = f"https://graph.microsoft.com/beta{path}"
        raise requests.HTTPError("429 Too Many Requests", response=response)

    monkeypatch.setattr(web_app, "_graph_api", throttled_graph)
    run_row = cloudosd_pg.get_run(pg_conn, run["run_id"])
    readiness = cloudosd_endpoints.autopilot_readiness_for_run(pg_conn, run_row)

    assert readiness["state"] == "sync_throttled"
    assert readiness["next_action"] == "wait_for_graph_backoff"
    assert "Graph throttled, retrying later" in readiness["detail"]
    assert calls == [("/deviceManagement/windowsAutopilotSettings/sync", "POST")]
    events = cloudosd_pg.list_events(pg_conn, run["run_id"])
    assert [event["event_type"] for event in events].count("autopilot_cache_auto_sync_attempted") == 1
    assert [event["event_type"] for event in events].count("autopilot_cache_sync_throttled") == 1

    second = cloudosd_endpoints.autopilot_readiness_for_run(pg_conn, run_row)
    assert second["state"] == "sync_throttled"
    assert calls == [("/deviceManagement/windowsAutopilotSettings/sync", "POST")]


def test_cloudosd_readiness_watcher_syncs_once_and_advances_state(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, cloudosd_endpoints, cloudosd_pg, devices_pg, monitor_main

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    hash_file = hash_dir / "20260514T090909Z-vm253-Gell-253-AP6-osd-v2_hwid.csv"
    hash_file.write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-253-AP6,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-253-AP6",
        vm_group_tag="GellNative",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=253,
        vm_uuid="dddddddd-eeee-ffff-aaaa-bbbbbbbbbbbb",
        mac="52:54:00:aa:bb:f8",
        node="pve",
        computer_name="Gell-253-AP6",
    )
    cloudosd_pg.record_autopilot_upload_attempt(
        pg_conn,
        run_id=run["run_id"],
        job_id="upload-253",
        hash_filename=hash_file.name,
        expected_group_tag="GellNative",
        status="complete",
    )

    sync_calls = []

    def fake_sync(conn=None):
        sync_calls.append(True)
        devices_pg.upsert_autopilot([
            {
                "id": "ap-253",
                "serialNumber": "Gell-253-AP6",
                "groupTag": "GellNative",
                "deploymentProfileAssignmentStatus": "assignedInSync",
                "enrollmentState": "enrolled",
                "lastContactedDateTime": "2026-05-14T09:12:00Z",
            }
        ])
        devices_pg.upsert_intune([
            {
                "id": "intune-253",
                "serialNumber": "Gell-253-AP6",
                "deviceName": "GELL-253-AP6",
                "operatingSystem": "Windows",
                "managementState": "managed",
                "lastSyncDateTime": "2026-05-14T09:12:30Z",
                "enrolledDateTime": "2026-05-14T09:11:00Z",
            }
        ])
        if conn is not None:
            cloudosd_pg.clear_graph_sync_backoff(conn)
        return {"autopilot": 1, "intune": 1, "entra": 0}

    monkeypatch.setattr(cloudosd_endpoints, "_sync_cloud_devices_from_graph", fake_sync)
    result = monitor_main._do_cloudosd_readiness_tick(limit=10)

    assert result["synced"] is True
    assert result["sync_needed"] == 1
    assert len(sync_calls) == 1
    persisted = cloudosd_pg.get_autopilot_readiness(pg_conn, run["run_id"])
    assert persisted["state"] == "enrolled"
    assert persisted["autopilot_device_id"] == "ap-253"
    assert persisted["assignment_status"] == "assignedInSync"


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


def test_cloudosd_warning_failed_suffix_event_does_not_mark_run_failed(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "CLOUDOSD-WARN",
            "architecture": "amd64",
            "vm_memory_mb": 8192,
        },
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    assert cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/identity",
        json={
            "vmid": 224,
            "vm_uuid": "33333333-4444-5555-6666-777777777777",
            "mac": "52:54:00:aa:bb:ee",
            "node": "pve",
        },
    ).status_code == 200
    registered = cloudosd_client.post(
        "/api/cloudosd/pe/register",
        json={
            "vm_uuid": "33333333-4444-5555-6666-777777777777",
            "mac": "52:54:00:aa:bb:ee",
            "architecture": "amd64",
            "build_sha": "cloudosdtest",
        },
    )
    assert registered.status_code == 200, registered.text

    event = cloudosd_client.post(
        f"/api/cloudosd/runs/{run_id}/events",
        headers=_bearer(registered.json()["bearer_token"]),
        json={
            "phase": "first_boot",
            "event_type": "firstboot_oobe_bootstrap_session_logoff_failed",
            "severity": "warning",
            "message": "No User exists for *",
        },
    )
    assert event.status_code == 200, event.text

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["state"] != "failed"

    row = pg_conn.execute(
        "SELECT state, last_error FROM ts_provisioning_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row["state"] != "failed"
    assert row["last_error"] is None


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


def test_cloudosd_agent_msi_resolves_from_host_repo_mount(tmp_path, monkeypatch):
    from web import cloudosd_endpoints

    host_repo = tmp_path / "host-repo"
    msi_path = host_repo / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi"
    _write_fake_msi(msi_path)
    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    monkeypatch.setenv("HOST_REPO_MOUNT", str(host_repo))
    monkeypatch.setattr(cloudosd_endpoints, "_APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(cloudosd_endpoints, "_REPO_ROOT", tmp_path / "repo")

    assert cloudosd_endpoints._asset_path("autopilotagent.msi") == msi_path


def test_cloudosd_agent_msi_prefers_setup_registry_x64_artifact(tmp_path, monkeypatch):
    from web import cloudosd_endpoints, setup_artifacts

    host_repo = tmp_path / "host-repo"
    placeholder = host_repo / "autopilot-agent" / "artifacts" / "AutopilotAgent.msi"
    placeholder.parent.mkdir(parents=True)
    placeholder.write_bytes(b"placeholder")
    legacy_msi = tmp_path / "app" / "output" / "cloudosd" / "AutopilotAgent.msi"
    _write_fake_msi(legacy_msi)
    setup_msi = tmp_path / "setup-artifacts" / "agent-msi" / "AutopilotAgent-0.1.2-win-x64.msi"
    _write_fake_msi(setup_msi)

    monkeypatch.delenv("AUTOPILOT_AGENT_MSI_PATH", raising=False)
    monkeypatch.setenv("HOST_REPO_MOUNT", str(host_repo))
    monkeypatch.setattr(cloudosd_endpoints, "_APP_ROOT", tmp_path / "app")
    monkeypatch.setattr(cloudosd_endpoints, "_REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(
        setup_artifacts,
        "list_artifacts",
        lambda kind=None: [{
            "kind": "agent-msi",
            "filename": setup_msi.name,
            "path": str(setup_msi),
        }],
    )

    assert cloudosd_endpoints._asset_path("autopilotagent.msi") == setup_msi


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


def test_cloudosd_provision_job_is_normalized_into_playbook_milestones(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, jobs_pg

    monkeypatch.setattr(
        web_app,
        "_read_json_file",
        lambda _path: {
            "osdeploy_blank_template_vmid": 9001,
            "virtio_iso_volid": "local:iso/virtio-win.iso",
        },
    )
    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(artifact["id"], vm_name="CLOUDOSD-PLAYBOOK"),
    ).json()

    provision = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/provision")
    assert provision.status_code == 202, provision.text

    events = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}/events")

    assert events.status_code == 200
    body = events.json()
    playbook_events = body["milestone_groups"]["Proxmox playbook"]
    assert playbook_events
    assert playbook_events[-1]["event_type"] == "provision_job_status"
    assert "provision_cloudosd" in playbook_events[-1]["message"]
    job = jobs_pg.get_job(provision.json()["job_id"])
    assert job["args"]["cloudosd_blank_template_vmid"] == 9001
    assert job["args"]["winpe_blank_template_vmid"] == 9001


def test_cloudosd_lifecycle_events_sync_v2_task_engine_progress(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg, ts_engine_pg, winpe_token

    artifact = _create_artifact(pg_conn)
    run = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(artifact["id"], vm_name="CLOUDOSD-PROGRESS"),
    ).json()
    before = [
        row for row in ts_engine_pg.list_runs(pg_conn)
        if row["id"] == run["run_id"]
    ][0]
    pg_conn.commit()
    assert before["done_count"] == 0
    assert before["step_count"] == 7

    token = winpe_token.sign(run_id=run["run_id"], ttl_seconds=3600)
    for event in (
        {
            "phase": "pe",
            "event_type": "osdcloud_start",
            "message": "Starting OSDCloud deploy",
        },
        {
            "phase": "offline_validation",
            "event_type": "offline_validation_ok",
            "message": "Offline Windows validation passed",
        },
        {
            "phase": "setupcomplete",
            "event_type": "setupcomplete_chained",
            "message": "SetupComplete first-boot chain staged",
        },
        {
            "phase": "pe",
            "event_type": "cloudosd_pe_complete",
            "message": "CloudOSD PE phase complete",
        },
    ):
        response = cloudosd_client.post(
            f"/api/cloudosd/runs/{run['run_id']}/events",
            headers=_bearer(token),
            json=event,
        )
        assert response.status_code == 200, response.text

    cloudosd_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        heartbeat_at=cloudosd_pg._now(),
    )

    after = [
        row for row in ts_engine_pg.list_runs(pg_conn)
        if row["id"] == run["run_id"]
    ][0]
    pg_conn.commit()
    assert after["done_count"] == 6
    assert after["step_count"] == 7
    assert after["state"] == "full_os_waiting_v2"

    task_engine = cloudosd_client.get("/api/task-engine/page")
    assert task_engine.status_code == 200
    runs = {row["id"]: row for row in task_engine.json()["runs"]}
    assert runs[run["run_id"]]["done_count"] == 6
    assert runs[run["run_id"]]["step_count"] == 7
    assert runs[run["run_id"]]["state"] == "full_os_waiting_v2"


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

    response = cloudosd_client.get(f"/cloudosd/runs/{run['run_id']}", follow_redirects=False)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == f"/react/cloudosd/runs/{run['run_id']}"

    response = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}/page")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["run_id"] == run["run_id"]
    assert body["run"]["local_admin"]["username"] == "localadmin"
    assert body["run"]["local_admin"]["password"] == run["local_admin"]["password"]
    assert [event["message"] for event in body["events"]][-2:] == [
        "CloudOSD PE bridge registered",
        "Starting OSDCloud deploy",
    ]


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
    assert body["network_targets"][0]["value"] == "vmbr0"
    assert body["network_targets"][0]["kind"] == "bridge"
    assert body["source"] == "configured"


def test_cloudosd_proxmox_options_include_sdn_vnet_targets(
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

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
            "/cluster/sdn/vnets": [
                {"vnet": "lab101", "zone": "lab-simple", "alias": "Lab 101"}
            ],
        }
        if path not in values:
            raise AssertionError(path)
        return values[path]

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)

    response = cloudosd_client.get("/api/cloudosd/proxmox/options")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bridges"] == ["vmbr0"]
    assert any(
        target["kind"] == "bridge" and target["value"] == "vmbr0"
        for target in body["network_targets"]
    )
    assert any(
        target["kind"] == "sdn_vnet"
        and target["value"] == "lab101"
        and target["zone"] == "lab-simple"
        for target in body["network_targets"]
    )


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
    assert caps["provision_cloudosd"] == 4
    assert caps["upload_hash"] == 5
    assert caps["provision_clone"] == 3

    assert auth.is_exempt_path("/api/cloudosd/pe/register")
    assert auth.is_exempt_path("/api/cloudosd/pe/package/run-1")
    assert auth.is_exempt_path("/api/cloudosd/assets/PVEAutopilot-FirstBoot.ps1")
    assert auth.is_exempt_path("/api/cloudosd/runs/run-1")
    assert auth.is_exempt_path("/api/cloudosd/runs/run-1/identity")
    assert auth.is_exempt_path("/api/cloudosd/runs/run-1/events")
    assert not auth.is_exempt_path("/api/cloudosd/runs")
    assert not auth.is_exempt_path("/api/cloudosd/runs/run-1/provision")


def test_provision_page_exposes_cloudosd_boot_mode_and_batch_fields(
    cloudosd_client,
    pg_conn,
):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.get("/provision", follow_redirects=False)

    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/react/provision"

    response = cloudosd_client.get("/api/provision/page")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["defaults"]["count"] == 1
    assert body["defaults"]["cores"] >= 1
    assert any(row["id"] == artifact["id"] for row in body["cloudosd_ready_artifacts"])
    assert set(body["cloudosd_options"]) >= {"nodes", "storages", "bridges"}
    assert body["cloudosd_options"]["storages"]["iso"]
    assert body["cloudosd_options"]["storages"]["disk"]


def test_provision_page_exposes_only_cloudosd_domain_join_sequences(
    cloudosd_client,
    pg_conn,
):
    from web import sequences_pg

    _create_artifact(pg_conn)
    cloudosd_join_id = sequences_pg.create_sequence(
        None,
        name="CloudOSD AD Domain Join UI",
        description="CloudOSD-only domain join intent",
        target_os="windows",
        steps=[
            {
                "step_type": "join_ad_domain",
                "params": {"credential_id": 7, "ou_path": ""},
                "enabled": True,
            },
        ],
    )
    sequences_pg.create_sequence(
        None,
        name="Clone and WinPE Windows UI",
        description="Generic Windows sequence",
        target_os="windows",
        is_default=True,
        steps=[
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    )
    sequences_pg.create_sequence(
        None,
        name="WinPE E2E Smoke UI",
        description="WinPE-only smoke sequence",
        target_os="windows",
        steps=[],
    )
    sequences_pg.create_sequence(
        None,
        name="Ubuntu Plain Legacy UI",
        description="Legacy Ubuntu sequence should use Ubuntu selector",
        target_os="ubuntu",
        steps=[],
    )

    response = cloudosd_client.get("/api/provision/page")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["sequences"]) == 1
    assert body["sequences"][0] == {
        **body["sequences"][0],
        "id": cloudosd_join_id,
        "name": "CloudOSD AD Domain Join UI",
        "description": "CloudOSD-only domain join intent",
        "is_default": False,
        "produces_autopilot_hash": False,
        "target_os": "windows",
        "hash_capture_phase": "oobe",
        "step_count": 1,
        "boot_modes": ["cloudosd"],
    }
    assert response.json()["default_sequence_id"] == ""


def test_provision_rejects_sequence_for_wrong_boot_mode(
    cloudosd_client,
    pg_conn,
):
    from web import sequences_pg

    artifact = _create_artifact(pg_conn)
    winpe_seq = sequences_pg.create_sequence(
        None,
        name="WinPE E2E Smoke UI",
        description="WinPE-only smoke sequence",
        target_os="windows",
        steps=[],
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "profile": "dell",
            "sequence_id": str(winpe_seq),
            "artifact_id": artifact["id"],
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "hostname_pattern": "bad-{index}",
        },
    )

    assert response.status_code == 400, response.text
    assert "not available for OSDCloud boot mode" in response.json()["detail"]


def test_provision_cloudosd_batch_auto_selects_ready_artifact_when_form_omits_id(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, jobs_pg

    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "fake-root-pw",
        },
    )
    monkeypatch.setattr(
        web_app,
        "_proxmox_api",
        lambda path, *args, **kwargs: {
            "/cluster/nextid": 100,
            "/cluster/resources?type=vm": [],
            "/cluster/status": [
                {"type": "node", "name": "pve", "ip": "10.0.0.2"},
            ],
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
        }[path],
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "profile": "lenovo-t14",
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "serial_prefix": "GELL",
            "group_tag": "GellNative",
            "hostname_pattern": "GELL-OSD-{index}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
            "os_version": "Windows 11 25H2",
            "os_edition": "Enterprise",
            "os_activation": "Volume",
            "os_language": "en-us",
            "tpm_enabled": "on",
            "secure_boot": "on",
            "driver_pack_policy": "None",
            "outbound_policy_mode": "blocked",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    job = next(
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    )
    assert job["args"]["cloudosd_artifact_volid"] == artifact["proxmox_volid"]


def test_provision_cloudosd_batch_links_runs_to_selected_bubble(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, lab_bubbles_pg

    artifact = _create_artifact(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(
        pg_conn,
        name="labz1",
        domain_name="home.gell.one",
        netbios_name="HOME",
        cidr="192.168.16.0/24",
        planned_bridge="lab101",
    )
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "fake-root-pw",
        },
    )
    monkeypatch.setattr(
        web_app,
        "_proxmox_api",
        lambda path, *args, **kwargs: {
            "/cluster/nextid": 100,
            "/cluster/resources?type=vm": [],
            "/cluster/status": [
                {"type": "node", "name": "pve", "ip": "10.0.0.2"},
            ],
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/cluster/sdn/vnets": [{"vnet": "lab101", "zone": "lab-simple"}],
            "/nodes/pve/qemu": [],
        }[path],
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "lenovo-t14",
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "serial_prefix": "LAB",
            "group_tag": "labz1",
            "hostname_pattern": "labz1-{index}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "lab101",
            "os_version": "Windows 11 25H2",
            "os_edition": "Enterprise",
            "os_activation": "Volume",
            "os_language": "en-us",
            "tpm_enabled": "on",
            "secure_boot": "on",
            "driver_pack_policy": "None",
            "outbound_policy_mode": "blocked",
            "bubble_id": bubble["id"],
            "asset_role": "workstation",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assets = lab_bubbles_pg.list_assets(pg_conn, bubble["id"])
    assert len(assets) == 1
    assert assets[0]["asset_type"] == "vm"
    assert assets[0]["asset_role"] == "workstation"
    assert assets[0]["membership_state"] == "provisioning"
    assert assets[0]["vmid"] == 100
    assert assets[0]["run_id"]


def test_provision_cloudosd_batch_creates_runs_and_jobs(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "fake-root-pw",
        },
    )
    monkeypatch.setattr(
        web_app,
        "_proxmox_api",
        lambda path, *args, **kwargs: {
            "/cluster/nextid": 100,
            "/cluster/resources?type=vm": [],
            "/cluster/status": [
                {"type": "node", "name": "pve", "ip": "10.0.0.2"},
            ],
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
        }[path],
    )
    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "lenovo-t14",
            "count": "3",
            "cores": "6",
            "memory_mb": "12288",
            "disk_size_gb": "96",
            "serial_prefix": "GELL",
            "group_tag": "GellNative",
            "hostname_pattern": "GELL-OSD-{index}",
            "chassis_type_override": "31",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
            "os_version": "Windows 11 25H2",
            "os_edition": "Enterprise",
            "os_activation": "Volume",
            "os_language": "en-us",
            "tpm_enabled": "on",
            "secure_boot": "on",
            "driver_pack_policy": "None",
            "outbound_policy_mode": "blocked",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/osdcloud")
    jobs = [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ]
    assert len(jobs) == 3
    names = {job["args"]["vm_name"] for job in jobs}
    assert names == {"GELL-OSD-01", "GELL-OSD-02", "GELL-OSD-03"}
    for job in jobs:
        args = job["args"]
        assert args["cloudosd_artifact_volid"] == artifact["proxmox_volid"]
        assert args["vm_cores"] == 6
        assert args["vm_memory_mb"] == 12288
        assert args["vm_disk_size_gb"] == 96
        assert args["vm_group_tag"] == "GellNative"
        assert args["vm_oem_profile"] == "lenovo-t14"
        assert args["chassis_type_override"] == 31
        assert "_proxmox_root_ticket" not in args
        assert "_proxmox_root_csrf_token" not in args
        assert "_skip_chassis_type_smbios_file" not in args

    runs = cloudosd_pg.list_runs(pg_conn, limit=10)
    assert {run["requested_vm_name"] for run in runs} >= names
    for run in runs:
        if run["requested_vm_name"] in names:
            assert run["vm_group_tag"] == "GellNative"
            assert run["vm_oem_profile"] == "lenovo-t14"
            assert run["chassis_type_override"] == 31
            assert run["source_surface"] == "provision"


def test_provision_cloudosd_short_hostname_keeps_full_group_tag(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "fake-root-pw",
        },
    )
    monkeypatch.setattr(
        web_app,
        "_proxmox_api",
        lambda path, *args, **kwargs: {
            "/cluster/nextid": 105,
            "/cluster/resources?type=vm": [],
            "/cluster/status": [
                {"type": "node", "name": "pve", "ip": "10.0.0.2"},
            ],
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
        }[path],
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "generic-desktop",
            "count": "2",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "serial_prefix": "",
            "group_tag": "NTTENANT01-Desktop",
            "hostname_pattern": "ntt01-{index}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
            "os_version": "Windows 11 25H2",
            "os_edition": "Enterprise",
            "os_activation": "Volume",
            "os_language": "en-us",
            "tpm_enabled": "on",
            "secure_boot": "on",
            "driver_pack_policy": "None",
            "outbound_policy_mode": "blocked",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    jobs = [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ]
    assert len(jobs) == 2
    assert {job["args"]["vm_group_tag"] for job in jobs} == {"NTTENANT01-Desktop"}
    assert {job["args"]["hostname_pattern"] for job in jobs} == {"ntt01-01", "ntt01-02"}
    assert all(len(job["args"]["hostname_pattern"]) <= 15 for job in jobs)

    runs = cloudosd_pg.list_runs(pg_conn, limit=10)
    created = [
        run for run in runs
        if run["requested_vm_name"] in {"ntt01-01", "ntt01-02"}
    ]
    assert len(created) == 2
    assert {run["vm_group_tag"] for run in created} == {"NTTENANT01-Desktop"}
    assert {run["expected_computer_name"] for run in created} == {"ntt01-01", "ntt01-02"}
    assert all(len(run["expected_computer_name"]) <= 15 for run in created)


def test_provision_cloudosd_batch_reserves_unique_vmids_without_vmid_token(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "fake-root-pw",
        },
    )

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/cluster/nextid": 100,
            "/cluster/resources?type=vm": [{"vmid": 100}, {"vmid": 102}],
            "/cluster/status": [
                {"type": "node", "name": "pve", "ip": "10.0.0.2"},
            ],
            "/nodes": [{"node": "pve"}],
            "/storage": [
                {"storage": "local", "content": "iso"},
                {"storage": "local-lvm", "content": "images"},
            ],
            "/nodes/pve/network": [{"iface": "vmbr0", "type": "bridge"}],
            "/nodes/pve/qemu": [],
        }
        if path not in values:
            raise AssertionError(path)
        return values[path]

    monkeypatch.setattr(web_app, "_proxmox_api", fake_proxmox_api)
    cloudosd_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="AD-RESERVED",
        requested_vmid=101,
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "generic-desktop",
            "count": "4",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "serial_prefix": "AD",
            "group_tag": "Domain",
            "hostname_pattern": "AD-{serial}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
            "os_version": "Windows 11 25H2",
            "os_edition": "Enterprise",
            "os_activation": "Volume",
            "os_language": "en-us",
            "tpm_enabled": "on",
            "secure_boot": "on",
            "driver_pack_policy": "None",
            "outbound_policy_mode": "blocked",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    jobs = [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ]
    assert len(jobs) == 4
    requested_vmids = sorted(job["args"].get("requested_vmid") for job in jobs)
    assert requested_vmids == [103, 104, 105, 106]
    assert all(job["args"]["vm_name"].startswith("AD-") for job in jobs)

    runs = cloudosd_pg.list_runs(pg_conn, limit=10)
    run_vmids = sorted(
        run["requested_vmid"]
        for run in runs
        if run["requested_vm_name"] in {job["args"]["vm_name"] for job in jobs}
    )
    assert run_vmids == [103, 104, 105, 106]


def test_provision_page_shows_cloudosd_batch_progress_rows(
    cloudosd_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app, agent_telemetry_pg, cloudosd_pg, devices_pg

    hash_dir = tmp_path / "hashes"
    hash_dir.mkdir()
    monkeypatch.setattr(web_app, "HASH_DIR", hash_dir)
    (hash_dir / "20260514T020202Z-vm251-Gell-251-OSD1-osd-v2_hwid.csv").write_text(
        "Device Serial Number,Windows Product ID,Hardware Hash,Group Tag\n"
        "Gell-251-OSD1,,hardware-hash,GellNative\n",
        encoding="utf-8",
    )
    devices_pg.reset_for_tests(pg_conn)
    devices_pg.init(pg_conn)
    devices_pg.upsert_autopilot([
        {
            "id": "ap-251",
            "serialNumber": "Gell-251-OSD1",
            "groupTag": "GellNative",
            "deploymentProfileAssignmentStatus": "assignedInSync",
            "enrollmentState": "enrolled",
            "displayName": "GELL-251-OSD1",
            "lastContactedDateTime": "2026-05-14T02:10:00Z",
        }
    ])
    devices_pg.upsert_intune([
        {
            "id": "intune-251",
            "serialNumber": "Gell-251-OSD1",
            "deviceName": "GELL-251-OSD1",
            "operatingSystem": "Windows",
            "managementState": "managed",
            "lastSyncDateTime": "2026-05-14T02:12:00Z",
            "enrolledDateTime": "2026-05-14T02:09:00Z",
        }
    ])
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-251-OSD1",
        vm_group_tag="GellNative",
        source_surface="provision",
    )
    cloudosd_pg.set_run_identity(
        pg_conn,
        run_id=run["run_id"],
        vmid=251,
        vm_uuid="cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
        mac="52:54:00:aa:bb:f1",
        node="pve",
        computer_name="Gell-251-OSD1",
    )
    cloudosd_pg.mark_pe_registered(pg_conn, run_id=run["run_id"])
    cloudosd_pg.mark_osdcloud_started(pg_conn, run_id=run["run_id"])
    cloudosd_pg.mark_osdcloud_finished(pg_conn, run_id=run["run_id"])
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-251",
        token="token-251",
        vmid=251,
        serial_number="Gell-251-OSD1",
        computer_name="GELL-251-OSD1",
        created_from_run_id=run["run_id"],
    )
    heartbeat = agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-251",
        payload={
            "vmid": 251,
            "computer_name": "GELL-251-OSD1",
            "serial_number": "Gell-251-OSD1",
            "current_run_id": run["run_id"],
            "current_phase": "cloudosd",
        },
    )
    cloudosd_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        heartbeat_at=heartbeat["received_at"],
        heartbeat=heartbeat,
    )
    cloudosd_pg.ts_engine_pg.mark_steps_done_by_kind(
        pg_conn,
        run_id=run["run_id"],
        kinds={"capture_autopilot_hash", "wait_agent_heartbeat"},
        agent_id="agent-251",
    )

    api = cloudosd_client.get("/api/cloudosd/provision/progress")
    assert api.status_code == 200, api.text
    payload = api.json()
    assert payload["summary"]["total"] >= 1
    assert payload["summary"]["deployed"] >= 1
    assert payload["summary"]["uploaded"] >= 1
    assert payload["summary"]["assigned"] >= 1
    assert payload["summary"]["contacted_enrolled"] >= 1
    rows = payload["runs"]
    row = next(item for item in rows if item["run_id"] == run["run_id"])
    assert row["vm_name"] == "Gell-251-OSD1"
    assert row["milestones"]["vm_created"]["state"] == "done"
    assert row["milestones"]["pe_registered"]["state"] == "done"
    assert row["milestones"]["osdcloud_done"]["state"] == "done"
    assert row["milestones"]["agent_heartbeat"]["state"] == "done"
    assert row["milestones"]["v2_steps_done"]["state"] == "done"
    assert row["milestones"]["intune_state"]["state"] == "done"
    assert row["autopilot_readiness"]["state"] == "enrolled"
    assert row["autopilot_readiness"]["autopilot"]["device_id"] == "ap-251"
    assert row["autopilot_readiness"]["assignment"]["status"] == "assignedInSync"
    assert row["autopilot_readiness"]["enrollment"]["status"] == "enrolled"
    assert row["intune_evidence"]["upload"]["autopilot_device_id"] == "ap-251"
    assert row["intune_evidence"]["assignment"]["status"] == "assignedInSync"
    assert row["intune_evidence"]["enrollment"]["status"] == "enrolled"

    page = cloudosd_client.get("/api/provision/page")
    assert page.status_code == 200, page.text
    rows = page.json()["cloudosd_batch_progress"]["runs"]
    row = next(item for item in rows if item["run_id"] == run["run_id"])
    assert row["vm_name"] == "Gell-251-OSD1"
    assert row["milestones"]["vm_created"]["state"] == "done"
    assert row["milestones"]["pe_registered"]["state"] == "done"
    assert row["milestones"]["osdcloud_done"]["state"] == "done"
    assert row["milestones"]["agent_heartbeat"]["state"] == "done"
    assert row["milestones"]["v2_steps_done"]["state"] == "done"
    assert row["autopilot_readiness"]["state"] == "enrolled"


def test_cloudosd_archive_hides_run_from_default_history_but_preserves_detail(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg

    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-ARCHIVE-01",
    )
    cloudosd_pg.append_event(
        pg_conn,
        run_id=run["run_id"],
        phase="controller",
        event_type="test_evidence",
        message="evidence should survive archive",
    )

    archive = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/archive")
    assert archive.status_code == 200, archive.text
    assert archive.json()["run"]["archived"] is True

    default_list = cloudosd_client.get("/api/cloudosd/runs")
    assert default_list.status_code == 200, default_list.text
    assert run["run_id"] not in {item["run_id"] for item in default_list.json()["runs"]}

    archived_list = cloudosd_client.get("/api/cloudosd/runs?include_archived=true")
    assert archived_list.status_code == 200, archived_list.text
    archived_run = next(item for item in archived_list.json()["runs"] if item["run_id"] == run["run_id"])
    assert archived_run["archived"] is True

    detail = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}")
    assert detail.status_code == 200, detail.text
    assert any(event["event_type"] == "test_evidence" for event in detail.json()["events"])

    restore = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/unarchive")
    assert restore.status_code == 200, restore.text
    assert restore.json()["run"]["archived"] is False

    restored_list = cloudosd_client.get("/api/cloudosd/runs")
    assert run["run_id"] in {item["run_id"] for item in restored_list.json()["runs"]}


def test_cloudosd_archive_hides_provision_progress_by_default(
    cloudosd_client,
    pg_conn,
):
    run = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-ARCHIVE-02",
        source_surface="provision",
    )

    assert cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/archive").status_code == 200

    default_progress = cloudosd_client.get("/api/cloudosd/provision/progress")
    assert default_progress.status_code == 200, default_progress.text
    assert run["run_id"] not in {item["run_id"] for item in default_progress.json()["runs"]}

    archived_progress = cloudosd_client.get("/api/cloudosd/provision/progress?include_archived=true")
    assert archived_progress.status_code == 200, archived_progress.text
    assert run["run_id"] in {item["run_id"] for item in archived_progress.json()["runs"]}


def test_cloudosd_bulk_archive_actions_hide_stale_failed_and_completed_old_runs(
    cloudosd_client,
    pg_conn,
):
    from web import cloudosd_pg

    failed = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-Stale-Failed",
        source_surface="provision",
    )
    complete = _create_cloudosd_run(
        cloudosd_client,
        pg_conn,
        vm_name="Gell-Complete-Old",
        source_surface="provision",
    )
    pg_conn.execute(
        """
        UPDATE cloudosd_runs
        SET state = 'failed', updated_at = now() - interval '13 hours'
        WHERE run_id = %s
        """,
        (failed["run_id"],),
    )
    pg_conn.execute(
        """
        UPDATE cloudosd_runs
        SET state = 'complete', updated_at = now() - interval '25 hours'
        WHERE run_id = %s
        """,
        (complete["run_id"],),
    )
    pg_conn.commit()

    stale = cloudosd_client.post("/api/cloudosd/runs/archive-stale-failed")
    assert stale.status_code == 200, stale.text
    assert stale.json()["archived_count"] == 1
    completed = cloudosd_client.post("/api/cloudosd/runs/archive-completed-old")
    assert completed.status_code == 200, completed.text
    assert completed.json()["archived_count"] == 1

    failed_row = cloudosd_pg.get_run(pg_conn, failed["run_id"])
    complete_row = cloudosd_pg.get_run(pg_conn, complete["run_id"])
    assert failed_row["archived"] is True
    assert complete_row["archived"] is True
    assert failed_row["archive_reason"] == "stale failed CloudOSD run"
    assert complete_row["archive_reason"] == "completed CloudOSD run hidden from default history"

    default_progress = cloudosd_client.get("/api/cloudosd/provision/progress")
    assert failed["run_id"] not in {row["run_id"] for row in default_progress.json()["runs"]}
    assert complete["run_id"] not in {row["run_id"] for row in default_progress.json()["runs"]}

    events = {
        event["event_type"]
        for event in (
            cloudosd_pg.list_events(pg_conn, failed["run_id"])
            + cloudosd_pg.list_events(pg_conn, complete["run_id"])
        )
    }
    assert "stale_failed_runs_archived" in events
    assert "completed_old_runs_archived" in events


def test_provision_cloudosd_uppercase_vmid_serial_tokens_allocate_real_vmid(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app, cloudosd_pg, jobs_pg

    artifact = _create_artifact(pg_conn)

    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
            "vault_proxmox_root_password": "fake-root-pw",
        },
    )

    def fake_proxmox_api(path, *args, **kwargs):
        values = {
            "/cluster/nextid": 120,
            "/cluster/resources?type=vm": [{"vmid": 100}, {"vmid": 119}],
            "/cluster/status": [
                {"type": "node", "name": "pve", "ip": "10.0.0.2"},
            ],
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

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "dell-latitude-3550",
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "group_tag": "GellNative",
            "hostname_pattern": "Gell-{VMID}-{SERIAL}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
            "os_version": "Windows 11 25H2",
            "os_edition": "Enterprise",
            "os_activation": "Volume",
            "os_language": "en-us",
            "tpm_enabled": "on",
            "secure_boot": "on",
            "driver_pack_policy": "None",
            "outbound_policy_mode": "blocked",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    jobs = [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ]
    assert len(jobs) == 1
    args = jobs[0]["args"]
    assert args["requested_vmid"] == 120
    assert args["proxmox_node_ssh_host"] == "10.0.0.2"
    assert not args["vm_custom_serial"].startswith(("SVC-", "CZC-", "HP-"))
    assert "-" not in args["vm_custom_serial"]
    assert args["vm_name"].startswith("Gell-120-")
    assert not args["vm_name"].startswith("Gell-120-SVC-")
    assert args["vm_name"].endswith(args["vm_custom_serial"])
    runs = cloudosd_pg.list_runs(pg_conn, limit=10)
    run = next(row for row in runs if row["run_id"] == args["cloudosd_run_id"])
    assert run["requested_vmid"] == 120
    assert run["requested_vm_name"] == args["vm_name"]
    assert run["vm_oem_profile"] == "dell-latitude-3550"


def test_provision_cloudosd_rejects_low_ram_before_enqueue(
    cloudosd_client,
    pg_conn,
):
    from web import jobs_pg

    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "lenovo-t14",
            "count": "1",
            "cores": "4",
            "memory_mb": "4096",
            "disk_size_gb": "80",
            "hostname_pattern": "LOW-RAM-{index}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "at least" in response.json()["detail"]
    assert [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ] == []


def test_provision_cloudosd_rejects_malformed_hostname_placeholder_before_enqueue(
    cloudosd_client,
    pg_conn,
):
    from web import jobs_pg

    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "lenovo-t14",
            "count": "4",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "hostname_pattern": "Gell-{vmid)",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "invalid placeholder" in response.json()["detail"]
    assert [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ] == []


def test_provision_cloudosd_rejects_invalid_generated_pve_name_before_enqueue(
    cloudosd_client,
    pg_conn,
):
    from web import jobs_pg

    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "lenovo-t14",
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "hostname_pattern": "Gell_{index}",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "invalid Proxmox VM name" in response.json()["detail"]
    assert [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ] == []


def test_provision_cloudosd_chassis_requires_root_ssh(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import app as web_app

    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        web_app,
        "_load_proxmox_config",
        lambda: {
            "proxmox_node": "pve",
            "proxmox_snippets_storage": "local",
            "proxmox_host": "10.0.0.1",
        },
    )

    response = cloudosd_client.post(
        "/api/jobs/provision",
        data={
            "boot_mode": "cloudosd",
            "artifact_id": artifact["id"],
            "profile": "lenovo-t14",
            "count": "1",
            "cores": "4",
            "memory_mb": "8192",
            "disk_size_gb": "80",
            "hostname_pattern": "NO-ROOT-{index}",
            "chassis_type_override": "31",
            "node": "pve",
            "iso_storage": "local",
            "storage": "local-lvm",
            "network_bridge": "vmbr0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Proxmox root SSH" in detail
    assert "Proxmox Permission Bootstrap" in detail


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
    _write_fake_msi(msi_path, payload=b"MZ" + (b"f" * 4094))
    try:
        response = cloudosd_client.get("/api/cloudosd/assets/autopilotagent.msi")
        assert response.status_code == 200
        assert response.content == b"MZ" + (b"f" * 4094)
    finally:
        msi_path.unlink(missing_ok=True)


def test_cloudosd_agent_msi_asset_rejects_placeholder_from_app_output(
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
    msi_path.write_text("placeholder", encoding="utf-8")
    try:
        response = cloudosd_client.get("/api/cloudosd/assets/autopilotagent.msi")
        assert response.status_code == 404
        assert "valid AutopilotAgent MSI" in response.json()["detail"]
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


def test_cloudosd_run_accepts_sdn_vnet_network_target(
    cloudosd_client,
    pg_conn,
    monkeypatch,
):
    from web import cloudosd_endpoints, jobs_pg

    artifact = _create_artifact(pg_conn)
    monkeypatch.setattr(
        cloudosd_endpoints,
        "proxmox_options_payload",
        lambda: {
            "schema_version": 1,
            "source": "live",
            "defaults": {
                "node": "pve",
                "iso_storage": "local",
                "disk_storage": "local-lvm",
                "bridge": "vmbr0",
            },
            "catalog": cloudosd_endpoints.catalog_payload(),
            "nodes": ["pve"],
            "storages": {"iso": ["local"], "disk": ["local-lvm"]},
            "bridges": ["vmbr0"],
            "network_targets": [
                {"kind": "bridge", "value": "vmbr0", "label": "vmbr0"},
                {
                    "kind": "sdn_vnet",
                    "value": "lab101",
                    "label": "Lab 101",
                    "zone": "lab-simple",
                },
            ],
            "vms": [],
        },
    )

    run_response = cloudosd_client.post(
        "/api/cloudosd/runs",
        json=_run_payload(
            artifact["id"],
            vm_name="CLOUDOSD-SDN",
            network_bridge="lab101",
        ),
    )

    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    assert run["network_bridge"] == "lab101"

    response = cloudosd_client.post(f"/api/cloudosd/runs/{run['run_id']}/provision")

    assert response.status_code == 202, response.text
    job = jobs_pg.get_job(response.json()["job_id"])
    assert job["args"]["proxmox_bridge"] == "lab101"


def test_cloudosd_wizard_page_lists_artifacts_and_policy(cloudosd_client, pg_conn):
    artifact = _create_artifact(pg_conn)

    response = cloudosd_client.get("/osdcloud", follow_redirects=False)
    builder_response = cloudosd_client.get("/osdcloud/builder", follow_redirects=False)
    artifacts_response = cloudosd_client.get("/osdcloud/artifacts", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/react/cloudosd"
    assert builder_response.status_code == 302
    assert builder_response.headers["location"] == "/react/cloudosd?view=builder"
    assert artifacts_response.status_code == 302
    assert artifacts_response.headers["location"] == "/react/cloudosd?view=artifacts"

    overview = cloudosd_client.get("/api/cloudosd/page").json()
    builder = cloudosd_client.get("/api/cloudosd/page?view=builder").json()
    artifacts = cloudosd_client.get("/api/cloudosd/page?view=artifacts").json()
    assert overview["cloudosd_view"] == "overview"
    assert builder["cloudosd_view"] == "builder"
    assert artifacts["cloudosd_view"] == "artifacts"
    assert any(row["iso_sha256"] == artifact["iso_sha256"] for row in artifacts["artifacts"])
    assert builder["catalog"]["os_versions"]


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

    response = cloudosd_client.get(f"/osdcloud/runs/{run['run_id']}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == f"/react/cloudosd/runs/{run['run_id']}"

    response = cloudosd_client.get(f"/api/cloudosd/runs/{run['run_id']}/page")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["run_id"] == run["run_id"]
    assert body["run"]["requested_vm_name"] == "Cloud OSD Lab 001"
    assert body["run"]["pve_vm_name"] == "Cloud OSD Lab 001"
    assert body["run"]["expected_computer_name"] == "CloudOSDLab001"
    assert body["latest_heartbeat"] is None
