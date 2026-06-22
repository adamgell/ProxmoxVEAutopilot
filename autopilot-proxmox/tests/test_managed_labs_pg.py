import pytest
from psycopg.errors import UniqueViolation

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
    assert any(row["provider"] == "proxmox" for row in payload["boundaries"])
    assert {
        row["kind"]
        for row in payload["boundary_objects"]
        if row["provider"] == "proxmox"
    } >= {"sdn_zone", "sdn_vnet", "sdn_subnet"}


def test_create_lab_seeds_default_boundary_model(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)

    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Managed Lab",
        short_code="mlb01",
        group_tag="Managed-Lab",
        network_cidr="10.52.20.0/24",
        gateway_ip="10.52.20.1",
        sdn_zone="lab-mlb01",
        sdn_vnet="mlb01-vnet",
        sdn_subnet="10.52.20.0/24",
    )

    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])

    boundary_keys = {(row["provider"], row["kind"]) for row in payload["boundaries"]}
    assert ("proxmox", "network") in boundary_keys
    assert ("ad", "directory") in boundary_keys
    assert ("entra", "identity") in boundary_keys
    assert ("intune", "endpoint_management") in boundary_keys
    assert ("deployment", "naming") in boundary_keys
    proxmox_objects = {
        (row["kind"], row["name"]): row
        for row in payload["boundary_objects"]
        if row["provider"] == "proxmox"
    }
    assert proxmox_objects[("sdn_zone", "lab-mlb01")]["desired_state"] == {"zone": "lab-mlb01", "type": "simple"}
    assert proxmox_objects[("sdn_vnet", "mlb01-vnet")]["desired_state"] == {
        "vnet": "mlb01-vnet",
        "zone": "lab-mlb01",
        "alias": "Managed Lab",
    }
    assert proxmox_objects[("sdn_subnet", "10.52.20.0/24")]["desired_state"] == {
        "vnet": "mlb01-vnet",
        "subnet": "10.52.20.0/24",
        "gateway": "10.52.20.1",
        "snat": True,
    }
    assert all(row["actual_state"] == {} for row in payload["boundary_objects"])


def test_create_lab_generates_proxmox_safe_sdn_defaults_when_omitted(pg_conn):
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

    assert lab["sdn_zone"] == "lab_ntt01"
    assert lab["sdn_vnet"] == "ntt01_vnet"
    assert lab["desired_state"]["network"]["sdn_zone"] == "lab_ntt01"
    assert lab["desired_state"]["network"]["sdn_vnet"] == "ntt01_vnet"
    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])
    proxmox_objects = {
        (row["kind"], row["name"]): row
        for row in payload["boundary_objects"]
        if row["provider"] == "proxmox"
    }
    assert proxmox_objects[("sdn_zone", "lab_ntt01")]["desired_state"] == {"zone": "lab_ntt01", "type": "simple"}
    assert proxmox_objects[("sdn_vnet", "ntt01_vnet")]["desired_state"] == {
        "vnet": "ntt01_vnet",
        "zone": "lab_ntt01",
        "alias": "Generated Network Lab",
    }


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
    boundary = managed_labs_pg.create_boundary(
        pg_conn,
        lab_id=lab["id"],
        provider="proxmox",
        kind="network",
        name="Pilot SDN",
        ownership="managed",
        source="created",
        desired_state={"zone": "lab-plt01", "vnet": "plt01-vnet"},
    )
    boundary_object = managed_labs_pg.create_boundary_object(
        pg_conn,
        lab_id=lab["id"],
        boundary_id=boundary["id"],
        provider="proxmox",
        kind="sdn_zone",
        name="lab-plt01",
        ownership="managed",
        source="created",
        provider_ids={"zone": "lab-plt01"},
        desired_state={"type": "simple", "zone": "lab-plt01"},
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
    assert boundary["id"] in [item["id"] for item in payload["boundaries"]]
    assert boundary_object["id"] in [item["id"] for item in payload["boundary_objects"]]
    assert payload["findings"] == []
    assert payload["fix_actions"] == []
    assert payload["reconcile_runs"] == []


def test_boundary_object_provider_identity_must_be_unique_across_labs(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    first_lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Alpha Lab",
        short_code="alp01",
        group_tag="ALPHA-Lab",
        network_cidr="10.80.1.0/24",
    )
    second_lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Bravo Lab",
        short_code="brv01",
        group_tag="BRAVO-Lab",
        network_cidr="10.81.1.0/24",
    )
    first_boundary = managed_labs_pg.create_boundary(
        pg_conn,
        lab_id=first_lab["id"],
        provider="proxmox",
        kind="network",
        name="Alpha SDN",
        ownership="managed",
        source="created",
        desired_state={"zone": "shared-zone"},
    )
    second_boundary = managed_labs_pg.create_boundary(
        pg_conn,
        lab_id=second_lab["id"],
        provider="proxmox",
        kind="network",
        name="Bravo SDN",
        ownership="managed",
        source="created",
        desired_state={"zone": "shared-zone"},
    )

    managed_labs_pg.create_boundary_object(
        pg_conn,
        lab_id=first_lab["id"],
        boundary_id=first_boundary["id"],
        provider="proxmox",
        kind="sdn_zone",
        name="shared-zone",
        ownership="managed",
        source="created",
        provider_ids={"zone": "shared-zone"},
        desired_state={"type": "simple", "zone": "shared-zone"},
    )

    with pytest.raises(UniqueViolation):
        managed_labs_pg.create_boundary_object(
            pg_conn,
            lab_id=second_lab["id"],
            boundary_id=second_boundary["id"],
            provider="proxmox",
            kind="sdn_zone",
            name="shared-zone-copy",
            ownership="managed",
            source="created",
            provider_ids={"zone": "shared-zone"},
            desired_state={"type": "simple", "zone": "shared-zone"},
        )

def test_reservations_enforce_unique_values_and_generate_safe_names(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="NTT Lab",
        short_code="ntt01",
        group_tag="NTT-Lab",
        network_cidr="10.50.20.0/24",
    )

    names = managed_labs_pg.reserve_default_names(
        pg_conn,
        lab_id=lab["id"],
        short_code="ntt01",
        role="wks",
        count=2,
    )

    assert [row["value"] for row in names] == ["ntt01-wks-001", "ntt01-wks-002"]
    assert all(len(row["value"]) <= 15 for row in names)

    duplicate = managed_labs_pg.reserve_value(
        pg_conn,
        lab_id=lab["id"],
        reservation_type="hostname",
        value="ntt01-wks-001",
    )
    assert duplicate["status"] == "active"
    assert duplicate["value"] == "ntt01-wks-001"


def test_reserve_value_rejects_cross_lab_hostname_collision(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    first_lab = managed_labs_pg.create_lab(
        pg_conn,
        name="First Lab",
        short_code="fst01",
        group_tag="FIRST-Lab",
        network_cidr="10.50.30.0/24",
    )
    second_lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Second Lab",
        short_code="snd01",
        group_tag="SECOND-Lab",
        network_cidr="10.50.31.0/24",
    )

    original = managed_labs_pg.reserve_value(
        pg_conn,
        lab_id=first_lab["id"],
        reservation_type="hostname",
        value="shared-host-01",
    )
    duplicate = managed_labs_pg.reserve_value(
        pg_conn,
        lab_id=first_lab["id"],
        reservation_type="hostname",
        value="shared-host-01",
    )

    assert duplicate["id"] == original["id"]

    with pytest.raises(ValueError, match="already reserved by another lab"):
        managed_labs_pg.reserve_value(
            pg_conn,
            lab_id=second_lab["id"],
            reservation_type="hostname",
            value="shared-host-01",
        )


def test_cidr_overlap_detection_finds_existing_lab_reservation(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Lab One",
        short_code="lab01",
        group_tag="LabOne",
        network_cidr="10.80.0.0/24",
    )
    managed_labs_pg.reserve_value(
        pg_conn,
        lab_id=lab["id"],
        reservation_type="cidr",
        value="10.80.0.0/24",
    )

    overlaps = managed_labs_pg.find_overlapping_cidr_reservations(pg_conn, "10.80.0.128/25")

    assert overlaps[0]["lab_id"] == lab["id"]
    assert overlaps[0]["value"] == "10.80.0.0/24"


def test_reconcile_findings_fix_actions_and_snapshots_are_queryable(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Fix Lab",
        short_code="fix01",
        group_tag="Fix",
        network_cidr="10.90.0.0/24",
    )
    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)
    finding = managed_labs_pg.record_finding(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        provider="proxmox",
        finding_type="sdn_zone_missing",
        severity="fixable",
        detail="SDN zone lab-fix01 is missing.",
        object_ref={"zone": "lab-fix01"},
        desired_state={"zone": "lab-fix01", "type": "simple"},
    )
    fix = managed_labs_pg.create_fix_action(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        provider="proxmox",
        action_type="create_sdn_zone",
        priority=10,
        detail="Create SDN zone lab-fix01.",
        request={"zone": "lab-fix01", "type": "simple"},
    )
    snapshot = managed_labs_pg.record_provider_snapshot(
        pg_conn,
        lab_id=lab["id"],
        provider="proxmox",
        snapshot_type="pre_fix",
        object_ref={"action_type": "create_sdn_zone"},
        snapshot={"zones": []},
    )
    pending = managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"])
    loaded_fix = managed_labs_pg.get_fix_action(pg_conn, fix["id"])
    updated = managed_labs_pg.update_fix_action(
        pg_conn,
        fix["id"],
        status="fixed",
        result={"ok": True},
        snapshot_id=snapshot["id"],
    )
    finished = managed_labs_pg.finish_reconcile_run(
        pg_conn,
        run_id=run["id"],
        status="ready",
        summary="Reconciliation completed.",
    )
    payload = managed_labs_pg.page_payload(pg_conn, selected_lab_id=lab["id"])

    assert finding["status"] == "open"
    assert managed_labs_pg.list_open_findings(pg_conn, lab["id"])[0]["finding_type"] == "sdn_zone_missing"
    assert [row["id"] for row in pending] == [fix["id"]]
    assert loaded_fix is not None
    assert loaded_fix["id"] == fix["id"]
    assert updated["status"] == "fixed"
    assert updated["snapshot_id"] == snapshot["id"]
    assert finished["status"] == "ready"
    assert finished["finished_at"] is not None
    assert managed_labs_pg.list_pending_fix_actions(pg_conn, lab["id"]) == []
    assert payload["selected_lab"]["last_reconcile_run_id"] == run["id"]
    assert [row["id"] for row in payload["reconcile_runs"]] == [run["id"]]
    assert [row["id"] for row in payload["findings"]] == [finding["id"]]
    assert payload["fix_actions"] == []


@pytest.mark.parametrize(
    ("run_status", "attempt", "expected_lab_status"),
    [
        ("ready", 3, "ready"),
        ("blocked", 3, "blocked"),
        ("failed", 1, "validating"),
        ("failed", 5, "blocked"),
    ],
)
def test_finish_reconcile_run_updates_lab_current_state(pg_conn, run_status, attempt, expected_lab_status):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Current State Lab",
        short_code="cur01",
        group_tag="CURRENT-Lab",
        network_cidr="10.90.10.0/24",
    )

    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=attempt)
    finished = managed_labs_pg.finish_reconcile_run(
        pg_conn,
        run_id=run["id"],
        status=run_status,
        summary=f"Run ended with {run_status}.",
    )
    updated_lab = managed_labs_pg.get_lab(pg_conn, lab["id"])

    assert finished["status"] == run_status
    assert updated_lab is not None
    assert updated_lab["status"] == expected_lab_status
    assert updated_lab["retry_count"] == attempt
    assert updated_lab["last_reconcile_run_id"] == run["id"]


def test_update_fix_action_requires_snapshot_for_terminal_status(pg_conn):
    managed_labs_pg.reset_for_tests(pg_conn)
    managed_labs_pg.init(pg_conn)
    lab = managed_labs_pg.create_lab(
        pg_conn,
        name="Snapshot Lab",
        short_code="snp01",
        group_tag="SNAPSHOT-Lab",
        network_cidr="10.90.20.0/24",
    )
    run = managed_labs_pg.start_reconcile_run(pg_conn, lab_id=lab["id"], attempt=1)
    fix = managed_labs_pg.create_fix_action(
        pg_conn,
        lab_id=lab["id"],
        reconcile_run_id=run["id"],
        provider="proxmox",
        action_type="create_sdn_zone",
        priority=5,
        detail="Create SDN zone after snapshot.",
        request={"zone": "lab-snp01"},
    )

    with pytest.raises(ValueError, match="snapshot_id is required for terminal status"):
        managed_labs_pg.update_fix_action(
            pg_conn,
            fix["id"],
            status="fixed",
            result={"ok": True},
        )
