import uuid

import pytest

from web import lab_bubbles_pg


def test_bubble_schema_create_get_list_patch(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)

    bubble = lab_bubbles_pg.create_bubble(
        pg_conn,
        name="ACME Lab",
        domain_name="lab.acme.test",
        netbios_name="ACME",
        cidr="10.42.12.0/24",
        gateway_ip="10.42.12.1",
        planned_bridge="vmbr-lab12",
        dhcp_scope="10.42.12.0",
        dhcp_pool_start="10.42.12.100",
        dhcp_pool_end="10.42.12.199",
    )

    assert bubble["slug"] == "acme-lab"
    assert bubble["lifecycle_state"] == "planned"
    assert bubble["domain_name"] == "lab.acme.test"
    assert bubble["dhcp_owner_asset_id"] is None

    listed = lab_bubbles_pg.list_bubbles(pg_conn)
    assert [row["id"] for row in listed] == [bubble["id"]]

    patched = lab_bubbles_pg.update_bubble(
        pg_conn,
        bubble["id"],
        lifecycle_state="active",
        dns_ready=True,
    )
    assert patched["lifecycle_state"] == "active"
    assert patched["dns_ready"] is True
    assert lab_bubbles_pg.get_bubble(pg_conn, bubble["id"])["dns_ready"] is True


def test_assets_services_audit_and_readiness(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")

    dc_asset = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
        agent_id="dc01-agent",
        membership_state="active",
    )
    service = lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        scope="bubble_local",
        provider_asset_id=dc_asset["id"],
        readiness_state="ready",
        evidence_summary={"scope": "10.42.12.0", "leases": 3},
    )
    updated_asset = lab_bubbles_pg.update_asset(
        pg_conn,
        dc_asset["id"],
        evidence_state="confirmed",
        notes="DC agent evidence confirmed",
    )
    updated_service = lab_bubbles_pg.update_service(
        pg_conn,
        service["id"],
        readiness_state="degraded",
        evidence_summary={
            "scope": "10.42.12.0",
            "leases": 4,
            "warning": "short lease window",
        },
    )
    patched = lab_bubbles_pg.update_readiness_from_dc_evidence(
        pg_conn,
        bubble["id"],
        dc_asset_id=dc_asset["id"],
        evidence={
            "ad_ds_ready": True,
            "dns_ready": True,
            "dhcp_ready": True,
            "dhcp_scope": "10.42.12.0",
            "dhcp_pool_start": "10.42.12.100",
            "dhcp_pool_end": "10.42.12.199",
        },
    )

    assert service["service_kind"] == "dhcp"
    assert updated_asset["evidence_state"] == "confirmed"
    assert updated_service["readiness_state"] == "degraded"
    assert updated_service["evidence_summary"]["leases"] == 4
    assert patched["dc_ready"] is True
    assert patched["dns_ready"] is True
    assert patched["dhcp_ready"] is True
    assert patched["workload_ready"] is True
    assert patched["dhcp_pool_start"] == "10.42.12.100"
    assert patched["dhcp_pool_end"] == "10.42.12.199"

    moved = lab_bubbles_pg.move_asset(
        pg_conn,
        dc_asset["id"],
        bubble["id"],
        reason="repair membership",
        actor="operator",
    )
    assert moved["id"] == dc_asset["id"]
    events = lab_bubbles_pg.list_audit_events(pg_conn, bubble["id"])
    assert events[-1]["action"] == "asset_moved"


def test_readiness_evidence_rolls_back_when_audit_asset_is_invalid(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")

    with pytest.raises(Exception):
        lab_bubbles_pg.update_readiness_from_dc_evidence(
            pg_conn,
            bubble["id"],
            dc_asset_id=str(uuid.uuid4()),
            evidence={
                "ad_ds_ready": True,
                "dns_ready": True,
                "dhcp_ready": True,
                "dhcp_scope": "10.42.12.0",
            },
        )

    pg_conn.rollback()
    refreshed = lab_bubbles_pg.get_bubble(pg_conn, bubble["id"])
    assert refreshed["dc_ready"] is False
    assert refreshed["dns_ready"] is False
    assert refreshed["dhcp_ready"] is False
    assert refreshed["workload_ready"] is False


def test_readiness_evidence_requires_active_dc_asset_in_bubble(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    source = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    target = lab_bubbles_pg.create_bubble(pg_conn, name="Contoso Lab")
    target_dc = lab_bubbles_pg.add_asset(
        pg_conn,
        target["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=230,
    )
    source_file_server = lab_bubbles_pg.add_asset(
        pg_conn,
        source["id"],
        asset_type="vm",
        asset_role="file_server",
        vmid=131,
    )

    for asset_id in (target_dc["id"], source_file_server["id"]):
        with pytest.raises(ValueError, match="asset not found in bubble"):
            lab_bubbles_pg.update_readiness_from_dc_evidence(
                pg_conn,
                source["id"],
                dc_asset_id=asset_id,
                evidence={
                    "ad_ds_ready": True,
                    "dns_ready": True,
                    "dhcp_ready": True,
                },
            )

    refreshed = lab_bubbles_pg.get_bubble(pg_conn, source["id"])
    assert refreshed["dc_ready"] is False
    assert refreshed["dns_ready"] is False
    assert refreshed["dhcp_ready"] is False


def test_move_asset_blocks_cross_bubble_service_provider(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    source = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    target = lab_bubbles_pg.create_bubble(pg_conn, name="Contoso Lab")
    dc_asset = lab_bubbles_pg.add_asset(
        pg_conn,
        source["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
    )
    lab_bubbles_pg.add_service(
        pg_conn,
        source["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=dc_asset["id"],
    )

    with pytest.raises(ValueError, match="asset provides services"):
        lab_bubbles_pg.move_asset(
            pg_conn,
            dc_asset["id"],
            target["id"],
            reason="wrong bubble",
        )


def test_service_provider_must_belong_to_service_bubble(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    source = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    target = lab_bubbles_pg.create_bubble(pg_conn, name="Contoso Lab")
    source_dc = lab_bubbles_pg.add_asset(
        pg_conn,
        source["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
    )
    target_dc = lab_bubbles_pg.add_asset(
        pg_conn,
        target["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=230,
    )

    with pytest.raises(ValueError, match="asset not found in bubble"):
        lab_bubbles_pg.add_service(
            pg_conn,
            target["id"],
            service_kind="dhcp",
            service_name="Wrong DHCP",
            provider_asset_id=source_dc["id"],
        )

    service = lab_bubbles_pg.add_service(
        pg_conn,
        source["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=source_dc["id"],
    )
    with pytest.raises(ValueError, match="asset not found in bubble"):
        lab_bubbles_pg.update_service(
            pg_conn,
            service["id"],
            provider_asset_id=target_dc["id"],
        )


def test_gate_states_allow_workgroup_and_block_domain_before_readiness(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")

    workgroup = lab_bubbles_pg.evaluate_launch_gate(
        pg_conn,
        bubble["id"],
        requires_domain_join=False,
        requires_configmgr=False,
        is_multi_bubble_context=False,
        is_multi_domain_context=False,
    )
    assert workgroup["state"] == "warning"
    assert workgroup["allowed"] is True

    domain = lab_bubbles_pg.evaluate_launch_gate(
        pg_conn,
        bubble["id"],
        requires_domain_join=True,
        requires_configmgr=False,
        is_multi_bubble_context=False,
        is_multi_domain_context=False,
    )
    assert domain["state"] == "blocked"
    assert domain["allowed"] is False
    assert "DC agent has not reported DHCP scope readiness" in domain["reasons"]


def test_build_vm_page_payload_groups_fleets_infra_services_and_unassigned(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="workstation",
        vmid=101,
    )
    dc = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
    )
    lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=dc["id"],
        readiness_state="ready",
    )
    lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="workstation",
        vmid=200,
        membership_state="retired",
    )
    payload = lab_bubbles_pg.build_vm_page_payload(
        pg_conn,
        vms=[
            {
                "vmid": 101,
                "name": "WS01",
                "status": "running",
                "part_of_domain": False,
            },
            {
                "vmid": 130,
                "name": "DC01",
                "status": "running",
                "part_of_domain": True,
            },
            {
                "vmid": 200,
                "name": "LOOSE",
                "status": "stopped",
                "part_of_domain": False,
            },
        ],
        agent_rows=[{"agent_id": "dc01", "vmid": 130, "domain_joined": True}],
    )

    assert payload["workstation_fleets"][0]["bubble"]["name"] == "ACME Lab"
    assert payload["workstation_fleets"][0]["workstation_count"] == 1
    assert payload["critical_infrastructure"][0]["role"] == "domain_controller"
    assert payload["connected_services"][0]["service_kind"] == "dhcp"
    assert payload["unassigned_assets"][0]["vmid"] == 200
    assert payload["gate_states"][0]["bubble_id"] == bubble["id"]


def test_asset_for_agent_returns_bubble_membership(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    asset = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        agent_id="dc01-agent",
    )

    found = lab_bubbles_pg.asset_for_agent(pg_conn, bubble["id"], "dc01-agent")
    assert found["id"] == asset["id"]
    assert lab_bubbles_pg.asset_for_agent(pg_conn, bubble["id"], "missing") is None
