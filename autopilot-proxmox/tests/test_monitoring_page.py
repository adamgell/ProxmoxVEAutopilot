"""Smoke test for GET /monitoring — renders seeded state end-to-end."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_db
    db_path = tmp_path / "device_monitor.db"
    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", db_path)
    device_history_db.init(db_path)
    with TestClient(app_module.app) as c:
        yield c, db_path


def test_monitoring_page_empty_state(client):
    c, _ = client
    r = c.get("/monitoring")
    assert r.status_code == 200
    assert "Device monitoring" in r.text
    assert "No devices probed yet" in r.text
    # Badge for enabled monitor renders.
    assert "enabled" in r.text
    # Nav link visible.
    assert '<a href="/monitoring">Monitoring</a>' in r.text


def test_monitoring_page_renders_a_row_with_badges(client):
    from web import device_history_db
    c, db = client
    sweep_id = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep_id, {
        "vmid": 116, "status": "running", "node": "pve2",
        "name": "Gell-EC41E7EB", "config_digest": "abc",
        "checked_at": "2026-04-20T23:55:00+00:00",
    })
    device_history_db.insert_device_probe(db, sweep_id, {
        "vmid": 116, "win_name": "GELL-EC41E7EB",
        "serial": "Gell-EC41E7EB",
        "ad_found": 1, "ad_match_count": 1,
        "ad_matches_json": json.dumps([
            {"distinguishedName": "CN=GELL-EC41E7EB,OU=Devices,OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
             "userAccountControl": 4096},
        ]),
        "entra_found": 1, "entra_match_count": 1,
        "entra_matches_json": json.dumps([{"trustType": "ServerAd"}]),
        "intune_found": 1, "intune_match_count": 1,
        "intune_matches_json": json.dumps([{"complianceState": "compliant"}]),
        "probe_errors_json": "{}",
        "checked_at": "2026-04-20T23:55:00+00:00",
    })
    device_history_db.finish_sweep(db, sweep_id, vm_count=1)

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
    from web import device_history_db
    c, db = client
    device_history_db.update_settings(db, interval_seconds=300)
    r = c.get("/monitoring")
    assert "aggressive" in r.text


def test_monitoring_page_shows_service_health(client):
    from web import service_health
    c, db = client
    service_health.init(db)
    service_health.heartbeat(
        db,
        service_id="web", service_type="web",
        version_sha="abc1234", detail="idle",
    )
    service_health.heartbeat(
        db,
        service_id="builder-xyz", service_type="builder",
        version_sha="abc1234", detail="running",
    )
    r = c.get("/monitoring")
    assert r.status_code == 200
    assert "web" in r.text
    assert "builder-xyz" in r.text
    assert "abc1234" in r.text
