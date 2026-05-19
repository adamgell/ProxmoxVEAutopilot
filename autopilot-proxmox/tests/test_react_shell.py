import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from web import auth


def test_react_shell_auth_boundary_is_narrow():
    assert auth.is_exempt_path("/static/react/assets/app.js")
    assert not auth.is_exempt_path("/react-shell")
    assert not auth.is_exempt_path("/react/dashboard")
    assert not auth.is_exempt_path("/react/jobs")
    assert not auth.is_exempt_path("/react/monitoring")
    assert not auth.is_exempt_path("/react")
    assert not auth.is_exempt_path("/app")
    assert not auth.is_exempt_path("/app/jobs")
    assert not auth.is_exempt_path("/openapi.json")


@pytest.mark.parametrize("path", ["/react-shell", "/react/dashboard", "/react/jobs", "/react/monitoring"])
def test_react_shell_routes_render_authenticated_bootstrap(web_client, path):
    response = web_client.get(path)

    assert response.status_code == 200
    assert 'id="react-root"' in response.text
    assert 'data-react-shell="protected"' in response.text
    assert "Proxmox VE Autopilot" in response.text


def test_react_read_api_response_shapes(web_client):
    jobs = web_client.get("/api/jobs")
    assert jobs.status_code == 200
    assert isinstance(jobs.json(), list)

    running = web_client.get("/api/jobs/running")
    assert running.status_code == 200
    assert set(running.json()) >= {"running", "running_count", "queued_count"}
    assert isinstance(running.json()["running"], list)

    recent = web_client.get("/api/jobs/recent?limit=5")
    assert recent.status_code == 200
    assert set(recent.json()) >= {"jobs"}
    assert isinstance(recent.json()["jobs"], list)

    services = web_client.get("/api/services")
    assert services.status_code == 200
    assert set(services.json()) >= {"services", "available"}
    assert isinstance(services.json()["services"], list)

    fleet = web_client.get("/api/fleet/summary")
    assert fleet.status_code == 200
    assert "total" in fleet.json()

    summary = web_client.get("/api/cockpit/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert set(body) >= {
        "readiness_score",
        "jobs",
        "recent_jobs",
        "services",
        "fleet",
        "monitoring",
    }
    assert set(body["jobs"]) >= {"running", "running_count", "queued_count"}


def test_live_jobs_payload_contract(web_client):
    from web import app as web_app

    payload = web_app._live_jobs_payload()

    assert set(payload) >= {"running", "recent", "table", "generated_at"}
    assert set(payload["running"]) >= {"running", "running_count", "queued_count"}
    assert "jobs" in payload["recent"]
    assert "jobs" in payload["table"]


def test_observe_monitoring_api_response_shapes(web_client):
    runtime = web_client.get("/api/monitoring/runtime-services")
    assert runtime.status_code == 200
    runtime_body = runtime.json()
    assert set(runtime_body) >= {"available", "error", "containers"}
    assert isinstance(runtime_body["containers"], list)

    deployments = web_client.get("/api/monitoring/deployments/summary")
    assert deployments.status_code == 200
    assert set(deployments.json()) >= {"total", "running", "succeeded", "failed"}

    signals = web_client.get("/api/monitoring/signals")
    assert signals.status_code == 200
    signals_body = signals.json()
    assert set(signals_body) >= {
        "generated_at",
        "build",
        "metrics",
        "signals",
        "operator_paths",
        "lifecycle_lanes",
        "deployment_health",
        "services",
        "runtime",
        "fleet_attention",
    }
    assert isinstance(signals_body["metrics"], list)
    assert isinstance(signals_body["signals"], list)
    assert isinstance(signals_body["operator_paths"], list)
    assert isinstance(signals_body["lifecycle_lanes"], list)
    assert set(signals_body["deployment_health"]) >= {"summary", "active", "recent_completions", "bottlenecks"}
    assert isinstance(signals_body["services"], list)
    assert set(signals_body["runtime"]) >= {"available", "error", "containers"}
    assert isinstance(signals_body["fleet_attention"], list)
    assert {
        "runtime",
        "service_health",
        "jobs",
        "build_host",
        "artifacts",
        "deploy_readiness",
        "deployment_speed",
        "agent",
        "lifecycle",
        "identity",
        "fleet_evidence",
    }.issubset({item["family"] for item in signals_body["signals"]})
    assert any(path["href"].startswith(("/react/", "/cloudosd", "/osdeploy", "/setup", "/vms", "/devices", "/hashes")) for path in signals_body["operator_paths"])


def test_openapi_export_script_uses_local_app_import(tmp_path):
    output = tmp_path / "openapi.json"
    env = os.environ.copy()
    env["AUTOPILOT_AUTH_BYPASS"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/export_openapi_schema.py",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    schema = json.loads(output.read_text(encoding="utf-8"))
    assert schema["info"]["title"] == "Proxmox VE Autopilot"
    assert "/api/version" in schema["paths"]
