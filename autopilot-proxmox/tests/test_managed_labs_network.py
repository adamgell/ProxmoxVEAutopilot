import pytest

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
        sdn_zone="lab-net01",
        sdn_vnet="net01-vnet",
        sdn_subnet="10.91.0.0/24",
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


def _snapshot_count(pg_conn):
    return pg_conn.execute("SELECT count(*) AS total FROM lab_provider_snapshots").fetchone()["total"]


def test_execute_create_zone_snapshots_inventory_before_mutation(pg_conn):
    lab, fix = _seed_fix(pg_conn, "create_sdn_zone", {"zone": "lab-net01", "type": "simple"})
    calls = []
    state = {"zone_present": False}

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones" and method == "GET":
            return [{"zone": "lab-net01", "type": "simple"}] if state["zone_present"] else []
        if path == "/cluster/sdn/vnets":
            return []
        if path == "/cluster/sdn/zones" and method == "POST":
            state["zone_present"] = True
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
    get_indexes = [
        index
        for index, call in enumerate(calls)
        if call[0] == "GET" and call[1] in {"/cluster/sdn/zones", "/cluster/sdn/vnets"}
    ]
    post_index = calls.index(("POST", "/cluster/sdn/zones", {"zone": "lab-net01", "type": "simple"}))
    assert get_indexes
    assert any(index < post_index for index in get_indexes)
    assert any(index > post_index for index in get_indexes)
    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])
    assert payload["fix_actions"] == []
    assert payload["events"][0]["event_type"] == "fix_action_updated"
    zone_object = next(
        row for row in payload["boundary_objects"] if row["provider"] == "proxmox" and row["kind"] == "sdn_zone"
    )
    assert zone_object["actual_state"]["zone"] == "lab-net01"


def test_execute_create_zone_requires_post_mutation_verification(pg_conn):
    _lab, fix = _seed_fix(pg_conn, "create_sdn_zone", {"zone": "lab-net01", "type": "simple"})
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

    assert result["status"] == "failed"
    assert result["result"]["ok"] is False
    assert result["result"]["verification"]["observed"] is False
    post_index = calls.index(("POST", "/cluster/sdn/zones", {"zone": "lab-net01", "type": "simple"}))
    assert any(index > post_index and call[:2] == ("GET", "/cluster/sdn/zones") for index, call in enumerate(calls))


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
    put_index = next(index for index, call in enumerate(calls) if call[0] == "PUT")
    assert any(
        call[0] == "GET" and call[1] in {"/cluster/sdn/zones", "/cluster/sdn/vnets"} and index > put_index
        for index, call in enumerate(calls)
    )


def test_execute_create_subnet_adds_required_pve_subnet_type(pg_conn):
    _lab, fix = _seed_fix(
        pg_conn,
        "create_sdn_subnet",
        {"vnet": "net01_vnet", "subnet": "10.91.0.0/24", "gateway": "10.91.0.1", "snat": True},
    )
    calls = []
    state = {"subnet_present": False}

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones" and method == "GET":
            return [{"zone": "lab_net01", "type": "simple"}]
        if path == "/cluster/sdn/vnets" and method == "GET":
            return [{"vnet": "net01_vnet", "zone": "lab_net01"}]
        if path == "/cluster/sdn/vnets/net01_vnet/subnets" and method == "GET":
            return [{"subnet": "10.91.0.0/24", "gateway": "10.91.0.1", "snat": True}] if state["subnet_present"] else []
        if path == "/cluster/sdn/vnets/net01_vnet/subnets" and method == "POST":
            state["subnet_present"] = True
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
    assert (
        "POST",
        "/cluster/sdn/vnets/net01_vnet/subnets",
        {"subnet": "10.91.0.0/24", "gateway": "10.91.0.1", "snat": True, "type": "subnet"},
    ) in calls


def test_execute_apply_sdn_accepts_raw_lock_token_response(pg_conn):
    _lab, fix = _seed_fix(pg_conn, "apply_sdn", {"allow_pending": True})
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/lock":
            return "token-1"
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
    assert any(call[0] == "PUT" and call[2]["lock-token"] == "token-1" for call in calls)
    assert any(call[0] == "DELETE" and call[1].endswith("token-1") for call in calls)


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
    state = {"zone_present": False, "vnet_present": False}

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones" and method == "GET":
            return [{"zone": "lab-net01", "type": "simple"}] if state["zone_present"] else []
        if path == "/cluster/sdn/vnets" and method == "GET":
            return [{"vnet": "net01-vnet", "zone": "lab-net01"}] if state["vnet_present"] else []
        if path == "/cluster/sdn/zones" and method == "POST":
            state["zone_present"] = True
            return {"ok": True}
        if path == "/cluster/sdn/vnets" and method == "POST":
            state["vnet_present"] = True
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


def test_execute_pending_network_fixes_only_processes_supported_proxmox_rows(pg_conn):
    lab, owned = _seed_fix(pg_conn, "create_sdn_zone", {"zone": "lab-net01", "type": "simple"})
    foreign_provider = managed_labs_pg.create_fix_action(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=None,
        provider="network",
        action_type="create_sdn_zone",
        priority=11,
        detail="Foreign provider row",
        request={"zone": "foreign-zone", "type": "simple"},
    )
    unsupported = managed_labs_pg.create_fix_action(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=None,
        provider="proxmox",
        action_type="future_network_action",
        priority=12,
        detail="Unsupported action row",
        request={"zone": "future-zone"},
    )
    calls = []
    state = {"zone_present": False}

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones" and method == "GET":
            return [{"zone": "lab-net01", "type": "simple"}] if state["zone_present"] else []
        if path == "/cluster/sdn/vnets" and method == "GET":
            return []
        if path == "/cluster/sdn/zones" and method == "POST":
            state["zone_present"] = True
            return {"ok": True}
        return []

    result = managed_labs_network.execute_pending_network_fixes(
        pg_conn,
        lab_id=lab["id"],
        pve_api=fake_api,
        pve_put=lambda path, data=None: {},
        pve_delete=lambda path: {},
    )

    assert [item["id"] for item in result["fixed"]] == [owned["id"]]
    assert result["blocked"] == []
    assert result["failed"] == []
    assert calls.count(("POST", "/cluster/sdn/zones", {"zone": "lab-net01", "type": "simple"})) == 1

    still_pending = {
        row["id"]: row["status"]
        for row in managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"])
    }
    assert still_pending == {
        foreign_provider["id"]: "pending",
        unsupported["id"]: "pending",
    }


@pytest.mark.parametrize(
    ("provider", "action_type", "status", "message"),
    [
        ("network", "create_sdn_zone", "pending", "unsupported provider"),
        ("proxmox", "future_network_action", "pending", "unsupported action type"),
        ("proxmox", "create_sdn_zone", "running", "unsupported status"),
    ],
)
def test_execute_fix_action_rejects_rows_outside_executor_scope(
    pg_conn, provider, action_type, status, message
):
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
        provider=provider,
        action_type=action_type,
        priority=10,
        detail=f"Run {action_type}",
        request={"zone": "lab-net01", "type": "simple"},
    )
    if status != "pending":
        pg_conn.execute(
            "UPDATE lab_fix_actions SET status = %s, completed_at = NULL, snapshot_id = NULL, result_json = '{}'::jsonb WHERE id = %s",
            (status, fix["id"]),
        )
        pg_conn.commit()

    calls = []
    before = _snapshot_count(pg_conn)

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        return []

    with pytest.raises(ValueError, match=message):
        managed_labs_network.execute_fix_action(
            pg_conn,
            fix_action_id=fix["id"],
            pve_api=fake_api,
            pve_put=lambda path, data=None: {},
            pve_delete=lambda path: {},
        )

    assert calls == []
    assert _snapshot_count(pg_conn) == before
    row = managed_labs_pg.get_fix_action(pg_conn, fix["id"])
    assert row["status"] == status
    assert row["snapshot_id"] is None
    assert row["result"] == fix["result"]


@pytest.mark.parametrize(
    ("action_type", "fix_request", "inventory", "expected_post"),
    [
        (
            "create_sdn_zone",
            {"zone": "lab-net01", "type": "simple"},
            {"zones": [{"zone": "lab-net01"}], "vnets": [], "subnets_by_vnet": {}},
            ("POST", "/cluster/sdn/zones"),
        ),
        (
            "create_sdn_vnet",
            {"vnet": "net01-vnet", "zone": "lab-net01"},
            {"zones": [], "vnets": [{"vnet": "net01-vnet"}], "subnets_by_vnet": {}},
            ("POST", "/cluster/sdn/vnets"),
        ),
        (
            "create_sdn_subnet",
            {"vnet": "net01-vnet", "subnet": "10.91.0.0/24", "gateway": "10.91.0.1", "snat": True},
            {
                "zones": [],
                "vnets": [{"vnet": "net01-vnet"}],
                "subnets_by_vnet": {"net01-vnet": [{"subnet": "10.91.0.0/24"}]},
            },
            ("POST", "/cluster/sdn/vnets/net01-vnet/subnets"),
        ),
    ],
)
def test_execute_fix_action_short_circuits_existing_create_targets(
    pg_conn, action_type, fix_request, inventory, expected_post
):
    _lab, fix = _seed_fix(pg_conn, action_type, fix_request)
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if method == "GET":
            if path == "/cluster/sdn/zones":
                return inventory.get("zones", [])
            if path == "/cluster/sdn/vnets":
                return inventory.get("vnets", [])
            if path == "/cluster/sdn/vnets/net01-vnet/subnets":
                return inventory.get("subnets_by_vnet", {}).get("net01-vnet", [])
        raise AssertionError(f"unexpected mutation call: {(method, path, data)}")

    result = managed_labs_network.execute_fix_action(
        pg_conn,
        fix_action_id=fix["id"],
        pve_api=fake_api,
        pve_put=lambda path, data=None: (_ for _ in ()).throw(AssertionError("unexpected PUT")),
        pve_delete=lambda path: (_ for _ in ()).throw(AssertionError("unexpected DELETE")),
    )

    assert result["status"] == "fixed"
    assert result["snapshot_id"]
    assert result["result"]["ok"] is True
    assert result["result"]["already_present"] is True
    assert expected_post not in [(call[0], call[1]) for call in calls]
