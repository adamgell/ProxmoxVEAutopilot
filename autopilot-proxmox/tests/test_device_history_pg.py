def test_latest_per_vmid_joins_snapshot_and_probe(pg_conn):
    from web import device_history_pg

    device_history_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
    sweep_id = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep_id, {
        "vmid": 108,
        "present": True,
        "node": "pve2",
        "name": "Gell-60F03E42",
        "status": "running",
        "tags": ["autopilot"],
        "config_digest": "abc",
    })
    device_history_pg.insert_device_probe(sweep_id, {
        "vmid": 108,
        "vm_name": "Gell-60F03E42",
        "win_name": "Gell-60F03E42",
        "serial": "Gell-60F03E42",
        "entra_found": True,
        "entra_match_count": 1,
        "entra_matches": [{"trustType": "AzureAD"}],
    })
    device_history_pg.finish_sweep(sweep_id, vm_count=1)

    rows = device_history_pg.latest_per_vmid()

    assert rows[0]["vmid"] == 108
    assert rows[0]["pve"]["name"] == "Gell-60F03E42"
    assert rows[0]["probe"]["entra_found"] is True


def test_latest_per_vmid_does_not_pair_current_pve_with_stale_probe(pg_conn):
    from web import device_history_pg

    device_history_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)

    old_sweep = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(old_sweep, {
        "vmid": 109,
        "status": "running",
        "name": "Gell-C368D9DC",
        "config_digest": "old",
    })
    device_history_pg.insert_device_probe(old_sweep, {
        "vmid": 109,
        "win_name": "GELL-C368D9DC",
        "entra_found": True,
        "entra_match_count": 1,
        "entra_matches": [{"trustType": "AzureAD"}],
    })
    device_history_pg.finish_sweep(old_sweep, vm_count=1)

    current_sweep = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(current_sweep, {
        "vmid": 109,
        "status": "running",
        "name": "Gell-C368D9DC",
        "config_digest": "current",
    })
    device_history_pg.finish_sweep(current_sweep, vm_count=1)

    rows = device_history_pg.latest_per_vmid()

    assert len(rows) == 1
    assert rows[0]["vmid"] == 109
    assert rows[0]["pve"]["config_digest"] == "current"
    assert rows[0]["probe"] is None
