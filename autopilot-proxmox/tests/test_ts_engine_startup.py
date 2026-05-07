from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import yaml


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
    assert postgres["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -U autopilot -d autopilot",
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
