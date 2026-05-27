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


def test_probe_endpoints_stubbed_to_501():
    client = TestClient(app)
    for path in ("tenant", "artifact"):
        r = client.post(f"/api/onboarding/probe/{path}", json={})
        assert r.status_code == 501


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


def test_launch_endpoint_stubbed_to_501():
    client = TestClient(app)
    r = client.post("/api/onboarding/launch", json={})
    assert r.status_code == 501


def test_setup_status_stubbed_to_501():
    client = TestClient(app)
    r = client.get("/api/onboarding/setup-status")
    assert r.status_code == 501
