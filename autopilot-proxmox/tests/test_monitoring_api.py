"""Tests for the /api/monitoring/* endpoints.

Uses the real FastAPI TestClient against app.py with the monitor DB
pointed at a tmp path. The background loop is never started here —
those tests live in test_device_monitor_loop.py."""
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Build a TestClient with DEVICE_MONITOR_DB redirected to a fresh
    tmp DB, so the /api/monitoring/* endpoints operate in isolation."""
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_db

    db_path = tmp_path / "device_monitor.db"
    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", db_path)
    device_history_db.init(db_path)
    with TestClient(app_module.app) as c:
        yield c


def test_settings_get_returns_defaults(client):
    r = client.get("/api/monitoring/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["interval_seconds"] == 900
    assert body["ad_credential_id"] == 0


def test_settings_put_updates_fields(client):
    r = client.put(
        "/api/monitoring/settings",
        json={"interval_seconds": 1200, "ad_credential_id": 7},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["interval_seconds"] == 1200
    assert body["ad_credential_id"] == 7


def test_settings_put_rejects_short_interval(client):
    r = client.put(
        "/api/monitoring/settings", json={"interval_seconds": 10},
    )
    assert r.status_code == 400
    assert "interval_seconds" in r.text


def test_search_ous_list_includes_seeded_default(client):
    r = client.get("/api/monitoring/search-ous")
    assert r.status_code == 200
    ous = r.json()
    assert len(ous) == 1
    assert ous[0]["dn"] == "OU=WorkspaceLabs,DC=home,DC=gell,DC=one"
    assert ous[0]["enabled"] is True


def test_search_ous_create_appends(client):
    r = client.post(
        "/api/monitoring/search-ous",
        json={"dn": "OU=OtherSite,DC=home,DC=gell,DC=one", "label": "Other"},
    )
    assert r.status_code == 201
    ous = client.get("/api/monitoring/search-ous").json()
    assert {o["dn"] for o in ous} == {
        "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
        "OU=OtherSite,DC=home,DC=gell,DC=one",
    }


def test_search_ous_create_rejects_invalid_dn(client):
    r = client.post(
        "/api/monitoring/search-ous",
        json={"dn": "not a dn"},
    )
    assert r.status_code == 400


def test_search_ous_create_rejects_duplicate_dn(client):
    # Default row is already WorkspaceLabs.
    r = client.post(
        "/api/monitoring/search-ous",
        json={"dn": "OU=WorkspaceLabs,DC=home,DC=gell,DC=one"},
    )
    assert r.status_code == 409


def test_search_ous_delete_last_returns_409(client):
    ou_id = client.get("/api/monitoring/search-ous").json()[0]["id"]
    r = client.delete(f"/api/monitoring/search-ous/{ou_id}")
    assert r.status_code == 409
    # Row still present.
    assert len(client.get("/api/monitoring/search-ous").json()) == 1


def test_search_ous_disable_last_enabled_returns_409(client):
    ou_id = client.get("/api/monitoring/search-ous").json()[0]["id"]
    r = client.put(
        f"/api/monitoring/search-ous/{ou_id}", json={"enabled": False},
    )
    assert r.status_code == 409
    assert client.get("/api/monitoring/search-ous").json()[0]["enabled"] is True


def test_search_ous_delete_after_adding_another_succeeds(client):
    client.post(
        "/api/monitoring/search-ous",
        json={"dn": "OU=OtherSite,DC=home,DC=gell,DC=one"},
    )
    first_id = client.get("/api/monitoring/search-ous").json()[0]["id"]
    r = client.delete(f"/api/monitoring/search-ous/{first_id}")
    assert r.status_code == 200
    ous = client.get("/api/monitoring/search-ous").json()
    assert [o["dn"] for o in ous] == ["OU=OtherSite,DC=home,DC=gell,DC=one"]


def test_search_ous_update_reorders_and_relabels(client):
    client.post(
        "/api/monitoring/search-ous",
        json={"dn": "OU=OtherSite,DC=home,DC=gell,DC=one", "sort_order": 5},
    )
    ou_id = client.get("/api/monitoring/search-ous").json()[1]["id"]
    # Use a negative sort_order so the new OU sorts before the
    # seeded default (sort_order=0 — tied would fall through to id).
    r = client.put(
        f"/api/monitoring/search-ous/{ou_id}",
        json={"label": "Remote", "sort_order": -1},
    )
    assert r.status_code == 200
    ous = client.get("/api/monitoring/search-ous").json()
    assert ous[0]["dn"] == "OU=OtherSite,DC=home,DC=gell,DC=one"
    assert ous[0]["label"] == "Remote"
