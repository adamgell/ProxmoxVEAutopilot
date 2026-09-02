from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_init_indexes_latest_heartbeat_lookup_by_run(pg_conn):
    """A run lookup must not scan the full heartbeat history."""
    from web import agent_telemetry_pg

    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)

    index = pg_conn.execute(
        """
        SELECT i.indisvalid,
               i.indisready,
               i.indnkeyatts,
               i.indoption::smallint[] AS sort_options,
               ARRAY(
                   SELECT pg_get_indexdef(i.indexrelid, position, true)
                   FROM generate_series(1, i.indnatts) AS position
                   ORDER BY position
               ) AS columns
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'idx_agent_heartbeats_run_time'
        """
    ).fetchone()

    assert index == {
        "indisvalid": True,
        "indisready": True,
        "indnkeyatts": 3,
        # PostgreSQL btree indoption bit 0 is DESC and bit 1 is NULLS FIRST.
        "sort_options": [0, 3, 3],
        "columns": [
            "current_run_id",
            "received_at",
            "id",
            "agent_id",
        ],
    }


def test_latest_by_vmid_omits_historical_vmids_no_longer_assigned(pg_conn):
    """Current inventory must not resurrect a VMID an agent moved away from."""
    from web import agent_telemetry_pg

    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-reassigned",
        token="agent-token",
        vmid=202,
    )
    now = datetime.now(timezone.utc)
    pg_conn.execute(
        """
        INSERT INTO agent_heartbeats (agent_id, received_at, vmid)
        VALUES (%s, %s, %s), (%s, %s, %s)
        """,
        (
            "agent-reassigned",
            now - timedelta(minutes=1),
            101,
            "agent-reassigned",
            now,
            202,
        ),
    )
    pg_conn.commit()

    latest = agent_telemetry_pg.latest_by_vmid(pg_conn)

    assert set(latest) == {202}
    assert latest[202]["agent_id"] == "agent-reassigned"


def test_latest_by_vmid_uses_current_owner_when_vmid_is_reassigned(pg_conn):
    """A moved agent's newer historical row must not beat the VM's owner."""
    from web import agent_telemetry_pg

    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-moved",
        token="moved-token",
        vmid=202,
    )
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-current",
        token="current-token",
        vmid=101,
    )
    now = datetime.now(timezone.utc)
    pg_conn.execute(
        """
        INSERT INTO agent_heartbeats (agent_id, received_at, vmid)
        VALUES (%s, %s, %s), (%s, %s, %s), (%s, %s, %s)
        """,
        (
            "agent-current",
            now - timedelta(minutes=2),
            101,
            "agent-moved",
            now - timedelta(minutes=1),
            101,
            "agent-moved",
            now,
            202,
        ),
    )
    pg_conn.commit()

    latest = agent_telemetry_pg.latest_by_vmid(pg_conn)

    assert latest[101]["agent_id"] == "agent-current"
    assert latest[202]["agent_id"] == "agent-moved"
