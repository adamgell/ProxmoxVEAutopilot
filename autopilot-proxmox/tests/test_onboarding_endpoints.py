"""Tests for web/onboarding_endpoints.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import onboarding_pg
from web.app import app


@pytest.fixture(autouse=True)
def _reset(pg_conn, monkeypatch):
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    # The endpoints derive owner_sub from the session; for tests we
    # short-circuit auth by patching the current_user dependency.
    from web import auth, onboarding_endpoints
    app.dependency_overrides[auth.current_user] = lambda: {"sub": "tester@example.com"}
    yield
    app.dependency_overrides.clear()


def test_get_state_returns_404_for_new_operator():
    client = TestClient(app)
    r = client.get("/api/onboarding/state")
    assert r.status_code == 404


def test_put_state_creates_row_and_returns_etag():
    client = TestClient(app)
    r = client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    assert r.status_code == 200
    assert r.headers["ETag"].startswith('W/"')
    body = r.json()
    assert body["persona"] == "lab"
    assert body["current_step"] == "welcome"
    assert body["status"] == "in_progress"


def test_put_state_requires_if_match_on_update():
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    r = client.put("/api/onboarding/state", json={"patch": {"persona": "msp"}})
    assert r.status_code == 428  # Precondition Required


def test_put_state_409_on_stale_if_match():
    client = TestClient(app)
    first = client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    etag = first.headers["ETag"]
    client.put(
        "/api/onboarding/state",
        json={"patch": {"persona": "msp"}},
        headers={"If-Match": etag},
    )
    r = client.put(
        "/api/onboarding/state",
        json={"patch": {"persona": "corp"}},
        headers={"If-Match": etag},  # stale now
    )
    assert r.status_code == 409


def test_delete_state_clears_row():
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    r = client.delete("/api/onboarding/state")
    assert r.status_code == 204
    r2 = client.get("/api/onboarding/state")
    assert r2.status_code == 404


def test_probe_artifact_returns_probe_helper_result(monkeypatch):
    from web import onboarding_probes

    expected = {
        "ok": True,
        "detail": "2 CloudOSD, 1 OSDeploy",
        "cloudosd": [
            {"id": "cosd-1", "label": "CloudOSD 2026-05", "built_at": "2026-05-20T10:00Z"},
            {"id": "cosd-2", "label": "CloudOSD 2026-04", "built_at": "2026-04-15T10:00Z"},
        ],
        "osdeploy": [
            {"id": "osd-1", "label": "OSDeploy 2026-05", "built_at": "2026-05-22T10:00Z"},
        ],
    }
    monkeypatch.setattr(onboarding_probes, "probe_artifact", lambda: expected)
    client = TestClient(app)
    r = client.post("/api/onboarding/probe/artifact")
    assert r.status_code == 200
    assert r.json() == expected


def test_probe_tenant_returns_probe_helper_result(monkeypatch):
    from web import onboarding_probes

    expected = {
        "ok": True,
        "detail": "tenant resolves on login.microsoftonline.com",
        "checks": {"shape": {"ok": True}, "graph": {"ok": True}},
    }
    monkeypatch.setattr(
        onboarding_probes,
        "probe_tenant",
        lambda tenant_id, tenant_domain, graph_check=True: expected,
    )
    client = TestClient(app)
    r = client.post(
        "/api/onboarding/probe/tenant",
        json={
            "tenant_id": "12345678-1234-1234-1234-123456789abc",
            "tenant_domain": "contoso.onmicrosoft.com",
            "graph_check": True,
        },
    )
    assert r.status_code == 200
    assert r.json() == expected


def test_probe_ad_returns_probe_helper_result(monkeypatch):
    from web import onboarding_probes

    expected = {
        "ok": True,
        "detail": "bound as svc-autopilot@home.gell.one",
        "checks": {
            "dns": {"ok": True, "detail": "resolved 192.168.2.10"},
            "icmp": {"ok": True, "detail": "1 round-trip 2ms"},
            "ldap": {"ok": True, "detail": "bound as svc-autopilot@home.gell.one"},
        },
    }
    monkeypatch.setattr(
        onboarding_probes,
        "probe_ad",
        lambda domain, account, password: expected,
    )
    client = TestClient(app)
    r = client.post(
        "/api/onboarding/probe/ad",
        json={"domain": "home.gell.one", "account": "svc-autopilot", "password": "pw"},
    )
    assert r.status_code == 200
    assert r.json() == expected


def test_launch_endpoint_returns_run_id_from_onboarding_launch(monkeypatch):
    from web import onboarding_endpoints, onboarding_launch

    def fake_launch(conn, *, owner_sub):
        assert owner_sub == "tester@example.com"
        return {"run_id": "test-run-1"}

    monkeypatch.setattr(onboarding_endpoints.onboarding_launch, "launch", fake_launch)
    # Belt-and-suspenders: also patch the source module in case any caller
    # reaches it via web.onboarding_launch directly.
    monkeypatch.setattr(onboarding_launch, "launch", fake_launch)
    client = TestClient(app)
    r = client.post("/api/onboarding/launch")
    assert r.status_code == 200
    assert r.json() == {"run_id": "test-run-1"}


def test_launch_endpoint_returns_400_when_launch_raises_value_error(monkeypatch):
    from web import onboarding_endpoints

    def raise_value_error(conn, *, owner_sub):
        raise ValueError("no onboarding row to launch")

    monkeypatch.setattr(onboarding_endpoints.onboarding_launch, "launch", raise_value_error)
    client = TestClient(app)
    r = client.post("/api/onboarding/launch")
    assert r.status_code == 400
    assert r.json()["detail"] == "no onboarding row to launch"


def test_setup_status_stubbed_to_501():
    client = TestClient(app)
    r = client.get("/api/onboarding/setup-status")
    assert r.status_code == 501
