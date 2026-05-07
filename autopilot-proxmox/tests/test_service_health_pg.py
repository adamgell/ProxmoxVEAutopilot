from __future__ import annotations


def test_heartbeat_upserts_and_classifies(pg_conn):
    from web import service_health_pg

    service_health_pg.init(pg_conn)
    pg_conn.execute("TRUNCATE service_health")
    pg_conn.commit()
    service_health_pg.heartbeat(
        service_id="web-1",
        service_type="web",
        version_sha="abc123",
        detail="ready",
    )
    rows = service_health_pg.list_services()

    assert rows[0]["service_id"] == "web-1"
    assert rows[0]["service_type"] == "web"
    assert rows[0]["version_sha"] == "abc123"
    assert rows[0]["status"] == "ok"
