from __future__ import annotations

import shutil

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


@pytest.fixture
def cloudosd_client(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, cloudosd_pg, sequences_pg, ts_engine_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
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


def _create_cloudosd_run(cloudosd_client, pg_conn, **overrides):
    artifact = _create_artifact(pg_conn)
    payload = _run_payload(artifact["id"], **overrides)
    response = cloudosd_client.post("/api/cloudosd/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


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


def _create_domain_join_sequence(pg_conn, cipher, *, unsupported_step: bool = False):
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
            "params": {"credential_id": cred_id, "ou_path": ""},
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
    assert "not CloudOSD-compatible" in response.json()["detail"]
    assert "run_script" in response.json()["detail"]
    assert [
        job for job in jobs_pg.list_jobs(limit=20)
        if job["job_type"] == "provision_cloudosd"
    ] == []


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


def test_cloudosd_provision_job_is_normalized_into_playbook_milestones(
    cloudosd_client,
    pg_conn,
):
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

    task_engine = cloudosd_client.get("/task-engine")
    assert task_engine.status_code == 200
    assert "6/7 done" in task_engine.text
    assert "full_os_waiting_v2" in task_engine.text


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
    assert 'data-cloudosd-field="domain_join_target"' in response.text
    assert 'data-cloudosd-field="domain_join_verification"' in response.text
    assert "Domain join" in response.text
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
    assert caps["provision_cloudosd"] == 4
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

    response = cloudosd_client.get("/provision")

    assert response.status_code == 200, response.text
    body = response.text
    assert body.index('name="boot_mode"') < body.index('name="sequence_id"')
    assert '<option value="cloudosd">CloudOSD' in body
    assert 'data-boot-section="cloudosd"' in body
    assert "CloudOSD blank uses a plain generated serial" in body
    assert f'value="{artifact["id"]}"' in body
    for required in (
        'name="artifact_id"',
        'name="count"',
        'name="cores"',
        'name="memory_mb"',
        'name="disk_size_gb"',
        'name="group_tag"',
        'name="profile"',
        'name="chassis_type_override"',
        'name="node"',
        'name="iso_storage"',
        'name="storage"',
        'name="network_bridge"',
        'name="os_version"',
        'name="os_edition"',
        'name="os_activation"',
        'name="os_language"',
    ):
        assert required in body


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
    assert response.headers["location"].startswith("/cloudosd")
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
    rows = api.json()["runs"]
    row = next(item for item in rows if item["run_id"] == run["run_id"])
    assert row["vm_name"] == "Gell-251-OSD1"
    assert row["milestones"]["vm_created"]["state"] == "done"
    assert row["milestones"]["pe_registered"]["state"] == "done"
    assert row["milestones"]["osdcloud_done"]["state"] == "done"
    assert row["milestones"]["agent_heartbeat"]["state"] == "done"
    assert row["milestones"]["v2_steps_done"]["state"] == "done"
    assert row["milestones"]["intune_state"]["state"] == "done"
    assert row["intune_evidence"]["upload"]["autopilot_device_id"] == "ap-251"
    assert row["intune_evidence"]["assignment"]["status"] == "assignedInSync"
    assert row["intune_evidence"]["enrollment"]["status"] == "enrolled"

    page = cloudosd_client.get("/provision")
    assert page.status_code == 200, page.text
    body = page.text
    assert "CloudOSD Batch Progress" in body
    assert "VM created" in body
    assert "PE registered" in body
    assert "OSDCloud done" in body
    assert "Agent heartbeat" in body
    assert "v2 steps done" in body
    assert "Intune state" in body
    assert "Gell-251-OSD1" in body
    assert "/api/cloudosd/provision/progress" in body


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
