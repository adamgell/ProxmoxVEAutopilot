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
    r = c.get("/monitoring", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/react/monitoring"

    r = c.get("/api/monitoring/page")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["settings"]["enabled"] is True


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

    r = c.get("/api/monitoring/page")
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["vm_name"] == "Gell-EC41E7EB"
    assert row["win_name"] == "GELL-EC41E7EB"
    assert row["entra_trust_type"] == "ServerAd"
    assert row["intune_compliance"] == "compliant"


def test_monitoring_page_interval_warning_below_15min(client, monkeypatch):
    from web import device_history_pg
    c = client
    device_history_pg.update_settings(interval_seconds=300)
    r = c.get("/api/monitoring/page")
    assert r.json()["settings"]["interval_seconds"] == 300


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
    r = c.get("/api/monitoring/page")
    assert r.status_code == 200
    services = {row["service_id"]: row for row in r.json()["service_health"]}
    assert services["web"]["version_sha"] == "abc1234"
    assert services["builder-xyz"]["detail"] == "running"


def test_monitoring_page_shows_runtime_logs_surface(client, monkeypatch):
    from web import app as app_module

    monkeypatch.setattr(app_module, "_runtime_container_status", lambda: {
        "available": True,
        "error": "",
        "containers": [
            {
                "id": "abc123",
                "name": "autopilot-mcp",
                "service": "autopilot-mcp",
                "image": "ghcr.io/adamgell/proxmox-autopilot:latest",
                "status": "running",
                "health": "healthy",
                "started_at": "2026-05-15T00:00:00Z",
                "finished_at": "",
                "restart_count": 0,
                "log_url": "/api/monitoring/service-logs?container=autopilot-mcp",
            },
        ],
    })

    r = client.get("/api/monitoring/page")

    assert r.status_code == 200
    runtime = r.json()["runtime_services"]
    assert runtime["available"] is True
    assert runtime["containers"][0]["name"] == "autopilot-mcp"
    assert runtime["containers"][0]["log_url"].endswith("container=autopilot-mcp")


def test_monitoring_service_logs_endpoint_redacts_secrets(client, monkeypatch):
    from web import app as app_module

    class FakeContainer:
        name = "autopilot-mcp"
        short_id = "abc123"
        status = "running"
        labels = {"com.docker.compose.service": "autopilot-mcp"}
        attrs = {
            "State": {"Status": "running", "StartedAt": "2026-05-15T00:00:00Z"},
            "Config": {"Image": "ghcr.io/adamgell/proxmox-autopilot:latest"},
        }

        def logs(self, *, tail, timestamps):
            assert tail == 42
            assert timestamps is True
            return b"2026-05-15T00:00:00Z ready token=super-secret\n"

    class FakeContainers:
        def get(self, name):
            assert name == "autopilot-mcp"
            return FakeContainer()

    class FakeClient:
        containers = FakeContainers()

    monkeypatch.setattr(app_module, "_docker_client", lambda: FakeClient())

    r = client.get("/api/monitoring/service-logs?container=autopilot-mcp&tail=42")

    assert r.status_code == 200
    payload = r.json()
    assert payload["container"] == "autopilot-mcp"
    assert payload["service"] == "autopilot-mcp"
    assert payload["lines"] == ["2026-05-15T00:00:00Z ready token=[redacted]"]


def test_monitoring_page_shows_deployment_speed_section(client, pg_conn):
    from web import deployment_health, jobs_pg

    deployment_health.reset_for_tests(pg_conn)
    jobs_pg.enqueue(
        job_id="job-monitoring",
        job_type="cloudosd_build_iso",
        playbook="cloudosd_remote_build",
        cmd=["true"],
        args={"artifact": "cloudosd"},
    )
    jobs_pg.claim_next_job(worker_id="builder-monitoring")
    jobs_pg.finalize_job("job-monitoring", exit_code=0)

    r = client.get("/api/monitoring/page")

    assert r.status_code == 200
    runs = r.json()["deployment_health"]["runs"]
    assert any(run["deployment_key"] == "job:job-monitoring" for run in runs)
