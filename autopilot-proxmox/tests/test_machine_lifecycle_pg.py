from __future__ import annotations


def _event_states(conn, identity_key: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT state
        FROM machine_lifecycle_events
        WHERE identity_key = %s
        ORDER BY observed_at ASC, id ASC
        """,
        (identity_key,),
    ).fetchall()
    return [row["state"] for row in rows]


def test_agent_heartbeat_records_workgroup_unenrolled_current_state(pg_conn):
    from web import agent_telemetry_pg, machine_lifecycle_pg

    agent_telemetry_pg.reset_for_tests(pg_conn)
    machine_lifecycle_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    machine_lifecycle_pg.init(pg_conn)
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-workgroup",
        token="agent-token",
        vmid=105,
        serial_number="WRKGRP-8F47E090",
        computer_name="WRKGRP-8F47E090",
    )

    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-workgroup",
        payload={
            "vmid": 105,
            "computer_name": "WRKGRP-8F47E090",
            "serial_number": "WRKGRP-8F47E090",
            "domain_name": "WORKGROUP",
            "domain_joined": False,
            "entra_joined": False,
        },
    )

    current = machine_lifecycle_pg.current_for_vm(pg_conn, 105)

    assert current is not None
    assert current["state"] == "workgroup_unenrolled"
    assert current["label"] == "unenrolled"
    assert current["source"] == "agent_heartbeat"
    assert current["vmid"] == 105
    assert current["agent_id"] == "agent-workgroup"
    assert current["domain_joined"] is False
    assert current["entra_joined"] is False
    assert _event_states(pg_conn, current["identity_key"]) == ["workgroup_unenrolled"]


def test_agent_heartbeat_appends_event_only_when_lifecycle_state_changes(pg_conn):
    from web import agent_telemetry_pg, machine_lifecycle_pg

    agent_telemetry_pg.reset_for_tests(pg_conn)
    machine_lifecycle_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    machine_lifecycle_pg.init(pg_conn)
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-transition",
        token="agent-token",
        vmid=106,
        serial_number="WRKGRP-DOMAIN",
        computer_name="WRKGRP-DOMAIN",
    )

    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-transition",
        payload={
            "vmid": 106,
            "computer_name": "WRKGRP-DOMAIN",
            "serial_number": "WRKGRP-DOMAIN",
            "domain_name": "WORKGROUP",
            "domain_joined": False,
            "entra_joined": False,
        },
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-transition",
        payload={
            "vmid": 106,
            "computer_name": "WRKGRP-DOMAIN",
            "serial_number": "WRKGRP-DOMAIN",
            "domain_name": "WORKGROUP",
            "domain_joined": False,
            "entra_joined": False,
        },
    )
    agent_telemetry_pg.record_heartbeat(
        pg_conn,
        agent_id="agent-transition",
        payload={
            "vmid": 106,
            "computer_name": "WRKGRP-DOMAIN",
            "serial_number": "WRKGRP-DOMAIN",
            "domain_name": "home.gell.one",
            "domain_joined": True,
            "entra_joined": False,
        },
    )

    current = machine_lifecycle_pg.current_for_vm(pg_conn, 106)

    assert current is not None
    assert current["state"] == "ad_domain_joined"
    assert current["label"] == "domain"
    assert current["domain_name"] == "home.gell.one"
    assert _event_states(pg_conn, current["identity_key"]) == [
        "workgroup_unenrolled",
        "ad_domain_joined",
    ]


def test_monitor_probe_records_intune_enrolled_current_state(pg_conn):
    from web import device_history_pg, machine_lifecycle_pg

    device_history_pg.reset_for_tests(pg_conn)
    machine_lifecycle_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
    machine_lifecycle_pg.init(pg_conn)
    sweep_id = device_history_pg.start_sweep()
    device_history_pg.insert_device_probe(
        sweep_id,
        {
            "vmid": 108,
            "vm_name": "Gell-60F03E42",
            "win_name": "GELL-60F03E42",
            "serial": "Gell-60F03E42",
            "ad_found": True,
            "ad_match_count": 1,
            "ad_matches": [{"domain": "home.gell.one"}],
            "entra_found": True,
            "entra_match_count": 1,
            "entra_matches": [{"trustType": "ServerAD"}],
            "intune_found": True,
            "intune_match_count": 1,
            "intune_matches": [{"complianceState": "compliant"}],
        },
    )

    current = machine_lifecycle_pg.current_for_vm(pg_conn, 108)

    assert current is not None
    assert current["state"] == "intune_enrolled"
    assert current["label"] == "Intune"
    assert current["source"] == "monitor_probe"
    assert current["domain_joined"] is True
    assert current["entra_joined"] is True
    assert current["intune_enrolled"] is True


def test_monitor_probe_without_directory_evidence_records_workgroup_unenrolled(pg_conn):
    from web import device_history_pg, machine_lifecycle_pg

    device_history_pg.reset_for_tests(pg_conn)
    machine_lifecycle_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
    machine_lifecycle_pg.init(pg_conn)
    sweep_id = device_history_pg.start_sweep()
    device_history_pg.insert_device_probe(
        sweep_id,
        {
            "vmid": 109,
            "vm_name": "WrkGrp-8F47E090",
            "win_name": "WRKGRP-8F47E090",
            "serial": "WrkGrp-8F47E090",
            "ad_found": False,
            "entra_found": False,
            "intune_found": False,
        },
    )

    current = machine_lifecycle_pg.current_for_vm(pg_conn, 109)

    assert current is not None
    assert current["state"] == "workgroup_unenrolled"
    assert current["label"] == "unenrolled"
