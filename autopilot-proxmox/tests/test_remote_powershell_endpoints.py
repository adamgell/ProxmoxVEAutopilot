from __future__ import annotations

from fastapi.testclient import TestClient


def test_queue_endpoint_facts_creates_typed_work_for_registered_agent(pg_conn):
    from web import agent_telemetry_pg, app as web_app

    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-ring0ivy24-01",
        token="ring0ivy24-test-token",
        vmid=135,
    )

    response = TestClient(web_app.app).post(
        "/api/remote-powershell/v1/agents/agent-ring0ivy24-01/endpoint-facts"
    )

    assert response.status_code == 202
    assert response.json()["kind"] == "remote_powershell"
    assert response.json()["request"] == {"command_id": "endpoint_facts"}
    assert response.json()["agent_id"] == "agent-ring0ivy24-01"


def test_queue_endpoint_facts_rejects_unknown_agent(pg_conn):
    from web import agent_telemetry_pg, app as web_app

    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)

    response = TestClient(web_app.app).post(
        "/api/remote-powershell/v1/agents/missing/endpoint-facts"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "agent is not registered: missing"
