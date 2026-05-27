from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from web import proxmox_sdn


router = APIRouter(prefix="/api/sdn", tags=["sdn"])


class SdnApplyBody(BaseModel):
    lock_token: str = ""


class SdnLockBody(BaseModel):
    lock_token: str = ""
    force: bool = False


class SdnLabBody(BaseModel):
    name: str
    zone: str
    vnet: str
    subnet: str = ""
    domain_name: str = ""
    cidr: str = ""
    gateway_ip: str = ""
    egress_policy: str = "open"
    snat_enabled: bool = True
    firewall_profile: str = "isolated_open_egress"


def _web_app():
    from web import app as web_app

    return web_app


def _database_url() -> str:
    try:
        return _web_app()._database_url()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Autopilot database is not configured")


@contextmanager
def _conn():
    from web import db_pg, lab_bubbles_pg, sdn_labs_pg

    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        sdn_labs_pg.init(conn)
        yield conn


def _api():
    return _web_app()._proxmox_api


def _put():
    return _web_app()._proxmox_api_put


def _delete():
    return _web_app()._proxmox_api_delete


def _body(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _object_id(row: dict[str, Any], *keys: str) -> str:
    for key in ("id", *keys):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _lab_preflight_payload(body: SdnLabBody) -> dict[str, Any]:
    inventory = proxmox_sdn.inventory(_api())
    zones = {_object_id(row, "zone") for row in inventory.get("zones", [])}
    vnets = {_object_id(row, "vnet") for row in inventory.get("vnets", [])}
    subnet_rows = list(inventory.get("subnets_by_vnet", {}).get(body.vnet, []))
    subnet = next((row for row in subnet_rows if _object_id(row, "subnet") == body.subnet), None)
    blocking = []
    warnings = []
    if body.zone not in zones:
        blocking.append({"id": "zone_missing", "detail": f"SDN zone is not present: {body.zone}"})
    if body.vnet not in vnets:
        blocking.append({"id": "vnet_missing", "detail": f"SDN VNet is not present: {body.vnet}"})
    if not body.subnet.strip():
        blocking.append({"id": "subnet_required", "detail": "Select an SDN subnet before creating an isolated lab."})
    if body.subnet and subnet is None:
        blocking.append({"id": "subnet_missing", "detail": f"SDN subnet is not present on {body.vnet}: {body.subnet}"})
    snat_enabled = bool(body.snat_enabled)
    if subnet is not None and body.egress_policy == "open":
        snat_enabled = bool(subnet.get("snat", body.snat_enabled))
        if not snat_enabled:
            warnings.append({
                "id": "snat_not_enabled",
                "detail": "Outbound egress is open, but the selected subnet does not report SNAT enabled.",
            })
    return {
        "ok": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "egress_policy": body.egress_policy or "open",
        "snat_enabled": snat_enabled,
        "firewall_profile": body.firewall_profile or "isolated_open_egress",
    }


@router.get("/inventory")
def inventory():
    return {
        "sdn": proxmox_sdn.inventory(_api()),
        "firewall": proxmox_sdn.firewall_inventory(_api()),
        "labs": [],
    }


@router.post("/zones")
def create_zone(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.create_zone(_api(), _body(body))


@router.patch("/zones/{zone}")
def update_zone(zone: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.update_zone(_api(), zone, _body(body))


@router.delete("/zones/{zone}")
def delete_zone(zone: str):
    return proxmox_sdn.delete_zone(_delete(), zone)


@router.post("/vnets")
def create_vnet(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.create_vnet(_api(), _body(body))


@router.patch("/vnets/{vnet}")
def update_vnet(vnet: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.update_vnet(_api(), vnet, _body(body))


@router.delete("/vnets/{vnet}")
def delete_vnet(vnet: str):
    return proxmox_sdn.delete_vnet(_delete(), vnet)


@router.post("/vnets/{vnet}/subnets")
def create_subnet(vnet: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.create_subnet(_api(), vnet, _body(body))


@router.patch("/vnets/{vnet}/subnets/{subnet:path}")
def update_subnet(vnet: str, subnet: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.update_subnet(_api(), vnet, subnet, _body(body))


@router.delete("/vnets/{vnet}/subnets/{subnet:path}")
def delete_subnet(vnet: str, subnet: str):
    return proxmox_sdn.delete_subnet(_delete(), vnet, subnet)


@router.post("/controllers")
def create_controller(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.create_controller(_api(), _body(body))


@router.patch("/controllers/{controller}")
def update_controller(controller: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.update_controller(_api(), controller, _body(body))


@router.delete("/controllers/{controller}")
def delete_controller(controller: str):
    return proxmox_sdn.delete_controller(_delete(), controller)


@router.post("/ipams")
def create_ipam(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.create_ipam(_api(), _body(body))


@router.delete("/ipams/{ipam}")
def delete_ipam(ipam: str):
    return proxmox_sdn.delete_ipam(_delete(), ipam)


@router.post("/dns")
def create_dns(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.create_dns(_api(), _body(body))


@router.delete("/dns/{dns}")
def delete_dns(dns: str):
    return proxmox_sdn.delete_dns(_delete(), dns)


@router.post("/lock")
def acquire_lock(allow_pending: bool = True):
    return proxmox_sdn.acquire_lock(_api(), allow_pending=allow_pending)


@router.delete("/lock")
def release_lock(body: SdnLockBody = Body(default_factory=SdnLockBody)):
    if not body.lock_token.strip():
        raise HTTPException(status_code=400, detail="lock_token is required")
    return proxmox_sdn.release_lock(_delete(), body.lock_token, force=body.force)


@router.post("/apply")
def apply_sdn(body: SdnApplyBody):
    if not body.lock_token.strip():
        raise HTTPException(status_code=400, detail="lock_token is required")
    return proxmox_sdn.apply_sdn(_put(), body.lock_token.strip())


@router.get("/firewall")
def firewall_inventory(
    node: str | None = Query(default=None),
    vmid: int | None = Query(default=None),
    vnet: str | None = Query(default=None),
):
    return proxmox_sdn.firewall_inventory(_api(), node=node, vmid=vmid, vnet=vnet)


@router.get("/firewall/cluster/options")
def get_cluster_firewall_options():
    return _api()("/cluster/firewall/options") or {}


@router.patch("/firewall/cluster/options")
def set_cluster_firewall_options(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_set_options(_put(), {"kind": "cluster"}, _body(body))


@router.post("/firewall/cluster/rules")
def create_cluster_firewall_rule(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_rule(_api(), {"kind": "cluster"}, _body(body))


@router.patch("/firewall/cluster/rules/{pos}")
def update_cluster_firewall_rule(pos: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_rule(_put(), {"kind": "cluster"}, pos, _body(body))


@router.delete("/firewall/cluster/rules/{pos}")
def delete_cluster_firewall_rule(pos: int):
    return proxmox_sdn.firewall_delete_rule(_delete(), {"kind": "cluster"}, pos)


@router.post("/firewall/cluster/groups")
def create_cluster_firewall_group(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_group(_api(), _body(body))


@router.patch("/firewall/cluster/groups/{group}")
def update_cluster_firewall_group(group: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_group(_put(), group, _body(body))


@router.delete("/firewall/cluster/groups/{group}")
def delete_cluster_firewall_group(group: str):
    return proxmox_sdn.firewall_delete_group(_delete(), group)


@router.post("/firewall/cluster/aliases")
def create_cluster_firewall_alias(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_alias(_api(), {"kind": "cluster"}, _body(body))


@router.patch("/firewall/cluster/aliases/{name}")
def update_cluster_firewall_alias(name: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_alias(_put(), {"kind": "cluster"}, name, _body(body))


@router.delete("/firewall/cluster/aliases/{name}")
def delete_cluster_firewall_alias(name: str):
    return proxmox_sdn.firewall_delete_alias(_delete(), {"kind": "cluster"}, name)


@router.post("/firewall/cluster/ipset")
def create_cluster_firewall_ipset(body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_ipset(_api(), {"kind": "cluster"}, _body(body))


@router.patch("/firewall/cluster/ipset/{name}")
def update_cluster_firewall_ipset(name: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_ipset(_put(), {"kind": "cluster"}, name, _body(body))


@router.delete("/firewall/cluster/ipset/{name}")
def delete_cluster_firewall_ipset(name: str):
    return proxmox_sdn.firewall_delete_ipset(_delete(), {"kind": "cluster"}, name)


@router.get("/firewall/cluster/macros")
def get_cluster_firewall_macros():
    return _api()("/cluster/firewall/macros") or []


@router.get("/firewall/cluster/refs")
def get_cluster_firewall_refs():
    return _api()("/cluster/firewall/refs") or []


@router.get("/firewall/nodes/{node}/options")
def get_node_firewall_options(node: str):
    return _api()(proxmox_sdn.firewall_scope_path({"kind": "node", "node": node}, "options")) or {}


@router.patch("/firewall/nodes/{node}/options")
def set_node_firewall_options(node: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_set_options(_put(), {"kind": "node", "node": node}, _body(body))


@router.post("/firewall/nodes/{node}/rules")
def create_node_firewall_rule(node: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_rule(_api(), {"kind": "node", "node": node}, _body(body))


@router.patch("/firewall/nodes/{node}/rules/{pos}")
def update_node_firewall_rule(node: str, pos: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_rule(_put(), {"kind": "node", "node": node}, pos, _body(body))


@router.delete("/firewall/nodes/{node}/rules/{pos}")
def delete_node_firewall_rule(node: str, pos: int):
    return proxmox_sdn.firewall_delete_rule(_delete(), {"kind": "node", "node": node}, pos)


@router.get("/firewall/vnets/{vnet}/options")
def get_vnet_firewall_options(vnet: str):
    return _api()(proxmox_sdn.firewall_scope_path({"kind": "vnet", "vnet": vnet}, "options")) or {}


@router.patch("/firewall/vnets/{vnet}/options")
def set_vnet_firewall_options(vnet: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_set_options(_put(), {"kind": "vnet", "vnet": vnet}, _body(body))


@router.post("/firewall/vnets/{vnet}/rules")
def create_vnet_firewall_rule(vnet: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_rule(_api(), {"kind": "vnet", "vnet": vnet}, _body(body))


@router.patch("/firewall/vnets/{vnet}/rules/{pos}")
def update_vnet_firewall_rule(vnet: str, pos: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_rule(_put(), {"kind": "vnet", "vnet": vnet}, pos, _body(body))


@router.delete("/firewall/vnets/{vnet}/rules/{pos}")
def delete_vnet_firewall_rule(vnet: str, pos: int):
    return proxmox_sdn.firewall_delete_rule(_delete(), {"kind": "vnet", "vnet": vnet}, pos)


@router.get("/firewall/vms/{node}/{vmid}/options")
def get_vm_firewall_options(node: str, vmid: int):
    return _api()(proxmox_sdn.firewall_scope_path({"kind": "qemu", "node": node, "vmid": vmid}, "options")) or {}


@router.patch("/firewall/vms/{node}/{vmid}/options")
def set_vm_firewall_options(node: str, vmid: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_set_options(_put(), {"kind": "qemu", "node": node, "vmid": vmid}, _body(body))


@router.post("/firewall/vms/{node}/{vmid}/rules")
def create_vm_firewall_rule(node: str, vmid: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_rule(_api(), {"kind": "qemu", "node": node, "vmid": vmid}, _body(body))


@router.patch("/firewall/vms/{node}/{vmid}/rules/{pos}")
def update_vm_firewall_rule(node: str, vmid: int, pos: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_rule(_put(), {"kind": "qemu", "node": node, "vmid": vmid}, pos, _body(body))


@router.delete("/firewall/vms/{node}/{vmid}/rules/{pos}")
def delete_vm_firewall_rule(node: str, vmid: int, pos: int):
    return proxmox_sdn.firewall_delete_rule(_delete(), {"kind": "qemu", "node": node, "vmid": vmid}, pos)


@router.post("/firewall/vms/{node}/{vmid}/aliases")
def create_vm_firewall_alias(node: str, vmid: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_alias(_api(), {"kind": "qemu", "node": node, "vmid": vmid}, _body(body))


@router.patch("/firewall/vms/{node}/{vmid}/aliases/{name}")
def update_vm_firewall_alias(node: str, vmid: int, name: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_alias(_put(), {"kind": "qemu", "node": node, "vmid": vmid}, name, _body(body))


@router.delete("/firewall/vms/{node}/{vmid}/aliases/{name}")
def delete_vm_firewall_alias(node: str, vmid: int, name: str):
    return proxmox_sdn.firewall_delete_alias(_delete(), {"kind": "qemu", "node": node, "vmid": vmid}, name)


@router.post("/firewall/vms/{node}/{vmid}/ipset")
def create_vm_firewall_ipset(node: str, vmid: int, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_create_ipset(_api(), {"kind": "qemu", "node": node, "vmid": vmid}, _body(body))


@router.patch("/firewall/vms/{node}/{vmid}/ipset/{name}")
def update_vm_firewall_ipset(node: str, vmid: int, name: str, body: dict[str, Any] = Body(default_factory=dict)):
    return proxmox_sdn.firewall_update_ipset(_put(), {"kind": "qemu", "node": node, "vmid": vmid}, name, _body(body))


@router.delete("/firewall/vms/{node}/{vmid}/ipset/{name}")
def delete_vm_firewall_ipset(node: str, vmid: int, name: str):
    return proxmox_sdn.firewall_delete_ipset(_delete(), {"kind": "qemu", "node": node, "vmid": vmid}, name)


@router.post("/labs/preflight")
def lab_preflight(body: SdnLabBody):
    return _lab_preflight_payload(body)


@router.post("/labs", status_code=201)
def create_lab(body: SdnLabBody):
    from web import lab_bubbles_pg, sdn_labs_pg

    preflight = _lab_preflight_payload(body)
    if preflight["blocking"]:
        raise HTTPException(status_code=409, detail={"message": "SDN lab preflight failed", **preflight})
    with _conn() as conn:
        lab_bubbles_pg.init(conn)
        sdn_labs_pg.init(conn)
        bubble = lab_bubbles_pg.create_bubble(
            conn,
            name=body.name,
            domain_name=body.domain_name,
            cidr=body.cidr,
            gateway_ip=body.gateway_ip,
            planned_bridge=body.vnet,
            isolation_status="planned",
        )
        binding = sdn_labs_pg.upsert_binding(
            conn,
            bubble_id=bubble["id"],
            zone=body.zone,
            vnet=body.vnet,
            subnet=body.subnet,
            egress_policy=preflight["egress_policy"],
            snat_enabled=preflight["snat_enabled"],
            firewall_profile=preflight["firewall_profile"],
            actor="operator",
        )
    return {"bubble": bubble, "binding": binding, "preflight": preflight}


@router.get("/labs/{bubble_id}/network")
def get_lab_network(bubble_id: str):
    from web import sdn_labs_pg

    with _conn() as conn:
        sdn_labs_pg.init(conn)
        binding = sdn_labs_pg.get_binding(conn, bubble_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Lab SDN binding not found")
    return {"binding": binding}
