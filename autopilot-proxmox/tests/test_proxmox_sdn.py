from web import proxmox_sdn


def test_sdn_inventory_reads_running_and_pending_collections():
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        if path == "/cluster/sdn/zones":
            return [{"zone": "lab-simple", "type": "simple"}]
        if path == "/cluster/sdn/vnets":
            return [{"vnet": "lab101", "zone": "lab-simple"}]
        if path == "/cluster/sdn/vnets/lab101/subnets":
            return [{"subnet": "10.60.10.0/24", "gateway": "10.60.10.1"}]
        return []

    payload = proxmox_sdn.inventory(fake_api)

    assert payload["zones"][0]["id"] == "lab-simple"
    assert payload["vnets"][0]["id"] == "lab101"
    assert payload["subnets_by_vnet"]["lab101"][0]["subnet"] == "10.60.10.0/24"
    assert ("GET", "/cluster/sdn/vnets/lab101/subnets", None) in calls


def test_secret_required_dns_create_is_rejected_before_pve_call():
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        return {}

    result = proxmox_sdn.create_dns(
        fake_api,
        {"dns": "pdns1", "type": "powerdns", "url": "https://pdns.local"},
    )

    assert result["ok"] is False
    assert result["code"] == "secret_required"
    assert calls == []


def test_supplied_provider_secret_is_still_rejected_before_pve_call():
    calls = []

    def fake_api(path, method="GET", data=None):
        calls.append((method, path, data))
        return {}

    result = proxmox_sdn.create_ipam(
        fake_api,
        {
            "ipam": "netbox1",
            "type": "netbox",
            "url": "https://netbox.local",
            "token": "do-not-forward",
        },
    )

    assert result["ok"] is False
    assert result["code"] == "secret_required"
    assert "do-not-forward" not in repr(result)
    assert calls == []


def test_firewall_scope_path_maps_cluster_vnet_node_and_vm_scopes():
    assert proxmox_sdn.firewall_scope_path({"kind": "cluster"}, "rules") == "/cluster/firewall/rules"
    assert (
        proxmox_sdn.firewall_scope_path({"kind": "vnet", "vnet": "lab101"}, "rules")
        == "/cluster/sdn/vnets/lab101/firewall/rules"
    )
    assert (
        proxmox_sdn.firewall_scope_path({"kind": "node", "node": "pve1"}, "options")
        == "/nodes/pve1/firewall/options"
    )
    assert (
        proxmox_sdn.firewall_scope_path({"kind": "qemu", "node": "pve1", "vmid": 101}, "ipset")
        == "/nodes/pve1/qemu/101/firewall/ipset"
    )


def test_apply_uses_proxmox_lock_token_field():
    calls = []

    def fake_put(path, data=None):
        calls.append((path, data))
        return {"ok": True}

    result = proxmox_sdn.apply_sdn(fake_put, "lock-token")

    assert result == {"ok": True}
    assert calls == [("/cluster/sdn", {"lock-token": "lock-token"})]


def test_release_lock_uses_query_params_because_proxmox_rejects_delete_bodies():
    calls = []

    def fake_delete(path):
        calls.append(path)
        return {"ok": True}

    result = proxmox_sdn.release_lock(fake_delete, "lock-token", force=True)

    assert result == {"ok": True}
    assert calls == ["/cluster/sdn/lock?lock-token=lock-token&force=1"]
