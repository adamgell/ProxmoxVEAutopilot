from web import managed_labs_pg, managed_labs_reconciler


def _lab(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    return managed_labs_pg.create_lab(
        pg_conn,
        name="NTT Lab",
        short_code="ntt01",
        group_tag="NTT-Lab",
        network_cidr="10.50.20.0/24",
        gateway_ip="10.50.20.1",
        sdn_zone="lab-ntt01",
        sdn_vnet="ntt01-vnet",
        sdn_subnet="10.50.20.0/24",
    )


def test_plan_network_reconcile_creates_ordered_sdn_fix_actions(pg_conn):
    lab = _lab(pg_conn)
    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)

    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )

    assert result["status"] == "fixing"
    assert [fix["action_type"] for fix in result["fix_actions"]] == [
        "create_sdn_zone",
        "create_sdn_vnet",
        "create_sdn_subnet",
        "apply_sdn",
    ]
    assert result["findings"][0]["finding_type"] == "sdn_zone_missing"
    assert result["fix_actions"][0]["request"] == {"zone": "lab-ntt01", "type": "simple"}


def test_plan_network_reconcile_marks_ready_when_sdn_state_exists(pg_conn):
    lab = _lab(pg_conn)
    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)

    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        inventory={
            "zones": [{"id": "lab-ntt01", "zone": "lab-ntt01", "type": "simple"}],
            "vnets": [{"id": "ntt01-vnet", "vnet": "ntt01-vnet", "zone": "lab-ntt01"}],
            "subnets_by_vnet": {
                "ntt01-vnet": [{"id": "10.50.20.0/24", "subnet": "10.50.20.0/24", "gateway": "10.50.20.1"}]
            },
        },
    )

    assert result["status"] == "ready"
    assert result["findings"] == []
    assert result["fix_actions"] == []


def test_bridge_mode_missing_target_blocks_without_fix(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Bridge Lab",
        short_code="brg01",
        group_tag="Bridge",
        network_cidr="10.51.0.0/24",
        network_mode="bridge",
    )
    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)

    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        inventory={"bridges": []},
    )

    assert result["status"] == "blocked"
    assert result["findings"][0]["finding_type"] == "bridge_validate_only"
    assert result["fix_actions"] == []
