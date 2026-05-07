from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest


@pytest.fixture
def service_health_table(pg_conn):
    from web import service_health_pg as service_health

    service_health.init(pg_conn)
    pg_conn.execute("TRUNCATE service_health")
    pg_conn.commit()
    return service_health


def test_init_creates_table(pg_conn):
    from web import service_health_pg as service_health

    pg_conn.execute("DROP TABLE IF EXISTS service_health")
    pg_conn.commit()
    service_health.init(pg_conn)

    row = pg_conn.execute(
        "SELECT to_regclass('public.service_health') AS name"
    ).fetchone()
    assert row["name"] == "service_health"


def test_heartbeat_inserts_row_on_first_call(service_health_table):
    service_health = service_health_table
    service_health.heartbeat(
        service_id="web",
        service_type="web",
        version_sha="abc1234",
        detail="idle",
    )
    rows = service_health.list_services()
    assert len(rows) == 1
    assert rows[0]["service_id"] == "web"
    assert rows[0]["version_sha"] == "abc1234"
    assert rows[0]["status"] == "ok"


def test_heartbeat_updates_existing_row(service_health_table):
    service_health = service_health_table
    service_health.heartbeat(
        service_id="web", service_type="web", version_sha="a", detail="x"
    )
    time.sleep(0.01)
    service_health.heartbeat(
        service_id="web", service_type="web", version_sha="b", detail="y"
    )
    rows = service_health.list_services()
    assert len(rows) == 1
    assert rows[0]["version_sha"] == "b"
    assert rows[0]["detail"] == "y"


def test_classify_staleness_ok_degraded_dead(pg_conn, service_health_table):
    service_health = service_health_table
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=5)
    degraded = now - timedelta(seconds=30)
    dead = now - timedelta(seconds=90)
    for sid, hb in [("a", fresh), ("b", degraded), ("c", dead)]:
        pg_conn.execute(
            """
            INSERT INTO service_health
                (service_id, service_type, version_sha, started_at, last_heartbeat)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (sid, "builder", "sha", fresh, hb),
        )
    pg_conn.commit()

    rows = {r["service_id"]: r["status"] for r in service_health.list_services()}
    assert rows["a"] == "ok"
    assert rows["b"] == "degraded"
    assert rows["c"] == "dead"


def test_prune_dead_workers_removes_old_rows(pg_conn, service_health_table):
    """Scaled-down builder rows older than the cutoff should disappear."""
    service_health = service_health_table
    very_old = datetime.now(timezone.utc) - timedelta(minutes=15)
    pg_conn.execute(
        """
        INSERT INTO service_health
            (service_id, service_type, version_sha, started_at, last_heartbeat)
        VALUES (%s, %s, %s, %s, %s)
        """,
        ("builder-xyz", "builder", "sha", very_old, very_old),
    )
    pg_conn.commit()

    n = service_health.prune_dead_workers(max_age_seconds=600)

    assert n == 1
    assert service_health.list_services() == []


def test_classify_handles_naive_heartbeat(pg_conn, service_health_table):
    """Defensive: a naive timestamp must be treated as UTC."""
    service_health = service_health_table
    naive_recent = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).replace(tzinfo=None)
    pg_conn.execute(
        """
        INSERT INTO service_health
            (service_id, service_type, version_sha, started_at, last_heartbeat)
        VALUES (%s, %s, %s, %s, %s)
        """,
        ("naive-web", "web", "sha", naive_recent, naive_recent),
    )
    pg_conn.commit()

    rows = service_health.list_services()
    naive_row = next(r for r in rows if r["service_id"] == "naive-web")
    assert naive_row["status"] == "ok"
    assert isinstance(naive_row["age_seconds"], int)
