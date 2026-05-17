from fastapi.testclient import TestClient


def _client(pg_conn, pg_dsn, monkeypatch):
    from web import install_tracking_pg

    install_tracking_pg.reset_for_tests(pg_conn)
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", pg_dsn)
    from web.app import app

    return TestClient(app)


def test_install_tracking_api_seeds_pvetest_checklist(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    response = client.get("/api/install-tracking")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 1
    ids = {item["item_id"] for item in payload["items"]}
    assert "pve-foundation" in ids
    assert "osdeploy-e2e-run" in ids
    assert payload["summary"]["total"] >= 8
    osdeploy = next(item for item in payload["items"] if item["item_id"] == "osdeploy-e2e-run")
    assert osdeploy["target"] == "VMID 106"
    assert osdeploy["evidence"]["run_id"] == "d6376517-2306-49ea-bfbe-228ed6cb499a"


def test_install_tracking_update_records_event_and_redacts_secrets(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    response = client.post(
        "/api/install-tracking/items/windows-build-box",
        json={
            "status": "running",
            "detail": "seeding artifacts from build box",
            "source": "pvetest",
            "evidence": {"password": "do-not-store", "artifact": "winpe.iso"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["status"] == "running"
    assert body["item"]["evidence"]["password"] == "[redacted]"
    assert body["item"]["evidence"]["artifact"] == "winpe.iso"

    listing = client.get("/api/install-tracking").json()
    event = listing["events"][0]
    assert event["item_id"] == "windows-build-box"
    assert event["status"] == "running"
    assert event["evidence"]["password"] == "[redacted]"


def test_install_tracking_page_renders_nav_and_table(pg_conn, pg_dsn, monkeypatch):
    client = _client(pg_conn, pg_dsn, monkeypatch)

    response = client.get("/install-tracking")

    assert response.status_code == 200
    assert "Install Tracking" in response.text
    assert "Clean OSDeploy run completes" in response.text
    assert 'href="/install-tracking"' in response.text
