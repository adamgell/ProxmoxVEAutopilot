from __future__ import annotations

import pytest


def test_database_url_requires_env(monkeypatch):
    from web import db_pg

    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres database URL is required"):
        db_pg.database_url()


def test_database_url_prefers_autopilot_database_url(monkeypatch):
    from web import db_pg

    monkeypatch.setenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", "postgresql://old")
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", "postgresql://new")

    assert db_pg.database_url() == "postgresql://new"
