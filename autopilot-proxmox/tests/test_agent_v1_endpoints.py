from __future__ import annotations

from hashlib import sha256
import shutil

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed agent endpoint tests",
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _bootstrap_fleet_agent(client: TestClient, agent_id: str = "agent-ninja", **payload):
    body = {"agent_id": agent_id, "computer_name": "GELL-NINJA107"}
    body.update(payload)
    return client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer("fleet-bootstrap"),
        json=body,
    )


def _approve_bootstrap(client: TestClient, approval_id: str, agent_token: str | None = None):
    body = {}
    if agent_token:
        body["agent_token"] = agent_token
    return client.post(f"/api/agent-approvals/{approval_id}/approve", json=body)


def _create_run(conn) -> str:
    from web import ts_engine_pg

    sequence_id = ts_engine_pg.create_sequence(conn, name="Agent v1 Demo")
    ts_engine_pg.add_step(
        conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Install QGA",
        kind="install_qga",
        phase="full_os",
        position=0,
    )
    version_id = ts_engine_pg.compile_sequence(conn, sequence_id)
    return ts_engine_pg.create_run_from_version(
        conn,
        sequence_version_id=version_id,
        deployment_target={
            "vmid": 119,
            "vm_uuid": "vm-119",
            "computer_name": "GELL-AGENT119",
            "serial_number": "GELL-AGENT119",
        },
    )


def _approved_agent_with_heartbeat(
    client: TestClient,
    *,
    agent_id: str,
    token: str,
    vmid: int,
    computer_name: str,
) -> str:
    reg = _bootstrap_fleet_agent(
        client,
        agent_id=agent_id,
        vmid=vmid,
        computer_name=computer_name,
    )
    assert reg.status_code == 200, reg.text
    approval_id = reg.json()["approval_id"]
    approved = _approve_bootstrap(client, approval_id, token)
    assert approved.status_code == 200, approved.text
    claimed = client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    )
    assert claimed.status_code == 200, claimed.text
    agent_token = claimed.json()["agent_token"]
    heartbeat = client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(agent_token),
        json={
            "agent_id": agent_id,
            "vmid": vmid,
            "computer_name": computer_name,
            "serial_number": computer_name,
            "primary_ipv4": f"10.211.55.{vmid}",
            "agent_version": "0.2.0-test",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return agent_token


@pytest.fixture
def agent_client(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, ts_engine_pg

    monkeypatch.delenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv(
        "AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256",
        sha256("fleet-bootstrap".encode("utf-8")).hexdigest(),
    )
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)

    from web import app as web_app

    return TestClient(web_app.app)


def test_agent_bootstrap_with_osd_run_token_stores_only_token_hash(
    agent_client,
    pg_conn,
):
    from web import agent_telemetry_pg, winpe_token

    run_id = _create_run(pg_conn)
    run_token = winpe_token.sign(run_id=run_id, ttl_seconds=300)

    response = agent_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer(run_token),
        json={
            "agent_id": "agent-119",
            "run_id": run_id,
            "phase": "full_os",
            "vmid": 119,
            "vm_uuid": "vm-119",
            "computer_name": "GELL-AGENT119",
            "serial_number": "GELL-AGENT119",
            "agent_version": "0.1.0",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agent_id"] == "agent-119"
    assert body["agent_token"]
    assert body["heartbeat_interval_seconds"] > 0

    device = agent_telemetry_pg.get_device(pg_conn, "agent-119")
    assert device["token_hash"]
    assert body["agent_token"] not in device["token_hash"]
    assert device["created_from_run_id"] == run_id
    assert device["vmid"] == 119


def test_agent_bootstrap_with_fleet_token_creates_pending_approval(agent_client, pg_conn):
    from web import agent_telemetry_pg

    response = _bootstrap_fleet_agent(agent_client, vmid=107)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_status"] == "pending"
    assert body["approval_id"]
    assert body["poll_url"] == f"/api/agent/v1/bootstrap/claim/{body['approval_id']}"
    assert "agent_token" not in body

    assert agent_telemetry_pg.get_device(pg_conn, "agent-ninja") is None
    approval = agent_telemetry_pg.get_bootstrap_approval(pg_conn, body["approval_id"])
    assert approval["agent_id"] == "agent-ninja"
    assert approval["status"] == "pending"
    assert approval["vmid"] == 107


def test_agent_bootstrap_rejects_wrong_fleet_token(agent_client):
    response = agent_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer("wrong-fleet-bootstrap"),
        json={
            "agent_id": "agent-denied",
            "vmid": 107,
            "computer_name": "GELL-DENIED107",
        },
    )

    assert response.status_code == 401


def test_agent_bootstrap_accepts_hash_as_bootstrap_proof(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, ts_engine_pg

    hash_proof = "be61f75013b30a88d5d6e35bf35d15c9153a38b6bd80e22352de8f7c13b958fd"
    monkeypatch.delenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256", hash_proof)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)

    from web import app as web_app

    client = TestClient(web_app.app)
    response = client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer(hash_proof),
        json={
            "agent_id": "agent-hash-proof",
            "computer_name": "GELL-HASH-PROOF",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_status"] == "pending"
    assert "agent_token" not in body


def test_agent_bootstrap_ignores_legacy_raw_fleet_token(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, ts_engine_pg

    monkeypatch.delenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN_SHA256", raising=False)
    monkeypatch.setenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN", "fleet-bootstrap")
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)

    from web import app as web_app

    client = TestClient(web_app.app)
    response = client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer("fleet-bootstrap"),
        json={
            "agent_id": "agent-legacy-token-denied",
            "computer_name": "GELL-LEGACY",
        },
    )

    assert response.status_code == 401


def test_agent_bootstrap_polling_requires_original_temporary_token(agent_client):
    reg = _bootstrap_fleet_agent(agent_client, agent_id="agent-poll-auth")
    approval_id = reg.json()["approval_id"]

    response = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("wrong-fleet-bootstrap"),
    )

    assert response.status_code == 401


def test_agent_approval_releases_known_agent_secret(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = _bootstrap_fleet_agent(agent_client, agent_id="agent-approved", vmid=108)
    approval_id = reg.json()["approval_id"]
    waiting = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    )
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["approval_status"] == "pending"
    assert "agent_token" not in waiting.json()

    known_secret = "known-agent-secret-for-test"
    approved = _approve_bootstrap(agent_client, approval_id, known_secret)
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval_status"] == "approved"

    claimed = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["approval_status"] == "approved"
    assert body["agent_token"] == known_secret
    assert body["heartbeat_interval_seconds"] > 0

    device = agent_telemetry_pg.get_device(pg_conn, "agent-approved")
    assert device["vmid"] == 108
    assert known_secret not in device["token_hash"]


def test_agent_rebootstrap_after_approval_returns_known_agent_secret(
    agent_client,
    pg_conn,
):
    from web import agent_telemetry_pg

    reg = _bootstrap_fleet_agent(agent_client, agent_id="agent-approved-rerun", vmid=109)
    approval_id = reg.json()["approval_id"]
    known_secret = "known-agent-secret-after-rerun"
    approved = _approve_bootstrap(agent_client, approval_id, known_secret)
    assert approved.status_code == 200, approved.text

    rerun = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-approved-rerun",
        vmid=109,
        computer_name="GELL-RERUN109",
    )

    assert rerun.status_code == 200, rerun.text
    body = rerun.json()
    assert body["approval_id"] == approval_id
    assert body["approval_status"] == "approved"
    assert body["agent_token"] == known_secret
    assert body["heartbeat_interval_seconds"] > 0
    approval = agent_telemetry_pg.get_bootstrap_approval(pg_conn, approval_id)
    assert approval["claimed_at"]


def test_agent_heartbeat_after_approval_marks_agent_active(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-active-after-approval",
        vmid=116,
    )
    approval_id = reg.json()["approval_id"]
    _approve_bootstrap(agent_client, approval_id, "active-after-approval-secret")
    token = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    ).json()["agent_token"]

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(token),
        json={
            "agent_id": "agent-active-after-approval",
            "vmid": 116,
            "computer_name": "GELL-EC41E7EB",
            "primary_ipv4": "10.211.55.116",
            "qga_state": "Running",
            "current_phase": "ninja",
        },
    )

    assert response.status_code == 200, response.text
    latest = agent_telemetry_pg.latest_agents(pg_conn)
    active = next(
        row for row in latest if row["agent_id"] == "agent-active-after-approval"
    )
    assert active["primary_ipv4"] == "10.211.55.116"
    assert active["qga_state"] == "Running"
    approval = agent_telemetry_pg.get_bootstrap_approval(pg_conn, approval_id)
    assert approval["status"] == "claimed"
    assert approval["claimed_at"]


def test_vms_agent_inventory_shows_pending_approved_and_active_states(agent_client):
    from web import app as app_module

    pending = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-ui-pending",
        computer_name="GELL-PENDING",
    ).json()

    approved = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-ui-approved",
        computer_name="GELL-APPROVED",
    ).json()
    approve_response = _approve_bootstrap(
        agent_client,
        approved["approval_id"],
        "ui-approved-secret",
    )
    assert approve_response.status_code == 200, approve_response.text

    active = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-ui-active",
        computer_name="GELL-ACTIVE",
        vmid=117,
    ).json()
    _approve_bootstrap(agent_client, active["approval_id"], "ui-active-secret")
    active_token = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{active['approval_id']}",
        headers=_bearer("fleet-bootstrap"),
    ).json()["agent_token"]
    heartbeat = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(active_token),
        json={
            "agent_id": "agent-ui-active",
            "vmid": 117,
            "computer_name": "GELL-ACTIVE",
            "primary_ipv4": "10.211.55.117",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text

    rows = {row["agent_id"]: row for row in app_module._agent_inventory_rows()}

    assert rows["agent-ui-pending"]["approval_status"] == "pending"
    assert rows["agent-ui-pending"]["approval_id"] == pending["approval_id"]
    assert rows["agent-ui-approved"]["approval_status"] == "approved"
    assert rows["agent-ui-approved"]["approval_id"] == approved["approval_id"]
    assert rows["agent-ui-active"]["approval_status"] == "active"
    assert rows["agent-ui-active"]["last_heartbeat_at"]


def test_agent_heartbeat_updates_latest_telemetry(agent_client, pg_conn):
    from web import agent_telemetry_pg, winpe_token

    run_id = _create_run(pg_conn)
    run_token = winpe_token.sign(run_id=run_id, ttl_seconds=300)
    reg = agent_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer(run_token),
        json={"agent_id": "agent-119", "run_id": run_id, "vmid": 119},
    )
    token = reg.json()["agent_token"]

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(token),
        json={
            "agent_id": "agent-119",
            "vmid": 119,
            "vm_uuid": "vm-119",
            "computer_name": "GELL-AGENT119",
            "primary_ipv4": "10.211.55.119",
            "ip_addresses": ["10.211.55.119", "fe80::1"],
            "os_name": "Microsoft Windows 11 Enterprise",
            "os_version": "10.0.26100",
            "os_build": "26100",
            "qga_service_name": "QEMU-GA",
            "qga_state": "Running",
            "entra_joined": True,
            "domain_joined": False,
            "current_run_id": run_id,
            "current_phase": "full_os",
        },
    )

    assert response.status_code == 200, response.text
    latest = agent_telemetry_pg.latest_by_vmid(pg_conn)
    assert latest[119]["primary_ipv4"] == "10.211.55.119"
    assert latest[119]["qga_state"] == "Running"
    assert latest[119]["entra_joined"] is True

    config = agent_client.get("/api/agent/v1/config", headers=_bearer(token))
    assert config.status_code == 200, config.text
    assert config.json()["last_heartbeat_at"]
    assert config.json()["last_primary_ipv4"] == "10.211.55.119"


def test_agent_heartbeat_allows_empty_current_run_id(agent_client, pg_conn):
    reg = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-empty-run",
        computer_name="ADAMGELL9324",
    )
    approval_id = reg.json()["approval_id"]
    _approve_bootstrap(agent_client, approval_id, "empty-run-secret")
    token = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    ).json()["agent_token"]

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(token),
        json={
            "agent_id": "agent-empty-run",
            "computer_name": "ADAMGELL9324",
            "primary_ipv4": "10.211.55.6",
            "current_run_id": "",
            "current_phase": "dev-machine-e2e",
        },
    )

    assert response.status_code == 200, response.text


def test_capture_job_enqueues_agent_work_item_instead_of_qga_playbook(
    agent_client,
    pg_conn,
):
    from web import agent_telemetry_pg, jobs_pg

    agent_token = _approved_agent_with_heartbeat(
        agent_client,
        agent_id="agent-gell-osd2",
        token="agent-gell-osd2-secret",
        vmid=118,
        computer_name="Gell-OSD2",
    )

    response = agent_client.post(
        "/api/jobs/capture",
        data={"vmid": "118", "vm_name": "Gell-OSD2", "group_tag": "CloudOSD"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = jobs_pg.get_job(job_id)
    assert job["job_type"] == "hash_capture"
    joined_cmd = " ".join(job["cmd"])
    assert "wait_agent_work_item.py" in joined_cmd
    assert "retry_inject_hash.yml" not in joined_cmd
    assert job["args"]["agent_id"] == "agent-gell-osd2"

    work = agent_telemetry_pg.get_work_item(pg_conn, job["args"]["work_item_id"])
    assert work["status"] == "pending"
    assert work["kind"] == "capture_autopilot_hash"
    assert work["request_json"]["group_tag"] == "CloudOSD"

    next_response = agent_client.post(
        "/api/agent/v1/work/next",
        headers=_bearer(agent_token),
        json={
            "agent_id": "agent-gell-osd2",
            "supported_kinds": ["capture_autopilot_hash"],
        },
    )
    assert next_response.status_code == 200, next_response.text
    item = next_response.json()["work_item"]
    assert item["id"] == work["id"]
    assert item["kind"] == "capture_autopilot_hash"
    assert item["request"]["vmid"] == 118


def test_agent_hash_persists_csv_and_completes_capture_work_item(
    agent_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app

    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path)
    agent_token = _approved_agent_with_heartbeat(
        agent_client,
        agent_id="agent-gell-osd3",
        token="agent-gell-osd3-secret",
        vmid=119,
        computer_name="Gell-OSD3",
    )
    response = agent_client.post(
        "/api/jobs/capture",
        data={"vmid": "119", "vm_name": "Gell-OSD3", "group_tag": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    next_response = agent_client.post(
        "/api/agent/v1/work/next",
        headers=_bearer(agent_token),
        json={
            "agent_id": "agent-gell-osd3",
            "supported_kinds": ["capture_autopilot_hash"],
        },
    )
    assert next_response.status_code == 200, next_response.text
    work_id = next_response.json()["work_item"]["id"]

    hash_response = agent_client.post(
        "/api/agent/v1/hash",
        headers=_bearer(agent_token),
        json={
            "work_item_id": work_id,
            "serial_number": "Gell-OSD3",
            "product_id": "",
            "hardware_hash": "hardware-hash-for-gell-osd3",
        },
    )

    assert hash_response.status_code == 200, hash_response.text
    body = hash_response.json()
    assert body["ok"] is True
    assert body["filename"].endswith("-vm119-Gell-OSD3-agent-v1_hwid.csv")

    files = list(tmp_path.glob("*_hwid.csv"))
    assert len(files) == 1
    csv_text = files[0].read_text(encoding="utf-8")
    assert "Device Serial Number,Windows Product ID,Hardware Hash" in csv_text
    assert "Gell-OSD3,,hardware-hash-for-gell-osd3" in csv_text

    work = agent_telemetry_pg.get_work_item(pg_conn, work_id)
    assert work["status"] == "complete"
    assert work["result_json"]["filename"] == body["filename"]
    assert work["result_json"]["source"] == "agent-v1"


def test_agent_token_revoke_blocks_heartbeat(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = _bootstrap_fleet_agent(agent_client, agent_id="agent-revoke", vmid=108)
    approval_id = reg.json()["approval_id"]
    _approve_bootstrap(agent_client, approval_id, "revoke-secret")
    token = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    ).json()["agent_token"]
    agent_telemetry_pg.revoke_agent(pg_conn, "agent-revoke")

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(token),
        json={"agent_id": "agent-revoke", "vmid": 108},
    )

    assert response.status_code == 401


def test_agent_events_are_recorded(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = _bootstrap_fleet_agent(agent_client, agent_id="agent-events", vmid=109)
    approval_id = reg.json()["approval_id"]
    _approve_bootstrap(agent_client, approval_id, "events-secret")
    token = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    ).json()["agent_token"]

    response = agent_client.post(
        "/api/agent/v1/events",
        headers=_bearer(token),
        json={
            "agent_id": "agent-events",
            "severity": "warning",
            "event_type": "qga_policy_reset",
            "message": "Removed guest-network-get-interfaces block.",
            "data": {"service": "QEMU-GA"},
        },
    )

    assert response.status_code == 200, response.text
    events = agent_telemetry_pg.list_events(pg_conn, "agent-events")
    assert events[0]["event_type"] == "qga_policy_reset"
    assert events[0]["data_json"]["service"] == "QEMU-GA"


def test_agent_api_auth_exemption_is_limited_to_agent_prefix():
    from web import auth

    assert auth.is_exempt_path("/api/agent/v1/bootstrap")
    assert auth.is_exempt_path("/api/agent/v1/heartbeat")
    assert auth.is_exempt_path("/api/agent/v1/events")
    assert auth.is_exempt_path("/api/agent/v1/config")
    assert not auth.is_exempt_path("/api/agent-approvals/abc/approve")


def test_vms_snapshot_prefers_agent_ip_and_guest_state(monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(
        app_module.device_history_db,
        "latest_per_vmid",
        lambda: [
            {
                "vmid": 108,
                "pve": {
                    "vmid": 108,
                    "name": "Gell-60F03E42",
                    "status": "running",
                },
                "probe": {
                    "serial": "Gell-60F03E42",
                    "dsreg_status": {},
                },
            }
        ],
    )
    monkeypatch.setattr(
        app_module.agent_telemetry_pg,
        "latest_by_vmid",
        lambda: {
            108: {
                "computer_name": "GELL-60F03E42",
                "primary_ipv4": "10.211.55.108",
                "os_version": "10.0.26100",
                "qga_state": "Running",
                "received_at": "2026-05-08T12:00:00+00:00",
            }
        },
    )

    rows = app_module._vms_from_monitor_snapshot()

    assert rows[0]["ip_address"] == "10.211.55.108"
    assert rows[0]["hostname"] == "GELL-60F03E42"
    assert rows[0]["os_version"] == "10.0.26100"
    assert rows[0]["agent_qga_state"] == "Running"
