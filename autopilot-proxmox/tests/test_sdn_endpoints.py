def test_lab_binding_defaults_outbound_egress_open(pg_conn):
    from web import lab_bubbles_pg, sdn_labs_pg

    lab_bubbles_pg.init(pg_conn)
    sdn_labs_pg.reset_for_tests(pg_conn)
    sdn_labs_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Isolated Lab")

    binding = sdn_labs_pg.upsert_binding(
        pg_conn,
        bubble_id=bubble["id"],
        zone="lab-simple",
        vnet="lab101",
        subnet="10.60.10.0/24",
        actor="test",
    )

    assert binding["egress_policy"] == "open"
    assert binding["snat_enabled"] is True
    assert binding["firewall_profile"] == "isolated_open_egress"


def test_sdn_inventory_endpoint_returns_zones_vnets_and_firewall(web_client, monkeypatch):
    from web import sdn_endpoints

    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "inventory",
        lambda api: {
            "zones": [{"id": "lab-simple", "type": "simple"}],
            "vnets": [{"id": "lab101", "zone": "lab-simple"}],
            "subnets_by_vnet": {"lab101": [{"subnet": "10.60.10.0/24"}]},
            "controllers": [],
            "ipams": [{"id": "pve", "type": "pve"}],
            "dns": [],
            "fabrics": [],
        },
    )
    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "firewall_inventory",
        lambda api, **kwargs: {
            "cluster": {"options": {}, "rules": []},
            "nodes": {},
            "vnets": {},
            "vms": {},
        },
    )

    response = web_client.get("/api/sdn/inventory")

    assert response.status_code == 200
    body = response.json()
    assert body["sdn"]["zones"][0]["id"] == "lab-simple"
    assert body["firewall"]["cluster"]["rules"] == []


def test_sdn_apply_requires_explicit_lock_token(web_client):
    response = web_client.post("/api/sdn/apply", json={})

    assert response.status_code == 400
    assert "lock_token" in response.json()["detail"]


def test_lab_preflight_requires_subnet_for_open_egress(web_client, monkeypatch):
    from web import sdn_endpoints

    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "inventory",
        lambda api: {
            "zones": [{"id": "lab-simple", "type": "simple"}],
            "vnets": [{"id": "lab101", "zone": "lab-simple"}],
            "subnets_by_vnet": {"lab101": []},
            "controllers": [],
            "ipams": [],
            "dns": [],
            "fabrics": [],
        },
    )

    response = web_client.post(
        "/api/sdn/labs/preflight",
        json={"name": "ACME Lab", "zone": "lab-simple", "vnet": "lab101"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["blocking"] == [
        {"id": "subnet_required", "detail": "Select an SDN subnet before creating an isolated lab."}
    ]


def test_sdn_vnet_firewall_rule_route_targets_vnet_scope(web_client, monkeypatch):
    from web import sdn_endpoints

    calls = []

    def fake_create_rule(api, scope, body):
        calls.append((scope, body))
        return {"pos": 0, **body}

    monkeypatch.setattr(sdn_endpoints.proxmox_sdn, "firewall_create_rule", fake_create_rule)

    response = web_client.post(
        "/api/sdn/firewall/vnets/lab101/rules",
        json={
            "type": "out",
            "action": "ACCEPT",
            "dest": "0.0.0.0/0",
            "comment": "allow lab outbound egress",
        },
    )

    assert response.status_code == 200
    assert calls == [
        (
            {"kind": "vnet", "vnet": "lab101"},
            {
                "type": "out",
                "action": "ACCEPT",
                "dest": "0.0.0.0/0",
                "comment": "allow lab outbound egress",
            },
        )
    ]


def test_lab_create_uses_open_egress_and_snat_by_default(web_client, pg_conn, monkeypatch):
    from web import lab_bubbles_pg, sdn_endpoints

    lab_bubbles_pg.init(pg_conn)
    monkeypatch.setattr(sdn_endpoints, "_conn", lambda: pg_conn)
    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "inventory",
        lambda api: {
            "zones": [{"id": "lab-simple", "type": "simple"}],
            "vnets": [{"id": "lab101", "zone": "lab-simple"}],
            "subnets_by_vnet": {"lab101": [{"subnet": "10.60.10.0/24", "snat": True}]},
            "controllers": [],
            "ipams": [],
            "dns": [],
            "fabrics": [],
        },
    )

    response = web_client.post(
        "/api/sdn/labs",
        json={
            "name": "ACME Lab",
            "zone": "lab-simple",
            "vnet": "lab101",
            "subnet": "10.60.10.0/24",
            "domain_name": "lab.acme.test",
            "cidr": "10.60.10.0/24",
            "gateway_ip": "10.60.10.1",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["binding"]["egress_policy"] == "open"
    assert body["binding"]["snat_enabled"] is True


def _pve_generated_subnet_inventory():
    return {
        "zones": [{"id": "lab-simple", "type": "simple"}],
        "vnets": [{"id": "lab101", "zone": "lab-simple"}],
        "subnets_by_vnet": {
            "lab101": [
                {
                    "id": "lab-simple-10.60.10.0-24",
                    "subnet": "lab-simple-10.60.10.0-24",
                    "cidr": "10.60.10.0/24",
                    "network": "10.60.10.0",
                    "gateway": "10.60.10.1",
                    "snat": 1,
                    "dhcp-dns-server": "10.60.10.10",
                    "dhcp-range": [
                        {
                            "start-address": "10.60.10.100",
                            "end-address": "10.60.10.199",
                        }
                    ],
                }
            ]
        },
        "controllers": [],
        "ipams": [],
        "dns": [],
        "fabrics": [],
    }


def test_orphan_vnets_exposes_provider_subnet_id_and_cidr(web_client, pg_conn, monkeypatch):
    from web import lab_bubbles_pg, sdn_endpoints, sdn_labs_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    sdn_labs_pg.reset_for_tests(pg_conn)
    sdn_labs_pg.init(pg_conn)
    monkeypatch.setattr(sdn_endpoints, "_conn", lambda: pg_conn)
    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "inventory",
        lambda api: _pve_generated_subnet_inventory(),
    )

    response = web_client.get("/api/sdn/labs/orphan-vnets")

    assert response.status_code == 200
    body = response.json()
    orphan = body["orphan_vnets"][0]
    assert orphan["vnet"] == "lab101"
    assert orphan["subnet"]["subnet"] == "lab-simple-10.60.10.0-24"
    assert orphan["subnet"]["cidr"] == "10.60.10.0/24"
    assert orphan["subnet"]["network"] == "10.60.10.0"
    assert orphan["subnet"]["gateway"] == "10.60.10.1"
    assert orphan["subnet"]["dhcp_range"] == "start-address=10.60.10.100,end-address=10.60.10.199"


def test_lab_network_prefers_live_cidr_over_pve_subnet_id(web_client, pg_conn, monkeypatch):
    from web import lab_bubbles_pg, sdn_endpoints, sdn_labs_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    sdn_labs_pg.reset_for_tests(pg_conn)
    sdn_labs_pg.init(pg_conn)
    monkeypatch.setattr(sdn_endpoints, "_conn", lambda: pg_conn)
    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "inventory",
        lambda api: _pve_generated_subnet_inventory(),
    )
    bubble = lab_bubbles_pg.create_bubble(
        pg_conn,
        name="Network CIDR Lab",
        cidr="lab-simple-10.60.10.0-24",
        gateway_ip="10.60.10.1",
    )
    sdn_labs_pg.upsert_binding(
        pg_conn,
        bubble_id=bubble["id"],
        zone="lab-simple",
        vnet="lab101",
        subnet="lab-simple-10.60.10.0-24",
        actor="test",
    )

    response = web_client.get(f"/api/sdn/labs/{bubble['id']}/network")

    assert response.status_code == 200
    body = response.json()
    assert body["subnet"]["subnet"] == "lab-simple-10.60.10.0-24"
    assert body["subnet"]["cidr"] == "10.60.10.0/24"
    assert body["subnet"]["network"] == "10.60.10.0"
    assert body["subnet"]["gateway"] == "10.60.10.1"
    assert body["subnet"]["dhcp_range"] == "start-address=10.60.10.100,end-address=10.60.10.199"


def test_lab_create_adopted_existing_sdn_marks_bubble_active_with_cidr(web_client, pg_conn, monkeypatch):
    from web import lab_bubbles_pg, sdn_endpoints, sdn_labs_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    sdn_labs_pg.reset_for_tests(pg_conn)
    sdn_labs_pg.init(pg_conn)
    monkeypatch.setattr(sdn_endpoints, "_conn", lambda: pg_conn)
    monkeypatch.setattr(
        sdn_endpoints.proxmox_sdn,
        "inventory",
        lambda api: _pve_generated_subnet_inventory(),
    )

    response = web_client.post(
        "/api/sdn/labs",
        json={
            "name": "Adopted CIDR Lab",
            "zone": "lab-simple",
            "vnet": "lab101",
            "subnet": "lab-simple-10.60.10.0-24",
            "domain_name": "lab.example.test",
            "cidr": "10.60.10.0/24",
            "gateway_ip": "10.60.10.1",
            "dhcp_scope": "10.60.10.0",
            "dhcp_pool_start": "10.60.10.100",
            "dhcp_pool_end": "10.60.10.199",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["bubble"]["cidr"] == "10.60.10.0/24"
    assert body["bubble"]["dhcp_scope"] == "10.60.10.0"
    assert body["bubble"]["dhcp_pool_start"] == "10.60.10.100"
    assert body["bubble"]["dhcp_pool_end"] == "10.60.10.199"
    assert body["bubble"]["planned_bridge"] == "lab101"
    assert body["bubble"]["lifecycle_state"] == "active"
    assert body["bubble"]["isolation_status"] == "isolated"
    assert body["binding"]["subnet"] == "lab-simple-10.60.10.0-24"
    assert body["binding"]["snat_enabled"] is True
