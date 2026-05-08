from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required for PostgreSQL-backed agent endpoint tests",
)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


@pytest.fixture
def agent_client(pg_conn, monkeypatch):
    from web import agent_telemetry_pg, ts_engine_pg

    monkeypatch.setenv("AUTOPILOT_AGENT_BOOTSTRAP_TOKEN", "fleet-bootstrap")
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


def test_agent_bootstrap_accepts_configured_fleet_token(agent_client, pg_conn):
    from web import agent_telemetry_pg

    response = agent_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer("fleet-bootstrap"),
        json={
            "agent_id": "agent-ninja",
            "vmid": 107,
            "computer_name": "GELL-NINJA107",
        },
    )

    assert response.status_code == 200, response.text
    device = agent_telemetry_pg.get_device(pg_conn, "agent-ninja")
    assert device["created_from_run_id"] is None
    assert device["vmid"] == 107


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


def test_agent_token_revoke_blocks_heartbeat(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = agent_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer("fleet-bootstrap"),
        json={"agent_id": "agent-revoke", "vmid": 108},
    )
    token = reg.json()["agent_token"]
    agent_telemetry_pg.revoke_agent(pg_conn, "agent-revoke")

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers=_bearer(token),
        json={"agent_id": "agent-revoke", "vmid": 108},
    )

    assert response.status_code == 401


def test_agent_events_are_recorded(agent_client, pg_conn):
    from web import agent_telemetry_pg

    reg = agent_client.post(
        "/api/agent/v1/bootstrap",
        headers=_bearer("fleet-bootstrap"),
        json={"agent_id": "agent-events", "vmid": 109},
    )

    response = agent_client.post(
        "/api/agent/v1/events",
        headers=_bearer(reg.json()["agent_token"]),
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
