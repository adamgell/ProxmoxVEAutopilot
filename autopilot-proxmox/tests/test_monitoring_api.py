"""Tests for the /api/monitoring/* endpoints.

Uses the real FastAPI TestClient against app.py and the Postgres-backed
monitoring repository. The background loop is never started here."""

import pytest


@pytest.fixture
def client(pg_conn):
    """Build a TestClient with fresh Postgres monitoring tables."""
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_pg

    device_history_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
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


def test_fleet_summary_uses_latest_postgres_sweep(client):
    from web import device_history_pg

    old = device_history_pg.start_sweep()
    device_history_pg.insert_device_probe(old, {
        "vmid": 1,
        "ad_found": True,
        "ad_match_count": 1,
        "entra_found": True,
        "entra_match_count": 1,
        "intune_found": True,
        "intune_match_count": 1,
    })
    device_history_pg.finish_sweep(old, vm_count=1)

    latest = device_history_pg.start_sweep()
    device_history_pg.insert_device_probe(latest, {
        "vmid": 2,
        "ad_found": True,
        "ad_match_count": 1,
        "entra_found": False,
        "entra_match_count": 0,
        "intune_found": False,
        "intune_match_count": 0,
    })
    device_history_pg.insert_device_probe(latest, {
        "vmid": 3,
        "ad_found": False,
        "ad_match_count": 0,
        "entra_found": True,
        "entra_match_count": 1,
        "intune_found": True,
        "intune_match_count": 1,
    })
    device_history_pg.finish_sweep(latest, vm_count=2)

    r = client.get("/api/fleet/summary")

    assert r.status_code == 200
    assert r.json() == {
        "total": 2,
        "ad_joined_pct": 50,
        "autopilot_pct": 50,
        "intune_pct": 50,
    }


def test_latest_monitor_sweep_status_reads_postgres(client):
    from web import app as app_module, device_history_pg

    sweep = device_history_pg.start_sweep()
    device_history_pg.finish_sweep(sweep, vm_count=4)

    status = app_module._latest_monitor_sweep_status()

    assert status["id"] == sweep
    assert status["vm_count"] == 4
    assert status["running"] is False
    assert status["started_at"]
    assert status["ended_at"]


def test_deployment_speed_api_returns_normalized_rows(client, pg_conn):
    from web import deployment_health, jobs_pg

    deployment_health.reset_for_tests(pg_conn)
    jobs_pg.enqueue(
        job_id="job-api",
        job_type="provision_clone",
        playbook="provision.yml",
        cmd=["true"],
        args={"vmid": 121},
    )
    jobs_pg.claim_next_job(worker_id="builder-api")
    jobs_pg.finalize_job("job-api", exit_code=0)

    summary = client.get("/api/monitoring/deployments/summary")
    rows = client.get("/api/monitoring/deployments/runs")
    detail = client.get("/api/monitoring/deployments/runs/job:job-api")
    baselines = client.get("/api/monitoring/deployments/baselines")

    assert summary.status_code == 200
    assert rows.status_code == 200
    assert detail.status_code == 200
    assert baselines.status_code == 200
    assert summary.json()["total"] >= 1
    assert any(row["deployment_key"] == "job:job-api" for row in rows.json()["runs"])
    assert detail.json()["deployment_key"] == "job:job-api"
    assert detail.json()["phases"]
