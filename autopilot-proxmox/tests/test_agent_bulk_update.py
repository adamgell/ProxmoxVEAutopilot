"""Bulk agent-software update from the fleet checkbox bar.

POST /api/agents/bulk-update restarts the AutopilotAgent service on each
selected agent's VM through the Proxmox guest agent. The agent already
self-upgrades after every heartbeat (Worker.cs calls
AgentUpdateService.CheckAndApplyOnceAsync each tick), so this exists for the
case that cannot self-heal: a wedged service that has stopped heartbeating,
which is the "Stale" state the fleet table shows most often.

The batch must report per-agent outcomes rather than aborting, because one
unreachable VM in a selection of twenty is normal.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(pg_conn):
    from web.app import app

    with TestClient(app) as test_client:
        yield test_client


def _devices(mapping):
    def get_device(_conn, agent_id):
        return mapping.get(agent_id)

    return get_device


def test_bulk_update_restarts_each_selected_agent(client):
    calls = []

    def fake_exec(node, vmid, ps, timeout_s=20):
        calls.append((node, vmid))
        assert "Restart-Service" in ps
        assert "AutopilotAgent" in ps
        return {"ok": True, "exitcode": 0, "out": "restarted", "err": ""}

    with patch("web.agent_telemetry_pg.get_device", _devices({
        "agent-a": {"agent_id": "agent-a", "vmid": 201},
        "agent-b": {"agent_id": "agent-b", "vmid": 202},
    })), \
         patch("web.app._resolve_vm_node", lambda vmid: "pve2"), \
         patch("web.app._guest_exec_ps_status", fake_exec):
        response = client.post(
            "/api/agents/bulk-update",
            json={"agent_ids": ["agent-a", "agent-b"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["restarted"] == 2
    assert body["requested"] == 2
    assert calls == [("pve2", 201), ("pve2", 202)]
    assert all(entry["restarted"] for entry in body["results"])


def test_bulk_update_reports_per_agent_failures_without_aborting(client):
    def fake_exec(node, vmid, ps, timeout_s=20):
        if vmid == 202:
            return {"ok": False, "error": "guest agent not running", "exitcode": 1}
        return {"ok": True, "exitcode": 0, "out": "restarted", "err": ""}

    with patch("web.agent_telemetry_pg.get_device", _devices({
        "agent-a": {"agent_id": "agent-a", "vmid": 201},
        "agent-b": {"agent_id": "agent-b", "vmid": 202},
        "agent-c": {"agent_id": "agent-c", "vmid": 203},
    })), \
         patch("web.app._resolve_vm_node", lambda vmid: "pve2"), \
         patch("web.app._guest_exec_ps_status", fake_exec):
        response = client.post(
            "/api/agents/bulk-update",
            json={"agent_ids": ["agent-a", "agent-b", "agent-c"]},
        )

    body = response.json()
    # The middle agent failed; the ones on either side of it still ran.
    assert body["restarted"] == 2
    assert body["requested"] == 3
    by_id = {entry["agent_id"]: entry for entry in body["results"]}
    assert by_id["agent-b"]["restarted"] is False
    assert "guest agent not running" in by_id["agent-b"]["error"]
    assert by_id["agent-a"]["restarted"] is True
    assert by_id["agent-c"]["restarted"] is True


def test_bulk_update_skips_agents_with_no_reachable_guest(client):
    with patch("web.agent_telemetry_pg.get_device", _devices({
        "agent-novm": {"agent_id": "agent-novm", "vmid": None},
        "agent-ghost": {"agent_id": "agent-ghost", "vmid": 999},
        "agent-unknown": None,
    })), \
         patch("web.app._resolve_vm_node", lambda vmid: None), \
         patch("web.app._guest_exec_ps_status", lambda *a, **k: pytest.fail("must not exec")):
        response = client.post(
            "/api/agents/bulk-update",
            json={"agent_ids": ["agent-novm", "agent-ghost", "agent-unknown"]},
        )

    body = response.json()
    assert body["restarted"] == 0
    by_id = {entry["agent_id"]: entry for entry in body["results"]}
    assert "no VMID" in by_id["agent-novm"]["error"]
    assert "no cluster node" in by_id["agent-ghost"]["error"]
    assert "not found" in by_id["agent-unknown"]["error"]


def test_bulk_update_rejects_an_empty_selection(client):
    response = client.post("/api/agents/bulk-update", json={"agent_ids": []})
    assert response.json().get("ok") is not True


def test_bulk_update_deduplicates_repeated_ids(client):
    calls = []

    def fake_exec(node, vmid, ps, timeout_s=20):
        calls.append(vmid)
        return {"ok": True, "exitcode": 0, "out": "restarted", "err": ""}

    with patch("web.agent_telemetry_pg.get_device", _devices({
        "agent-a": {"agent_id": "agent-a", "vmid": 201},
    })), \
         patch("web.app._resolve_vm_node", lambda vmid: "pve2"), \
         patch("web.app._guest_exec_ps_status", fake_exec):
        response = client.post(
            "/api/agents/bulk-update",
            json={"agent_ids": ["agent-a", "agent-a", "agent-a"]},
        )

    assert calls == [201]
    assert response.json()["requested"] == 1
