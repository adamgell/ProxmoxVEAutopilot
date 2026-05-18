from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
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
    agent_version: str = "0.2.0-test",
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
            "agent_version": agent_version,
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


def test_buildhost_fleet_bootstrap_auto_approves_expected_identity(
    agent_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import agent_telemetry_pg, agent_v1_endpoints

    state_path = tmp_path / "foundation_state.json"
    state_path.write_text(
        """
        {
          "build_host_agent_auto_approve": true,
          "build_host_vmid": "100",
          "build_host_expected_agent_id": "buildhost-100",
          "build_host_expected_computer_name": "AUTOPILOT-BLD"
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_v1_endpoints, "_SETUP_STATE_PATH", state_path)

    response = _bootstrap_fleet_agent(
        agent_client,
        agent_id="buildhost-100",
        phase="build-host",
        vmid=100,
        computer_name="AUTOPILOT-BLD",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_status"] == "approved"
    assert body["agent_token"]

    device = agent_telemetry_pg.get_device(pg_conn, "buildhost-100")
    assert device["vmid"] == 100
    assert device["computer_name"] == "AUTOPILOT-BLD"
    approval = agent_telemetry_pg.get_bootstrap_approval(pg_conn, body["approval_id"])
    assert approval["status"] in {"approved", "claimed"}
    assert approval["agent_token"]


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


def test_agent_admin_create_and_update_metadata(agent_client, pg_conn):
    from web import agent_telemetry_pg
    from web import app as app_module

    create = agent_client.post(
        "/api/agents",
        data={
            "agent_id": "agent-admin-crud",
            "vmid": "121",
            "computer_name": "GELL-CRUD",
            "serial_number": "GELL-CRUD-SERIAL",
            "agent_version": "0.1.1",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303, create.text

    device = agent_telemetry_pg.get_device(pg_conn, "agent-admin-crud")
    original_token_hash = device["token_hash"]
    assert device["vmid"] == 121
    assert device["computer_name"] == "GELL-CRUD"
    assert device["serial_number"] == "GELL-CRUD-SERIAL"
    assert device["agent_version"] == "0.1.1"

    update = agent_client.post(
        "/api/agents/agent-admin-crud/update",
        data={
            "vmid": "122",
            "computer_name": "GELL-CRUD-UPDATED",
            "serial_number": "GELL-CRUD-SERIAL-2",
            "agent_version": "0.1.2",
        },
        follow_redirects=False,
    )
    assert update.status_code == 303, update.text

    updated = agent_telemetry_pg.get_device(pg_conn, "agent-admin-crud")
    assert updated["token_hash"] == original_token_hash
    assert updated["vmid"] == 122
    assert updated["computer_name"] == "GELL-CRUD-UPDATED"
    assert updated["serial_number"] == "GELL-CRUD-SERIAL-2"
    assert updated["agent_version"] == "0.1.2"

    rows = {row["agent_id"]: row for row in app_module._agent_inventory_rows()}
    assert rows["agent-admin-crud"]["vmid"] == 122
    assert rows["agent-admin-crud"]["computer_name"] == "GELL-CRUD-UPDATED"


def test_agent_admin_hard_delete_removes_local_agent_state(agent_client, pg_conn):
    from web import agent_telemetry_pg

    create = agent_client.post(
        "/api/agents",
        data={
            "agent_id": "agent-delete-crud",
            "vmid": "123",
            "computer_name": "GELL-DELETE",
            "serial_number": "GELL-DELETE",
            "agent_version": "0.1.1",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303, create.text

    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-delete-crud",
        payload={
            "vmid": 123,
            "computer_name": "GELL-DELETE",
            "serial_number": "GELL-DELETE",
            "agent_version": "0.1.1",
        },
    )
    agent_telemetry_pg.record_event(
        pg_conn,
        agent_id="agent-delete-crud",
        payload={
            "severity": "info",
            "event_type": "test",
            "message": "delete cascade",
        },
    )
    agent_telemetry_pg.create_work_item(
        pg_conn,
        agent_id="agent-delete-crud",
        kind="capture_autopilot_hash",
        request={"group_tag": "DeleteTest"},
        vmid=123,
    )
    approval = agent_telemetry_pg.create_bootstrap_approval(
        pg_conn,
        bootstrap_token="delete-bootstrap",
        agent_id="agent-delete-crud",
        computer_name="GELL-DELETE",
    )

    delete = agent_client.post(
        "/api/agents/agent-delete-crud/delete",
        follow_redirects=False,
    )

    assert delete.status_code == 303, delete.text
    assert agent_telemetry_pg.get_device(pg_conn, "agent-delete-crud") is None
    assert agent_telemetry_pg.get_bootstrap_approval(pg_conn, approval["approval_id"]) is None
    assert pg_conn.execute(
        "SELECT count(*) FROM agent_heartbeats WHERE agent_id = %s",
        ("agent-delete-crud",),
    ).fetchone()["count"] == 0
    assert pg_conn.execute(
        "SELECT count(*) FROM agent_events WHERE agent_id = %s",
        ("agent-delete-crud",),
    ).fetchone()["count"] == 0
    assert pg_conn.execute(
        "SELECT count(*) FROM agent_work_items WHERE agent_id = %s",
        ("agent-delete-crud",),
    ).fetchone()["count"] == 0


def test_agent_admin_delete_missing_returns_operator_error(agent_client):
    response = agent_client.post(
        "/api/agents/missing-agent/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/vms?error=")


def test_vms_agent_inventory_renders_hard_delete_crud_controls(agent_client, pg_conn):
    from web import agent_telemetry_pg
    from web import app as app_module

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-ui-crud",
        token="ui-crud-secret",
        vmid=124,
        computer_name="GELL-UI-CRUD",
        serial_number="GELL-UI-CRUD",
        agent_version="0.1.1",
    )
    app_module._VMS_CACHE.update({
        "data": [],
        "devices": ([], ""),
        "hash_serials": set(),
        "fetched_at": app_module.time.monotonic(),
        "refreshing": False,
    })

    response = agent_client.get("/vms")

    assert response.status_code == 200
    assert 'action="/api/agents"' in response.text
    assert 'name="agent_id"' in response.text
    assert 'action="/api/agents/agent-ui-crud/update"' in response.text
    assert 'action="/api/agents/agent-ui-crud/delete"' in response.text
    assert "confirm(" not in response.text.partition('action="/api/agents/agent-ui-crud/delete"')[2].split("</form>", 1)[0]


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


def test_agent_heartbeat_records_claimable_capabilities(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = _bootstrap_fleet_agent(
        agent_client,
        agent_id="agent-capabilities",
        computer_name="GELL-CAPABILITIES",
    )
    approval_id = reg.json()["approval_id"]
    _approve_bootstrap(agent_client, approval_id, "capabilities-secret")
    token = agent_client.get(
        f"/api/agent/v1/bootstrap/claim/{approval_id}",
        headers=_bearer("fleet-bootstrap"),
    ).json()["agent_token"]

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(token),
        json={
            "agent_id": "agent-capabilities",
            "computer_name": "GELL-CAPABILITIES",
            "primary_ipv4": "10.211.55.122",
            "current_phase": "bootstrap",
            "server_url": "http://192.168.2.4:5000",
            "capabilities": [
                "capture_autopilot_hash",
                "configure_build_host_role",
            ],
        },
    )

    assert response.status_code == 200, response.text
    latest = agent_telemetry_pg.latest_for_agent(pg_conn, "agent-capabilities")
    assert latest["raw_json"]["capabilities"] == [
        "capture_autopilot_hash",
        "configure_build_host_role",
    ]
    assert latest["raw_json"]["server_url"] == "http://192.168.2.4:5000"


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


def test_capture_job_refuses_agents_without_work_queue_support(
    agent_client,
):
    from web import jobs_pg

    _approved_agent_with_heartbeat(
        agent_client,
        agent_id="agent-old-osd",
        token="agent-old-osd-secret",
        vmid=120,
        computer_name="Gell-OLDOSD",
        agent_version="0.1.0.0",
    )

    response = agent_client.post(
        "/api/jobs/capture",
        data={"vmid": "120", "vm_name": "Gell-OLDOSD", "group_tag": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/vms?error=")
    assert jobs_pg.list_jobs() == []


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


def test_build_host_workload_queue_uses_first_run_default_artifact_path(
    agent_client,
    pg_conn,
    monkeypatch,
):
    from web import agent_telemetry_pg
    from web import app as web_app

    _approved_agent_with_heartbeat(
        agent_client,
        agent_id="buildhost-100",
        token="buildhost-secret",
        vmid=100,
        computer_name="AUTOPILOT-BLD",
    )
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "controller": {"url": "http://controller:5000"},
        "build_host": {
            "vmid": 100,
            "expected_agent_id": "buildhost-100",
        },
    })

    response = agent_client.post("/api/setup/v1/build-host/workloads", json={})

    assert response.status_code == 202, response.text
    queued_kinds = {item["kind"] for item in response.json()["queued"]}
    assert {
        "install_build_prerequisites",
        "fetch_source_bundle",
        "build_agent_msi",
        "build_winpe",
        "build_cloudosd",
        "publish_artifacts",
    }.issubset(queued_kinds)
    assert "build_osdeploy" not in queued_kinds

    rows = pg_conn.execute(
        "SELECT kind, request_json FROM agent_work_items WHERE agent_id = %s",
        ("buildhost-100",),
    ).fetchall()
    requests = {row["kind"]: row["request_json"] for row in rows}
    assert requests["build_winpe"]["source_bundle_url"] == (
        "http://controller:5000/api/setup/v1/source-bundle.zip"
    )
    assert requests["build_winpe"]["work_root"] == (
        r"C:\BuildRoot\ProxmoxVEAutopilot"
    )
    assert agent_telemetry_pg.get_work_item(pg_conn, response.json()["queued"][0]["id"])


def test_agent_worker_dispatches_osdeploy_build_host_work():
    worker = (
        Path(__file__).resolve().parents[2]
        / "autopilot-agent"
        / "src"
        / "AutopilotAgent"
        / "Worker.cs"
    ).read_text(encoding="utf-8")

    assert "BuildHostWorkService.SupportedKinds.Contains" in worker


def test_agent_osdeploy_build_stages_source_bundle_when_source_tree_missing():
    service = (
        Path(__file__).resolve().parents[2]
        / "autopilot-agent"
        / "src"
        / "AutopilotAgent"
        / "BuildHostWorkService.cs"
    ).read_text(encoding="utf-8")

    assert 'ReadString(work.Request, "source_bundle_url", "")' in service
    assert "await FetchSourceBundleAsync(config, work, cancellationToken);" in service


def test_agent_osdeploy_build_uploads_only_manifest_selected_outputs():
    service = (
        Path(__file__).resolve().parents[2]
        / "autopilot-agent"
        / "src"
        / "AutopilotAgent"
        / "BuildHostWorkService.cs"
    ).read_text(encoding="utf-8")

    winpe_start = service.index("private async Task<Dictionary<string, object?>> BuildWinPeAsync")
    cloudosd_start = service.index("private async Task<Dictionary<string, object?>> BuildCloudOsdAsync")
    osdeploy_start = service.index("private async Task<Dictionary<string, object?>> BuildOsDeployAsync")
    selector_start = service.index("internal static IReadOnlyList<string> SelectOsDeployBuildOutputs")
    assert "SelectOsDeployBuildOutputs(outputRoot)" not in service[winpe_start:cloudosd_start]
    assert "SelectOsDeployBuildOutputs(outputRoot, output.Stdout, buildStartedUtc)" in service[osdeploy_start:selector_start]
    assert 'EnumerateFiles(outputRoot, "osdeploy-server-*.json"' in service
    assert 'AddManifestPath(root, paths, "output_wim")' in service
    assert 'AddManifestPath(root, paths, "output_iso")' in service
    assert 'stdout={Truncate(output.Stdout, 4000)} stderr={Truncate(output.Stderr, 4000)}' in service


def test_agent_build_prerequisites_install_pinned_osdeploy_modules():
    service = (
        Path(__file__).resolve().parents[2]
        / "autopilot-agent"
        / "src"
        / "AutopilotAgent"
        / "BuildHostWorkService.cs"
    ).read_text(encoding="utf-8")

    assert 'ReadString(work.Request, "osdeploy_version", "26.1.30.5")' in service
    assert 'ReadString(work.Request, "osdbuilder_version", "24.10.8.1")' in service
    assert "dotnet --list-sdks" in service
    assert "[Net.SecurityProtocolType]::Tls12" in service
    assert "Install-PackageProvider -Name NuGet -MinimumVersion '2.8.5.201' -ForceBootstrap" in service
    assert "Start-Process -FilePath powershell.exe" not in service
    assert "Install-Module -Name '$($moduleSpec.Name)' -RequiredVersion '$($moduleSpec.RequiredVersion)'" in service
    assert "-Scope AllUsers -Force -AllowClobber -Confirm:`$false" in service


def test_setup_promote_artifacts_registers_agent_built_osdeploy_artifact(
    agent_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app
    from web import osdeploy_pg, setup_artifacts

    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", tmp_path / "setup-state.json")
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "state": {"pve_node": "pve1", "pve_iso_storage": "local"},
    })
    uploads = []

    def fake_proxmox_upload(path, file_path, *, data=None, field_name=None, content_type=None):
        uploads.append({
            "path": path,
            "method": "POST",
            "data": data,
            "filename": Path(file_path).name,
            "field_name": field_name,
            "content_type": content_type,
        })
        return {"data": "local:iso/osdeploy-server-amd64-test.iso"}

    monkeypatch.setattr(web_app, "_proxmox_upload_file", fake_proxmox_upload)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    iso_path = source_dir / "osdeploy-server-amd64-test.iso"
    wim_path = source_dir / "osdeploy-server-amd64-test.wim"
    manifest_path = source_dir / "osdeploy-server-amd64-test.json"
    iso_path.write_bytes(b"iso bytes")
    wim_path.write_bytes(b"wim bytes")
    manifest = {
        "architecture": "amd64",
        "osdeploy_module_version": "26.1.30.5",
        "osdbuilder_module_version": "24.10.8.1",
        "adk_version": "10.1.26100.1",
        "build_sha": "abcdef",
        "source_media": "Windows Server 2025",
        "image_name": "Windows Server 2025 Datacenter",
        "image_index": 1,
        "os_version": "Windows Server 2025",
        "os_edition": "Datacenter",
        "os_language": "en-us",
        "built_by_host": "buildhost-100",
        "iso_sha256": sha256(iso_path.read_bytes()).hexdigest(),
        "wim_sha256": sha256(wim_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8-sig")

    work_item_id = "work-osdeploy-1"
    setup_artifacts.register_artifact(
        kind="manifest",
        source_path=manifest_path,
        producer_agent_id="buildhost-100",
        work_item_id=work_item_id,
    )
    setup_artifacts.register_artifact(
        kind="wim",
        source_path=wim_path,
        producer_agent_id="buildhost-100",
        work_item_id=work_item_id,
    )
    iso = setup_artifacts.register_artifact(
        kind="osdeploy-iso",
        source_path=iso_path,
        producer_agent_id="buildhost-100",
        work_item_id=work_item_id,
    )

    response = agent_client.post(
        "/api/setup/v1/artifacts/promote",
        json={"artifact_ids": [iso["artifact_id"]]},
    )

    assert response.status_code == 200, response.text
    assert uploads == [{
        "path": "/nodes/pve1/storage/local/upload",
        "method": "POST",
        "data": {"content": "iso"},
        "filename": "osdeploy-server-amd64-test.iso",
        "field_name": "filename",
        "content_type": "application/octet-stream",
    }]
    assert response.json()["promoted"][0]["proxmox_volid"] == (
        "local:iso/osdeploy-server-amd64-test.iso"
    )
    rows = osdeploy_pg.list_artifacts(pg_conn, architecture="amd64")
    assert len(rows) == 1
    artifact = rows[0]
    assert artifact["proxmox_volid"] == "local:iso/osdeploy-server-amd64-test.iso"
    assert artifact["build_job_id"] == work_item_id
    assert artifact["built_by_host"] == "buildhost-100"
    assert artifact["iso_sha256"] == manifest["iso_sha256"]
    assert artifact["wim_sha256"] == manifest["wim_sha256"]
    assert artifact["iso_path"].endswith("/osdeploy-iso/osdeploy-server-amd64-test.iso")
    assert artifact["wim_path"].endswith("/wim/osdeploy-server-amd64-test.wim")
    assert artifact["manifest_path"].endswith("/manifest/osdeploy-server-amd64-test.json")


def test_setup_promote_artifacts_can_mark_pve_pulled_osdeploy_artifact(
    agent_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app
    from web import osdeploy_pg, setup_artifacts

    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", tmp_path / "setup-state.json")
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "state": {"pve_node": "pve1", "pve_iso_storage": "local"},
    })
    monkeypatch.setattr(
        web_app,
        "_proxmox_upload_file",
        lambda *args, **kwargs: pytest.fail("PVE-pulled artifact should not upload through API"),
    )

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    iso_path = source_dir / "osdeploy-server-amd64-pulled.iso"
    wim_path = source_dir / "osdeploy-server-amd64-pulled.wim"
    manifest_path = source_dir / "osdeploy-server-amd64-pulled.json"
    iso_path.write_bytes(b"iso bytes")
    wim_path.write_bytes(b"wim bytes")
    manifest = {
        "architecture": "amd64",
        "osdeploy_module_version": "26.1.30.5",
        "osdbuilder_module_version": "24.10.8.1",
        "adk_version": "10.1.26100.1",
        "build_sha": "abcdef",
        "source_media": "Windows Server 2025",
        "image_name": "Windows Server 2025 Datacenter",
        "image_index": 1,
        "os_version": "Windows Server 2025",
        "os_edition": "Datacenter",
        "os_language": "en-us",
        "built_by_host": "buildhost-100",
        "iso_sha256": sha256(iso_path.read_bytes()).hexdigest(),
        "wim_sha256": sha256(wim_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8-sig")

    work_item_id = "work-osdeploy-pulled"
    setup_artifacts.register_artifact(
        kind="manifest",
        source_path=manifest_path,
        producer_agent_id="buildhost-100",
        work_item_id=work_item_id,
    )
    setup_artifacts.register_artifact(
        kind="wim",
        source_path=wim_path,
        producer_agent_id="buildhost-100",
        work_item_id=work_item_id,
    )
    iso = setup_artifacts.register_artifact(
        kind="osdeploy-iso",
        source_path=iso_path,
        producer_agent_id="buildhost-100",
        work_item_id=work_item_id,
    )

    response = agent_client.post(
        "/api/setup/v1/artifacts/promote",
        json={
            "artifact_ids": [iso["artifact_id"]],
            "storage": "local",
            "already_copied": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["promoted"][0]["proxmox_volid"] == (
        "local:iso/osdeploy-server-amd64-pulled.iso"
    )
    rows = osdeploy_pg.list_artifacts(pg_conn, architecture="amd64")
    assert len(rows) == 1
    assert rows[0]["proxmox_volid"] == "local:iso/osdeploy-server-amd64-pulled.iso"


def test_setup_promote_artifacts_defers_large_api_upload(
    agent_client,
    pg_conn,
    tmp_path,
    monkeypatch,
):
    from web import app as web_app
    from web import osdeploy_pg, setup_artifacts

    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(setup_artifacts, "REGISTRY_PATH", artifact_root / "artifact_registry.json")
    monkeypatch.setattr(web_app, "SETUP_STATE_PATH", tmp_path / "setup-state.json")
    monkeypatch.setattr(web_app, "_setup_readiness", lambda: {
        "state": {"pve_node": "pve1", "pve_iso_storage": "local"},
    })
    monkeypatch.setattr(web_app, "_setup_promote_api_upload_max_bytes", lambda: 4)
    monkeypatch.setattr(
        web_app,
        "_proxmox_upload_file",
        lambda *args, **kwargs: pytest.fail("large setup artifact should be pulled by PVE"),
    )

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    iso_path = source_dir / "osdeploy-server-amd64-large.iso"
    iso_path.write_bytes(b"large iso bytes")
    iso = setup_artifacts.register_artifact(
        kind="osdeploy-iso",
        source_path=iso_path,
        producer_agent_id="buildhost-100",
        work_item_id="work-large",
    )

    response = agent_client.post(
        "/api/setup/v1/artifacts/promote",
        json={"artifact_ids": [iso["artifact_id"]]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["promoted"] == []
    assert body["deferred"] == [{
        "artifact_id": iso["artifact_id"],
        "kind": "osdeploy-iso",
        "filename": "osdeploy-server-amd64-large.iso",
        "size_bytes": len(b"large iso bytes"),
        "reason": "pve_pull_required_for_large_artifact",
        "max_api_upload_bytes": 4,
    }]
    rows = osdeploy_pg.list_artifacts(pg_conn, architecture="amd64")
    assert rows == []


def test_setup_promote_artifacts_requires_selection_for_already_copied(agent_client):
    response = agent_client.post(
        "/api/setup/v1/artifacts/promote",
        json={"already_copied": True},
    )

    assert response.status_code == 400
    assert "artifact_ids is required" in response.text


def test_proxmox_upload_body_streams_file_without_buffering(tmp_path):
    from web import app as web_app

    payload = b"a" * 1024 * 1024
    iso_path = tmp_path / "large.iso"
    iso_path.write_bytes(payload)

    body = web_app._StreamingMultipartBody(
        fields={"content": "iso"},
        file_field="filename",
        file_path=iso_path,
        content_type="application/octet-stream",
    )
    try:
        assert len(body) > len(payload)
        assert "multipart/form-data; boundary=" in body.content_type
        first = body.read(128)
        second = body.read(128)
        assert len(first) == 128
        assert len(second) == 128
        assert b'name="content"' in first
        implicit = body.read()
        assert 0 < len(implicit) <= body._DEFAULT_READ_SIZE
        remaining = first + second + implicit
        while True:
            chunk = body.read()
            if not chunk:
                break
            assert len(chunk) <= body._DEFAULT_READ_SIZE
            remaining += chunk
        assert payload in remaining
        assert remaining.endswith(f"\r\n--{body.boundary}--\r\n".encode("utf-8"))
    finally:
        body.close()


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
