from web import managed_labs_pg


def test_create_lab_persists_current_state_and_initial_event(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)

    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="NTTENANT01 Lab",
        short_code="ntt01",
        group_tag="NTTENANT01-Desktop",
        network_cidr="10.50.20.0/24",
        gateway_ip="10.50.20.1",
        sdn_zone="lab-ntt01",
        sdn_vnet="ntt01-vnet",
        sdn_subnet="10.50.20.0/24",
    )

    assert lab["name"] == "NTTENANT01 Lab"
    assert lab["slug"] == "nttenant01-lab"
    assert lab["short_code"] == "ntt01"
    assert lab["group_tag"] == "NTTENANT01-Desktop"
    assert lab["network_cidr"] == "10.50.20.0/24"
    assert lab["status"] == "draft"
    assert lab["network_mode"] == "sdn"
    assert lab["sdn_zone"] == "lab-ntt01"

    loaded = managed_labs_pg.get_lab(pg_conn, lab["id"])
    assert loaded == lab

    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])
    assert payload["selected_lab"]["id"] == lab["id"]
    assert payload["labs"][0]["name"] == "NTTENANT01 Lab"
    assert payload["events"][0]["event_type"] == "lab_created"


def test_boundary_objects_track_provider_ids_desired_and_actual_state(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="ACME Lab",
        short_code="acm01",
        group_tag="ACME-Lab",
        network_cidr="10.60.10.0/24",
    )
    boundary = managed_labs_pg.create_boundary(
        pg_conn,
        lab_id=lab["id"],
        provider="proxmox",
        kind="network",
        name="ACME SDN",
        ownership="managed",
        source="created",
        desired_state={"zone": "lab-acm01", "vnet": "acm01-vnet"},
    )
    obj = managed_labs_pg.create_boundary_object(
        pg_conn,
        lab_id=lab["id"],
        boundary_id=boundary["id"],
        provider="proxmox",
        kind="sdn_zone",
        name="lab-acm01",
        ownership="managed",
        source="created",
        provider_ids={"zone": "lab-acm01"},
        desired_state={"type": "simple", "zone": "lab-acm01"},
    )

    assert obj["provider"] == "proxmox"
    assert obj["kind"] == "sdn_zone"
    assert obj["ownership"] == "managed"
    assert obj["source"] == "created"
    assert obj["provider_ids"] == {"zone": "lab-acm01"}
    assert obj["desired_state"]["type"] == "simple"


def test_page_payload_returns_current_state_and_append_only_events(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Pilot Lab",
        short_code="plt01",
        group_tag="Pilot",
        network_cidr="10.70.1.0/24",
    )
    managed_labs_pg.record_event(
        pg_conn,
        lab_id=lab["id"],
        event_type="manual_note",
        actor="test",
        detail="operator marked this lab as important",
        payload={"note": "important"},
    )

    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])

    assert payload["selected_lab"]["short_code"] == "plt01"
    assert [event["event_type"] for event in payload["events"]] == ["manual_note", "lab_created"]
    assert payload["findings"] == []
    assert payload["fix_actions"] == []
    assert payload["reconcile_runs"] == []
