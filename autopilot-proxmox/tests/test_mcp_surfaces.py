import importlib

from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_MCP_TOKEN", "test-mcp-token")
    from web.mcp import server

    importlib.reload(server)
    return TestClient(server.app)


def _client_with_pg(monkeypatch, pg_conn, pg_dsn):
    monkeypatch.setenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", pg_dsn)
    from web.mcp import mcp_pg

    mcp_pg.reset_for_tests(pg_conn)
    return _client(monkeypatch)


def _rpc(client, method, params=None):
    return client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer test-mcp-token",
            "Origin": "http://localhost",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def test_mcp_lists_new_operator_surfaces(monkeypatch):
    client = _client(monkeypatch)

    response = _rpc(client, "tools/list")

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "setup.get_readiness" in names
    assert "setup.queue_build_host_workloads" in names
    assert "osdeploy.get_catalog" in names
    assert "osdeploy.build_artifact" in names
    assert "ubuntu_osd.compile_steps" in names
    assert "autopilot_docs.search" in names
    assert "pve_autopilot.get_cockpit_summary" in names
    assert "pve_autopilot.list_approvals" in names
    assert "pve_autopilot.get_approval" in names
    assert "pve_autopilot.approve_action" in names
    assert "pve_autopilot.reject_action" in names
    assert "cloudosd.get_catalog" in names
    assert "autopilot_agent.run_diagnostic" in names


def test_mcp_rejects_missing_bearer_token(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/mcp",
        headers={"Origin": "http://localhost"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 401


def test_ubuntu_osd_compile_steps_tool_call(monkeypatch):
    client = _client(monkeypatch)

    response = _rpc(
        client,
        "tools/call",
        {
            "name": "ubuntu_osd.compile_steps",
            "arguments": {"plan_steps": [{"step_type": "run_shell", "params": {"script": "true"}}]},
        },
    )

    assert response.status_code == 200
    payload = response.json()["result"]["structuredContent"]
    assert payload["schema_version"] == 1
    assert "steps" in payload


def test_approval_gated_tools_persist_and_do_not_execute_directly(monkeypatch, pg_conn, pg_dsn):
    client = _client_with_pg(monkeypatch, pg_conn, pg_dsn)

    create_response = _rpc(
        client,
        "tools/call",
        {
            "name": "pve_autopilot.delete_vm",
            "arguments": {"vmid": 123, "password": "do-not-store"},
        },
    )

    assert create_response.status_code == 200
    payload = create_response.json()["result"]["structuredContent"]
    assert payload["approval_required"] is True
    assert payload["approval_id"]
    assert payload["proposed_arguments"]["password"] == "[redacted]"

    list_response = _rpc(
        client,
        "tools/call",
        {"name": "pve_autopilot.list_approvals", "arguments": {"status": "pending"}},
    )
    approvals = list_response.json()["result"]["structuredContent"]["approvals"]
    assert [approval["approval_id"] for approval in approvals] == [payload["approval_id"]]
    assert approvals[0]["tool_name"] == "pve_autopilot.delete_vm"
    assert approvals[0]["arguments"]["password"] == "[redacted]"

    approve_response = _rpc(
        client,
        "tools/call",
        {"name": "pve_autopilot.approve_action", "arguments": {"approval_id": payload["approval_id"]}},
    )
    approval = approve_response.json()["result"]["structuredContent"]["approval"]
    assert approval["status"] == "failed"
    assert approval["result"]["executed"] is False
    assert approval["result"]["reason"] == "executor_not_registered"

    audit_count = pg_conn.execute(
        "SELECT count(*) AS count FROM mcp_call_audit WHERE tool_name = %s",
        ("pve_autopilot.delete_vm",),
    ).fetchone()["count"]
    assert audit_count == 1


def test_approval_reject_persists(monkeypatch, pg_conn, pg_dsn):
    client = _client_with_pg(monkeypatch, pg_conn, pg_dsn)

    create_response = _rpc(
        client,
        "tools/call",
        {"name": "pve_autopilot.write_settings", "arguments": {"setting": "x"}},
    )
    approval_id = create_response.json()["result"]["structuredContent"]["approval_id"]

    reject_response = _rpc(
        client,
        "tools/call",
        {
            "name": "pve_autopilot.reject_action",
            "arguments": {"approval_id": approval_id, "reason": "test rejection"},
        },
    )

    approval = reject_response.json()["result"]["structuredContent"]["approval"]
    assert approval["status"] == "rejected"
    assert approval["result"]["reason"] == "test rejection"
