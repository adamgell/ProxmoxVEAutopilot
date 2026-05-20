from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml


@pytest.mark.real_app_database_startup
def test_registered_startup_initializes_app_database_url(monkeypatch):
    from fastapi.testclient import TestClient
    from web import app as web_app
    from web import (
        agent_telemetry_pg,
        cloudosd_cache,
        cloudosd_pg,
        db_pg,
        deployment_health_pg,
        device_history_pg,
        devices_pg,
        jobs_pg,
        lab_bubbles_pg,
        machine_lifecycle_pg,
        osdeploy_cache,
        osdeploy_pg,
        sequences_pg,
        service_health_pg,
        ts_engine_pg,
    )

    calls = []

    class FakeConn:
        pass

    @contextmanager
    def fake_connection(dsn):
        calls.append(("connect", dsn))
        yield FakeConn()

    def fake_ts_init(conn):
        calls.append(("ts_init", conn.__class__.__name__))

    def fake_jobs_init(conn):
        calls.append(("jobs_init", conn.__class__.__name__))

    def fake_sequences_init(conn):
        calls.append(("sequences_init", conn.__class__.__name__))

    def fake_seed_defaults(_handle, _cipher):
        calls.append(("sequences_seed", "ok"))

    def fake_service_health_init(conn):
        calls.append(("service_health_init", conn.__class__.__name__))

    def fake_device_history_init(conn):
        calls.append(("device_history_init", conn.__class__.__name__))

    def fake_devices_init(conn):
        calls.append(("devices_init", conn.__class__.__name__))

    def fake_machine_lifecycle_init(conn):
        calls.append(("machine_lifecycle_init", conn.__class__.__name__))

    def fake_agent_telemetry_init(conn):
        calls.append(("agent_telemetry_init", conn.__class__.__name__))

    def fake_cloudosd_init(conn):
        calls.append(("cloudosd_init", conn.__class__.__name__))

    def fake_cloudosd_cache_init(conn):
        calls.append(("cloudosd_cache_init", conn.__class__.__name__))

    def fake_osdeploy_init(conn):
        calls.append(("osdeploy_init", conn.__class__.__name__))

    def fake_osdeploy_cache_init(conn):
        calls.append(("osdeploy_cache_init", conn.__class__.__name__))

    def fake_deployment_health_init(conn):
        calls.append(("deployment_health_init", conn.__class__.__name__))

    def fake_lab_bubbles_init(conn):
        calls.append(("lab_bubbles_init", conn.__class__.__name__))

    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", "postgresql://new")
    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)
    monkeypatch.setattr(db_pg, "connection", fake_connection)
    monkeypatch.setattr(jobs_pg, "init", fake_jobs_init)
    monkeypatch.setattr(sequences_pg, "init", fake_sequences_init)
    monkeypatch.setattr(sequences_pg, "seed_defaults", fake_seed_defaults)
    monkeypatch.setattr(service_health_pg, "init", fake_service_health_init)
    monkeypatch.setattr(ts_engine_pg, "init", fake_ts_init)
    monkeypatch.setattr(device_history_pg, "init", fake_device_history_init)
    monkeypatch.setattr(devices_pg, "init", fake_devices_init)
    monkeypatch.setattr(machine_lifecycle_pg, "init", fake_machine_lifecycle_init)
    monkeypatch.setattr(agent_telemetry_pg, "init", fake_agent_telemetry_init)
    monkeypatch.setattr(cloudosd_pg, "init", fake_cloudosd_init)
    monkeypatch.setattr(cloudosd_cache, "init", fake_cloudosd_cache_init)
    monkeypatch.setattr(osdeploy_pg, "init", fake_osdeploy_init)
    monkeypatch.setattr(osdeploy_cache, "init", fake_osdeploy_cache_init)
    monkeypatch.setattr(deployment_health_pg, "init", fake_deployment_health_init)
    monkeypatch.setattr(lab_bubbles_pg, "init", fake_lab_bubbles_init)

    with TestClient(web_app.app):
        pass

    assert calls == [
        ("connect", "postgresql://new"),
        ("sequences_init", "FakeConn"),
        ("sequences_seed", "ok"),
        ("connect", "postgresql://new"),
        ("jobs_init", "FakeConn"),
        ("service_health_init", "FakeConn"),
        ("connect", "postgresql://new"),
        ("ts_init", "FakeConn"),
        ("device_history_init", "FakeConn"),
        ("devices_init", "FakeConn"),
        ("machine_lifecycle_init", "FakeConn"),
        ("agent_telemetry_init", "FakeConn"),
        ("cloudosd_init", "FakeConn"),
        ("cloudosd_cache_init", "FakeConn"),
        ("osdeploy_init", "FakeConn"),
        ("osdeploy_cache_init", "FakeConn"),
        ("deployment_health_init", "FakeConn"),
        ("lab_bubbles_init", "FakeConn"),
    ]


@pytest.mark.real_app_database_startup
def test_registered_startup_requires_database_url(monkeypatch):
    from fastapi.testclient import TestClient
    from web import app as web_app

    monkeypatch.delenv("AUTOPILOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres database URL is required"):
        with TestClient(web_app.app):
            pass


def test_app_database_startup_requires_database_url(monkeypatch):
    from web import app as web_app

    monkeypatch.delenv("AUTOPILOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres database URL is required"):
        web_app._database_url()


def test_ts_engine_startup_skips_when_database_url_is_unset(monkeypatch):
    from web import app as web_app

    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)

    assert web_app._init_ts_engine_database_if_configured() is False


def test_ts_engine_startup_initializes_postgres_when_database_url_is_set(monkeypatch):
    from web import app as web_app
    from web import ts_engine_pg

    calls = []

    class FakeConn:
        pass

    @contextmanager
    def fake_connect(dsn):
        calls.append(("connect", dsn))
        yield FakeConn()

    def fake_init(conn):
        calls.append(("init", conn.__class__.__name__))

    monkeypatch.setenv(
        "AUTOPILOT_TS_ENGINE_DATABASE_URL",
        "postgresql://autopilot:secret@127.0.0.1:5432/autopilot",
    )
    monkeypatch.setattr(ts_engine_pg, "connect", fake_connect)
    monkeypatch.setattr(ts_engine_pg, "init", fake_init)

    assert web_app._init_ts_engine_database_if_configured() is True
    assert calls == [
        ("connect", "postgresql://autopilot:secret@127.0.0.1:5432/autopilot"),
        ("init", "FakeConn"),
    ]


def test_docker_compose_declares_postgres_for_task_sequence_engine():
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())

    postgres = compose["services"]["autopilot-postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert "127.0.0.1:5432:5432" in postgres["ports"]
    assert "autopilot-postgres:/var/lib/postgresql/data" in postgres["volumes"]
    assert "apparmor=unconfined" in postgres["security_opt"]
    assert postgres["healthcheck"]["test"] == [
        "CMD-SHELL",
        (
            "pg_isready -U autopilot -d autopilot && "
            "psql -U autopilot -d autopilot -tAc 'SELECT 1' >/dev/null"
        ),
    ]

    autopilot = compose["services"]["autopilot"]
    assert autopilot["depends_on"]["autopilot-postgres"]["condition"] == (
        "service_healthy"
    )
    assert (
        "AUTOPILOT_TS_ENGINE_DATABASE_URL"
        in autopilot["environment"]
    )
    assert "autopilot-postgres" in compose["volumes"]


def test_docker_compose_persists_osdeploy_cache_for_all_runtime_services():
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())

    expected_mount = "./cache/osdeploy:/app/cache/osdeploy"
    for service_name in ("autopilot", "autopilot-builder", "autopilot-monitor", "autopilot-mcp"):
        service = compose["services"][service_name]
        assert expected_mount in service["volumes"]
        assert (
            service["environment"]["AUTOPILOT_OSDEPLOY_CACHE_ROOT"]
            == "${AUTOPILOT_OSDEPLOY_CACHE_ROOT:-/app/cache/osdeploy}"
        )
