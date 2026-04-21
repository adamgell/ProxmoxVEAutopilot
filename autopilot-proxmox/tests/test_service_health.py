import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "device_monitor.db"


def test_init_creates_table(db_path):
    from web import service_health
    service_health.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "service_health" in names


def test_heartbeat_inserts_row_on_first_call(db_path):
    from web import service_health
    service_health.init(db_path)
    service_health.heartbeat(
        db_path,
        service_id="web", service_type="web",
        version_sha="abc1234", detail="idle",
    )
    rows = service_health.list_services(db_path)
    assert len(rows) == 1
    assert rows[0]["service_id"] == "web"
    assert rows[0]["version_sha"] == "abc1234"
    assert rows[0]["status"] == "ok"


def test_heartbeat_updates_existing_row(db_path):
    from web import service_health
    service_health.init(db_path)
    service_health.heartbeat(db_path, service_id="web",
                             service_type="web", version_sha="a", detail="x")
    import time; time.sleep(1.1)
    service_health.heartbeat(db_path, service_id="web",
                             service_type="web", version_sha="b", detail="y")
    rows = service_health.list_services(db_path)
    assert len(rows) == 1
    assert rows[0]["version_sha"] == "b"
    assert rows[0]["detail"] == "y"


def test_classify_staleness_ok_degraded_dead(db_path):
    from web import service_health
    service_health.init(db_path)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=5)).isoformat(timespec="seconds")
    degraded = (now - timedelta(seconds=30)).isoformat(timespec="seconds")
    dead = (now - timedelta(seconds=90)).isoformat(timespec="seconds")
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        for sid, hb in [("a", fresh), ("b", degraded), ("c", dead)]:
            conn.execute(
                "INSERT INTO service_health "
                "(service_id, service_type, version_sha, started_at, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, "builder", "sha", fresh, hb),
            )
    rows = {r["service_id"]: r["status"]
            for r in service_health.list_services(db_path)}
    assert rows["a"] == "ok"
    assert rows["b"] == "degraded"
    assert rows["c"] == "dead"


def test_prune_dead_workers_removes_old_rows(db_path):
    """Worker rows whose heartbeat is older than 10 minutes get removed
    so /monitoring doesn't accrete ghosts from scaled-down builders."""
    from web import service_health
    service_health.init(db_path)
    now = datetime.now(timezone.utc)
    very_old = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO service_health "
            "(service_id, service_type, version_sha, started_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?)",
            ("builder-xyz", "builder", "sha", very_old, very_old),
        )
    n = service_health.prune_dead_workers(db_path, max_age_seconds=600)
    assert n == 1
    assert service_health.list_services(db_path) == []
