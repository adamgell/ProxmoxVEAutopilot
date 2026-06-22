from web import managed_labs_network, managed_labs_pg


def _seed_fix(pg_conn, action_type, request):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Network Lab",
        short_code="net01",
        group_tag="Network",
        network_cidr="10.91.0.0/24",
    )
    fix = managed_labs_pg.create_fix_action(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=None,
        provider="proxmox",
        action_type=action_type,
        priority=10,
        detail=f"Run {action_type}",
        request=request,
    )
    return lab, fix


def test_execute_create_zone_snapshots_inventory_before_mutation(pg_conn):
    lab, fix = _seed_fix(pg_conn, "create_sdn_zone", {"zone": "lab-net01", "type": "simple"})
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones" and method == "GET":
            return []
        if path == "/cluster/sdn/vnets":
            return []
        if path == "/cluster/sdn/zones" and method == "POST":
            return {"ok": True}
        return []

    result = managed_labs_network.execute_fix_action(
        pg_conn,
        fix_action_id=fix["id"],
        pve_api=fake_api,
        pve_put=lambda path, data=None: {},
        pve_delete=lambda path: {},
    )

    assert result["status"] == "fixed"
    assert ("POST", "/cluster/sdn/zones", {"zone": "lab-net01", "type": "simple"}) in calls
    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])
    assert payload["fix_actions"][0]["snapshot_id"]
    assert payload["events"][0]["event_type"] == "fix_action_updated"


def test_execute_apply_sdn_releases_lock_after_apply(pg_conn):
    _lab, fix = _seed_fix(pg_conn, "apply_sdn", {"allow_pending": True})
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/lock":
            return {"lock": "token-1"}
        if path == "/cluster/sdn/zones":
            return []
        if path == "/cluster/sdn/vnets":
            return []
        return []

    def fake_put(path, data=None):
        calls.append(("PUT", path, data))
        return {"applied": True}

    def fake_delete(path):
        calls.append(("DELETE", path, None))
        return {"released": True}

    result = managed_labs_network.execute_fix_action(
        pg_conn,
        fix_action_id=fix["id"],
        pve_api=fake_api,
        pve_put=fake_put,
        pve_delete=fake_delete,
    )

    assert result["status"] == "fixed"
    assert any(call[0] == "PUT" and "/cluster/sdn" in call[1] for call in calls)
    assert any(call[0] == "DELETE" and "/cluster/sdn/lock" in call[1] for call in calls)


def test_execute_pending_network_fixes_processes_pending_actions_in_priority_order(pg_conn):
    lab, first = _seed_fix(pg_conn, "create_sdn_zone", {"zone": "lab-net01", "type": "simple"})
    second = managed_labs_pg.create_fix_action(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=None,
        provider="proxmox",
        action_type="create_sdn_vnet",
        priority=20,
        detail="Run create_sdn_vnet",
        request={"vnet": "net01-vnet", "zone": "lab-net01"},
    )

    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones" and method == "GET":
            return []
        if path == "/cluster/sdn/vnets" and method == "GET":
            return []
        if path == "/cluster/sdn/zones" and method == "POST":
            return {"ok": True}
        if path == "/cluster/sdn/vnets" and method == "POST":
            return {"ok": True}
        return []

    result = managed_labs_network.execute_pending_network_fixes(
        pg_conn,
        lab_id=lab["id"],
        pve_api=fake_api,
        pve_put=lambda path, data=None: {},
        pve_delete=lambda path: {},
    )

    assert [item["id"] for item in result["fixed"]] == [first["id"], second["id"]]
    assert result["blocked"] == []
    assert result["failed"] == []
