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


def test_plan_network_reconcile_uses_proxmox_safe_generated_ids_when_sdn_ids_are_omitted(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Generated Network Lab",
        short_code="ntt01",
        group_tag="NTT-Lab",
        network_cidr="10.50.20.0/24",
        gateway_ip="10.50.20.1",
    )
    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)

    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )

    assert result["status"] == "fixing"
    assert result["fix_actions"][0]["request"] == {"zone": "ntt01z", "type": "simple"}
    assert result["fix_actions"][1]["request"] == {"vnet": "ntt01vn", "zone": "ntt01z", "alias": "Generated Network Lab"}
    assert result["fix_actions"][2]["request"] == {
        "vnet": "ntt01vn",
        "subnet": "10.50.20.0/24",
        "gateway": "10.50.20.1",
        "snat": True,
    }


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
                "ntt01-vnet": [
                    {
                        "id": "lab-ntt01-10.50.20.0-24",
                        "subnet": "lab-ntt01-10.50.20.0-24",
                        "cidr": "10.50.20.0/24",
                        "gateway": "10.50.20.1",
                    }
                ]
            },
        },
    )

    assert result["status"] == "ready"
    assert result["findings"] == []
    assert result["fix_actions"] == []


def test_plan_network_reconcile_marks_ready_state_and_clears_stale_work(pg_conn):
    lab = _lab(pg_conn)
    first_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)
    managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=first_run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )
    assert managed_labs_pg.list_open_findings(pg_conn, lab["id"])
    assert len(managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"])) == 4

    ready_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=2)
    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=ready_run["id"],
        inventory={
            "zones": [{"id": "lab-ntt01", "zone": "lab-ntt01", "type": "simple"}],
            "vnets": [{"id": "ntt01-vnet", "vnet": "ntt01-vnet", "zone": "lab-ntt01", "alias": "NTT Lab"}],
            "subnets_by_vnet": {
                "ntt01-vnet": [
                    {
                        "id": "lab-ntt01-10.50.20.0-24",
                        "subnet": "lab-ntt01-10.50.20.0-24",
                        "cidr": "10.50.20.0/24",
                        "gateway": "10.50.20.1",
                        "snat": True,
                    }
                ]
            },
        },
    )

    assert result == {"status": "ready", "findings": [], "fix_actions": []}
    assert managed_labs_pg.list_open_findings(pg_conn, lab["id"]) == []
    assert managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"]) == []
    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])
    objects = {(row["kind"], row["name"]): row for row in payload["boundary_objects"] if row["provider"] == "proxmox"}
    assert objects[("sdn_zone", "lab-ntt01")]["actual_state"] == {"id": "lab-ntt01", "zone": "lab-ntt01", "type": "simple"}
    assert objects[("sdn_vnet", "ntt01-vnet")]["actual_state"] == {
        "id": "ntt01-vnet",
        "vnet": "ntt01-vnet",
        "zone": "lab-ntt01",
        "alias": "NTT Lab",
    }
    assert objects[("sdn_subnet", "10.50.20.0/24")]["actual_state"] == {
        "id": "lab-ntt01-10.50.20.0-24",
        "subnet": "lab-ntt01-10.50.20.0-24",
        "cidr": "10.50.20.0/24",
        "gateway": "10.50.20.1",
        "snat": True,
    }


def test_plan_network_reconcile_replaces_pending_actions_instead_of_duplicating(pg_conn):
    lab = _lab(pg_conn)
    first_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)
    managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=first_run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )
    second_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=2)

    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=second_run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )

    assert [fix["action_type"] for fix in result["fix_actions"]] == [
        "create_sdn_zone",
        "create_sdn_vnet",
        "create_sdn_subnet",
        "apply_sdn",
    ]
    assert [fix["action_type"] for fix in managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"])] == [
        "create_sdn_zone",
        "create_sdn_vnet",
        "create_sdn_subnet",
        "apply_sdn",
    ]
    assert len(managed_labs_pg.list_open_findings(pg_conn, lab["id"])) == 3




def test_reconcile_ready_updates_boundary_state_and_clears_stale_current_rows(pg_conn):
    lab = _lab(pg_conn)
    first_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)
    managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=first_run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )

    second_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=2)
    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=second_run["id"],
        inventory={
            "zones": [{"id": "lab-ntt01", "zone": "lab-ntt01", "type": "simple"}],
            "vnets": [{"id": "ntt01-vnet", "vnet": "ntt01-vnet", "zone": "lab-ntt01"}],
            "subnets_by_vnet": {
                "ntt01-vnet": [
                    {
                        "id": "lab-ntt01-10.50.20.0-24",
                        "subnet": "lab-ntt01-10.50.20.0-24",
                        "cidr": "10.50.20.0/24",
                        "gateway": "10.50.20.1",
                    }
                ]
            },
        },
    )

    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])
    proxmox_boundary = next(row for row in payload["boundaries"] if row["provider"] == "proxmox")
    boundary_objects = {row["kind"]: row for row in payload["boundary_objects"] if row["provider"] == "proxmox"}

    assert result["status"] == "ready"
    assert payload["findings"] == []
    assert payload["fix_actions"] == []
    assert proxmox_boundary["last_reconcile_status"] == "ready"
    assert proxmox_boundary["actual_state"]["zone"]["zone"] == "lab-ntt01"
    assert proxmox_boundary["actual_state"]["vnet"]["vnet"] == "ntt01-vnet"
    assert proxmox_boundary["actual_state"]["subnet"]["cidr"] == "10.50.20.0/24"
    assert boundary_objects["sdn_zone"]["actual_state"]["zone"] == "lab-ntt01"
    assert boundary_objects["sdn_vnet"]["actual_state"]["vnet"] == "ntt01-vnet"
    assert boundary_objects["sdn_subnet"]["actual_state"]["cidr"] == "10.50.20.0/24"


def test_reconcile_rerun_replaces_current_findings_and_pending_fixes_without_duplicates(pg_conn):
    lab = _lab(pg_conn)
    first_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)
    managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=first_run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )

    second_run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=2)
    result = managed_labs_reconciler.plan_network_reconcile(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=second_run["id"],
        inventory={"zones": [], "vnets": [], "subnets_by_vnet": {}},
    )

    finding_count = pg_conn.execute(
        "SELECT count(*) AS total FROM lab_reconcile_findings WHERE lab_id = %s",
        (lab["id"],),
    ).fetchone()["total"]
    fix_count = pg_conn.execute(
        "SELECT count(*) AS total FROM lab_fix_actions WHERE lab_id = %s",
        (lab["id"],),
    ).fetchone()["total"]
    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])

    assert result["status"] == "fixing"
    assert len(managed_labs_pg.list_open_findings(pg_conn, lab["id"])) == 3
    assert len(managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"])) == 4
    assert len(payload["findings"]) == 3
    assert len(payload["fix_actions"]) == 4
    assert finding_count == 6
    assert fix_count == 8

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


# --- Fleet reconcile sweep -------------------------------------------------

_EMPTY_INVENTORY = {"zones": [], "vnets": [], "subnets_by_vnet": {}}


def _make_lab(pg_conn, *, name, short_code, cidr, zone, vnet):
    return managed_labs_pg.create_lab(
        pg_conn,
        name=name,
        short_code=short_code,
        group_tag=f"Sweep-{short_code}",
        network_cidr=cidr,
        gateway_ip=cidr.rsplit(".", 1)[0] + ".1",
        sdn_zone=zone,
        sdn_vnet=vnet,
        sdn_subnet=cidr,
    )


def _ready_inventory(*labs):
    return {
        "zones": [{"zone": lab["sdn_zone"]} for lab in labs],
        "vnets": [{"vnet": lab["sdn_vnet"]} for lab in labs],
        "subnets_by_vnet": {lab["sdn_vnet"]: [{"subnet": lab["sdn_subnet"]}] for lab in labs},
    }


def test_reconcile_all_labs_propose_only_records_fixes_without_applying(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    a = _make_lab(pg_conn, name="Lab A", short_code="swa01", cidr="10.60.10.0/24", zone="lab-swa01", vnet="swa01-vnet")
    b = _make_lab(pg_conn, name="Lab B", short_code="swb01", cidr="10.60.11.0/24", zone="lab-swb01", vnet="swb01-vnet")

    def _fail_if_called(conn, lab_id):
        raise AssertionError(f"apply_fn must not run in propose-only mode (lab {lab_id})")

    result = managed_labs_reconciler.reconcile_all_labs(
        pg_conn,
        inventory=_EMPTY_INVENTORY,
        auto_apply=False,
        apply_fn=_fail_if_called,
    )

    assert result["lab_count"] == 2
    assert result["auto_apply"] is False
    assert result["applied_count"] == 0
    assert result["counts"] == {"fixing": 2}
    # Pending fixes were recorded for both labs even though nothing was applied.
    assert managed_labs_pg.list_pending_fix_actions(pg_conn, a["id"])
    assert managed_labs_pg.list_pending_fix_actions(pg_conn, b["id"])


def test_reconcile_all_labs_auto_apply_runs_executor_for_fixing_labs(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    _make_lab(pg_conn, name="Lab A", short_code="swa01", cidr="10.60.10.0/24", zone="lab-swa01", vnet="swa01-vnet")
    _make_lab(pg_conn, name="Lab B", short_code="swb01", cidr="10.60.11.0/24", zone="lab-swb01", vnet="swb01-vnet")

    calls = []

    def _apply(conn, lab_id):
        calls.append(lab_id)
        return {"fixed": [{"id": "fix-1", "status": "fixed"}], "blocked": [], "failed": []}

    result = managed_labs_reconciler.reconcile_all_labs(
        pg_conn,
        inventory=_EMPTY_INVENTORY,
        auto_apply=True,
        apply_fn=_apply,
    )

    assert len(calls) == 2
    assert result["applied_count"] == 2
    assert result["counts"] == {"applied": 2}
    assert all(summary["applied"]["fixed"] for summary in result["labs"])


def test_reconcile_all_labs_auto_apply_skips_ready_labs(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    ready = _make_lab(pg_conn, name="Ready Lab", short_code="rdy01", cidr="10.60.20.0/24", zone="lab-rdy01", vnet="rdy01-vnet")
    fixing = _make_lab(pg_conn, name="Fixing Lab", short_code="fix01", cidr="10.60.21.0/24", zone="lab-fix01", vnet="fix01-vnet")

    calls = []

    def _apply(conn, lab_id):
        calls.append(lab_id)
        return {"fixed": [{"id": "fix-1", "status": "fixed"}], "blocked": [], "failed": []}

    # Inventory already satisfies the ready lab; the fixing lab's SDN is absent.
    result = managed_labs_reconciler.reconcile_all_labs(
        pg_conn,
        inventory=_ready_inventory(ready),
        auto_apply=True,
        apply_fn=_apply,
    )

    assert calls == [fixing["id"]]
    by_id = {summary["lab_id"]: summary for summary in result["labs"]}
    assert by_id[ready["id"]]["status"] == "ready"
    assert by_id[fixing["id"]]["status"] == "applied"


def test_reconcile_all_labs_continues_past_apply_failure(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    boom = _make_lab(pg_conn, name="Boom Lab", short_code="bom01", cidr="10.60.30.0/24", zone="lab-bom01", vnet="bom01-vnet")
    ok = _make_lab(pg_conn, name="OK Lab", short_code="okl01", cidr="10.60.31.0/24", zone="lab-okl01", vnet="okl01-vnet")

    def _apply(conn, lab_id):
        if lab_id == boom["id"]:
            raise RuntimeError("proxmox exploded")
        return {"fixed": [{"id": "fix-1", "status": "fixed"}], "blocked": [], "failed": []}

    result = managed_labs_reconciler.reconcile_all_labs(
        pg_conn,
        inventory=_EMPTY_INVENTORY,
        auto_apply=True,
        apply_fn=_apply,
    )

    assert result["lab_count"] == 2
    by_id = {summary["lab_id"]: summary for summary in result["labs"]}
    assert by_id[boom["id"]]["status"] == "failed"
    assert "proxmox exploded" in by_id[boom["id"]]["detail"]
    assert by_id[ok["id"]]["status"] == "applied"


def test_reconcile_all_labs_scopes_to_lab_ids(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    a = _make_lab(pg_conn, name="Lab A", short_code="swa01", cidr="10.60.10.0/24", zone="lab-swa01", vnet="swa01-vnet")
    _make_lab(pg_conn, name="Lab B", short_code="swb01", cidr="10.60.11.0/24", zone="lab-swb01", vnet="swb01-vnet")

    result = managed_labs_reconciler.reconcile_all_labs(
        pg_conn,
        inventory=_EMPTY_INVENTORY,
        auto_apply=False,
        lab_ids=[a["id"]],
    )

    assert result["lab_count"] == 1
    assert [summary["lab_id"] for summary in result["labs"]] == [a["id"]]
