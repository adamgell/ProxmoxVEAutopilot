from __future__ import annotations


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
