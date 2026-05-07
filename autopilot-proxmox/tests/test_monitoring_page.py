"""Smoke test for GET /monitoring — renders seeded state end-to-end."""

import pytest


@pytest.fixture
def client(pg_conn):
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_pg, service_health_pg
    device_history_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
    service_health_pg.init(pg_conn)
    pg_conn.execute("TRUNCATE service_health")
    pg_conn.commit()
    with TestClient(app_module.app) as c:
        yield c


def test_monitoring_page_empty_state(client):
    c = client
    r = c.get("/monitoring")
    assert r.status_code == 200
    assert "Device monitoring" in r.text
    assert "No devices probed yet" in r.text
    # Badge for enabled monitor renders.
    assert "enabled" in r.text
    # Nav link visible — the operator-cockpit redesign wrapped nav links
    # with class attributes, so match the href + label substrings
    # separately instead of the bare anchor tag.
    assert 'href="/monitoring"' in r.text
    assert "Monitoring" in r.text


def test_monitoring_page_renders_a_row_with_badges(client):
    from web import device_history_pg
    c = client
    sweep_id = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep_id, {
        "vmid": 116, "status": "running", "node": "pve2",
        "name": "Gell-EC41E7EB", "config_digest": "abc",
        "checked_at": "2026-04-20T23:55:00+00:00",
    })
    device_history_pg.insert_device_probe(sweep_id, {
        "vmid": 116, "win_name": "GELL-EC41E7EB",
        "serial": "Gell-EC41E7EB",
        "ad_found": True, "ad_match_count": 1,
        "ad_matches_json": [
            {"distinguishedName": "CN=GELL-EC41E7EB,OU=Devices,OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
             "userAccountControl": 4096},
        ],
        "entra_found": True, "entra_match_count": 1,
        "entra_matches_json": [{"trustType": "ServerAd"}],
        "intune_found": True, "intune_match_count": 1,
        "intune_matches_json": [{"complianceState": "compliant"}],
        "probe_errors_json": {},
        "checked_at": "2026-04-20T23:55:00+00:00",
    })
    device_history_pg.finish_sweep(sweep_id, vm_count=1)

    r = c.get("/monitoring")
    assert r.status_code == 200
    assert "Gell-EC41E7EB" in r.text
    assert "GELL-EC41E7EB" in r.text
    assert "ServerAd" in r.text        # trust-type pill visible
    assert "compliant" in r.text       # Intune status pill visible
    assert 'href="/devices/116"' in r.text  # links through
    # All three columns got OK badges.
    assert r.text.count('class="badge ok"') >= 3


def test_monitoring_page_interval_warning_below_15min(client, monkeypatch):
    from web import device_history_pg
    c = client
    device_history_pg.update_settings(interval_seconds=300)
    r = c.get("/monitoring")
    assert "aggressive" in r.text


def test_monitoring_page_shows_service_health(client):
    from web import service_health_pg as service_health
    c = client
    service_health.heartbeat(
        service_id="web", service_type="web",
        version_sha="abc1234", detail="idle",
    )
    service_health.heartbeat(
        service_id="builder-xyz", service_type="builder",
        version_sha="abc1234", detail="running",
    )
    r = c.get("/monitoring")
    assert r.status_code == 200
    assert "web" in r.text
    assert "builder-xyz" in r.text
    assert "abc1234" in r.text
