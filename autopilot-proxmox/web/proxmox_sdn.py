from __future__ import annotations

from urllib.parse import quote


SDN_ZONE_TYPES = ("simple", "vlan", "qinq", "vxlan", "evpn", "faucet")
SDN_CONTROLLER_TYPES = ("bgp", "evpn", "faucet", "isis")
SDN_IPAM_TYPES = ("pve", "netbox", "phpipam")
SDN_DNS_TYPES = ("powerdns",)

SECRET_FIELD_NAMES = {"key", "token", "password", "secret"}
SECRET_REQUIRED_PROVIDER_TYPES = {
    ("dns", "powerdns"): ("key",),
    ("ipam", "netbox"): ("token",),
    ("ipam", "phpipam"): ("token",),
}
SECRET_REQUIRED_RESPONSE = {
    "ok": False,
    "code": "secret_required",
    "detail": "Configure this provider in Proxmox first; Autopilot does not store provider secrets.",
}


def _part(value) -> str:
    return quote(str(value), safe="")


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in SECRET_FIELD_NAMES}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    # PVE's API expects booleans as 0/1 (or sometimes lowercase "true"/"false")
    # over the wire; Python's `False`/`True` get stringified by `requests` to
    # "False"/"True" (capitalized), which PVE rejects with
    # "vlanaware: type check ('boolean') failed - got 'False'". Coerce here so
    # every SDN body gets a wire-friendly form.
    # Note: isinstance(True, int) is True in Python, so the bool check must
    # come first.
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _with_id(row: dict, key: str) -> dict:
    clean = _clean(dict(row or {}))
    if "id" not in clean and clean.get(key) is not None:
        clean["id"] = clean[key]
    return clean


def _parse_dhcp_range(value) -> tuple[str, str]:
    """Normalize PVE's dhcp-range field into (start, end) IP strings.

    PVE returns this as a list of {start-address, end-address} dicts in
    inventory responses, but config dumps elsewhere use the legacy
    "start-address=X,end-address=Y" comma-separated string. Accept both
    plus single-dict and array-of-strings forms so the UI sees a stable
    shape regardless of which PVE endpoint the data came from.
    """
    if value is None:
        return "", ""
    if isinstance(value, dict):
        return (
            str(value.get("start-address") or value.get("start") or "").strip(),
            str(value.get("end-address") or value.get("end") or "").strip(),
        )
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                start = str(item.get("start-address") or item.get("start") or "").strip()
                end = str(item.get("end-address") or item.get("end") or "").strip()
                if start or end:
                    return start, end
            elif isinstance(item, str):
                start, end = _parse_dhcp_range(item)
                if start or end:
                    return start, end
        return "", ""
    if isinstance(value, str):
        start = ""
        end = ""
        for part in value.split(","):
            kv = part.split("=", 1)
            if len(kv) != 2:
                continue
            key, raw = kv[0].strip().lower(), kv[1].strip()
            if key == "start-address":
                start = raw
            elif key == "end-address":
                end = raw
        return start, end
    return "", ""


def _normalize_subnet(row: dict) -> dict:
    """Pre-parse subnet fields the UI consumes so consumers don't have
    to know that PVE sometimes returns dhcp-range as a list-of-dicts and
    sometimes as a comma-separated string."""
    out = _with_id(row, "subnet")
    start, end = _parse_dhcp_range(out.get("dhcp-range"))
    out["dhcp_range_start"] = start
    out["dhcp_range_end"] = end
    return out


def _surface_pve_error(exc: Exception, path: str) -> None:
    """Translate an upstream Proxmox HTTPError into a FastAPI HTTPException.

    The PVE API returns 400 "Parameter verification failed." (often with a
    detail like 'zone: ... does not exist') for user-input mistakes. The
    raw requests.HTTPError bubbles up as a generic 500 in FastAPI's
    handler, which means the React form shows "Internal Server Error"
    instead of the actual PVE reason. Catch the upstream status + body
    and rethrow with the same code so the UI surfaces what's wrong.
    """
    from fastapi import HTTPException

    response = getattr(exc, "response", None)
    if response is None:
        raise HTTPException(status_code=502, detail=f"upstream PVE call failed at {path}: {exc}")
    status = int(getattr(response, "status_code", 502) or 502)
    if status < 400 or status >= 600:
        status = 502
    body_text = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            errors = payload.get("errors") or {}
            if isinstance(errors, dict) and errors:
                body_text = "; ".join(f"{k}: {v}" for k, v in errors.items())
            else:
                body_text = str(payload.get("message") or payload.get("data") or "")
    except Exception:
        body_text = ""
    if not body_text:
        try:
            body_text = response.text or ""
        except Exception:
            body_text = ""
    body_text = body_text.strip()
    detail = body_text or f"upstream PVE error at {path}"
    raise HTTPException(status_code=status, detail=detail) from exc


def _call(pve_api, path: str, *, method: str = "POST", data: dict | None = None) -> dict:
    try:
        result = pve_api(path, method=method, data=_clean(data or {}))
    except Exception as exc:
        _surface_pve_error(exc, path)
    return _clean(result or {})


def _delete(pve_delete, path: str) -> dict:
    try:
        result = pve_delete(path)
    except Exception as exc:
        _surface_pve_error(exc, path)
    return _clean(result or {})


def _put(pve_put, path: str, data: dict | None = None) -> dict:
    try:
        result = pve_put(path, data=_clean(data or {}))
    except Exception as exc:
        _surface_pve_error(exc, path)
    return _clean(result or {})


def _secret_required(kind: str, body: dict) -> bool:
    provider_type = str((body or {}).get("type") or "").strip().lower()
    return (kind, provider_type) in SECRET_REQUIRED_PROVIDER_TYPES


def _secret_required_result() -> dict:
    return dict(SECRET_REQUIRED_RESPONSE)


def inventory(pve_api) -> dict:
    zones = [_with_id(row, "zone") for row in pve_api("/cluster/sdn/zones") or []]
    vnets = [_with_id(row, "vnet") for row in pve_api("/cluster/sdn/vnets") or []]
    subnets_by_vnet = {}
    for vnet in vnets:
        vnet_id = vnet.get("id") or vnet.get("vnet")
        if not vnet_id:
            continue
        try:
            rows = pve_api(f"/cluster/sdn/vnets/{_part(vnet_id)}/subnets") or []
        except Exception:
            rows = []
        subnets_by_vnet[str(vnet_id)] = [_normalize_subnet(row) for row in rows]
    try:
        controllers = [_with_id(row, "controller") for row in pve_api("/cluster/sdn/controllers") or []]
    except Exception:
        controllers = []
    try:
        ipams = [_with_id(row, "ipam") for row in pve_api("/cluster/sdn/ipams") or []]
    except Exception:
        ipams = []
    try:
        dns = [_with_id(row, "dns") for row in pve_api("/cluster/sdn/dns") or []]
    except Exception:
        dns = []
    try:
        fabrics = [_with_id(row, "fabric") for row in pve_api("/cluster/sdn/fabrics") or []]
    except Exception:
        fabrics = []
    return {
        "zones": zones,
        "vnets": vnets,
        "subnets_by_vnet": subnets_by_vnet,
        "controllers": controllers,
        "ipams": ipams,
        "dns": dns,
        "fabrics": fabrics,
    }


def create_zone(pve_api, body: dict) -> dict:
    return _call(pve_api, "/cluster/sdn/zones", data=body)


def update_zone(pve_api, zone: str, body: dict) -> dict:
    return _call(pve_api, f"/cluster/sdn/zones/{_part(zone)}", method="PUT", data=body)


def delete_zone(pve_delete, zone: str) -> dict:
    return _delete(pve_delete, f"/cluster/sdn/zones/{_part(zone)}")


def create_vnet(pve_api, body: dict) -> dict:
    return _call(pve_api, "/cluster/sdn/vnets", data=body)


def update_vnet(pve_api, vnet: str, body: dict) -> dict:
    return _call(pve_api, f"/cluster/sdn/vnets/{_part(vnet)}", method="PUT", data=body)


def delete_vnet(pve_delete, vnet: str) -> dict:
    return _delete(pve_delete, f"/cluster/sdn/vnets/{_part(vnet)}")


def create_subnet(pve_api, vnet: str, body: dict) -> dict:
    return _call(pve_api, f"/cluster/sdn/vnets/{_part(vnet)}/subnets", data=body)


def update_subnet(pve_api, vnet: str, subnet: str, body: dict) -> dict:
    return _call(
        pve_api,
        f"/cluster/sdn/vnets/{_part(vnet)}/subnets/{_part(subnet)}",
        method="PUT",
        data=body,
    )


def delete_subnet(pve_delete, vnet: str, subnet: str) -> dict:
    return _delete(pve_delete, f"/cluster/sdn/vnets/{_part(vnet)}/subnets/{_part(subnet)}")


def create_controller(pve_api, body: dict) -> dict:
    return _call(pve_api, "/cluster/sdn/controllers", data=body)


def update_controller(pve_api, controller: str, body: dict) -> dict:
    return _call(pve_api, f"/cluster/sdn/controllers/{_part(controller)}", method="PUT", data=body)


def delete_controller(pve_delete, controller: str) -> dict:
    return _delete(pve_delete, f"/cluster/sdn/controllers/{_part(controller)}")


def create_ipam(pve_api, body: dict) -> dict:
    if _secret_required("ipam", body):
        return _secret_required_result()
    return _call(pve_api, "/cluster/sdn/ipams", data=body)


def delete_ipam(pve_delete, ipam: str) -> dict:
    return _delete(pve_delete, f"/cluster/sdn/ipams/{_part(ipam)}")


def create_dns(pve_api, body: dict) -> dict:
    if _secret_required("dns", body):
        return _secret_required_result()
    return _call(pve_api, "/cluster/sdn/dns", data=body)


def delete_dns(pve_delete, dns: str) -> dict:
    return _delete(pve_delete, f"/cluster/sdn/dns/{_part(dns)}")


def acquire_lock(pve_api, allow_pending: bool = True) -> dict:
    return _call(pve_api, "/cluster/sdn/lock", data={"allow-pending": int(bool(allow_pending))})


def release_lock(pve_delete, lock_token: str, force: bool = False) -> dict:
    query = f"lock-token={_part(lock_token)}"
    if force:
        query = f"{query}&force=1"
    result = pve_delete(f"/cluster/sdn/lock?{query}")
    return _clean(result or {})


def apply_sdn(pve_put, lock_token: str, *, release_lock: bool = True) -> dict:
    """Apply pending SDN changes and (by default) release the lock.

    PVE's PUT /cluster/sdn accepts release-lock=1 alongside the lock-token
    so the apply + release happens atomically. Without it the lock lingers
    until the API client explicitly releases it, which leaves the SDN
    effectively-locked from any other operator until cleanup.
    """
    body: dict = {"lock-token": lock_token}
    if release_lock:
        body["release-lock"] = 1
    return _put(pve_put, "/cluster/sdn", body)


def firewall_scope_path(scope: dict, leaf: str) -> str:
    kind = str((scope or {}).get("kind") or "cluster").lower()
    leaf = leaf.strip("/")
    if kind == "cluster":
        return f"/cluster/firewall/{leaf}"
    if kind == "node":
        return f"/nodes/{_part(scope['node'])}/firewall/{leaf}"
    if kind == "vnet":
        return f"/cluster/sdn/vnets/{_part(scope['vnet'])}/firewall/{leaf}"
    if kind in {"qemu", "vm"}:
        return f"/nodes/{_part(scope['node'])}/qemu/{_part(scope['vmid'])}/firewall/{leaf}"
    raise ValueError(f"Unknown firewall scope kind: {kind}")


def firewall_inventory(pve_api, node: str | None = None, vmid: int | None = None, vnet: str | None = None) -> dict:
    payload = {
        "cluster": {
            "options": _clean(pve_api("/cluster/firewall/options") or {}),
            "rules": _clean(pve_api("/cluster/firewall/rules") or []),
            "groups": _clean(pve_api("/cluster/firewall/groups") or []),
            "aliases": _clean(pve_api("/cluster/firewall/aliases") or []),
            "ipset": _clean(pve_api("/cluster/firewall/ipset") or []),
        },
        "nodes": {},
        "vnets": {},
        "vms": {},
    }
    try:
        payload["cluster"]["macros"] = _clean(pve_api("/cluster/firewall/macros") or [])
    except Exception:
        payload["cluster"]["macros"] = []
    try:
        payload["cluster"]["refs"] = _clean(pve_api("/cluster/firewall/refs") or [])
    except Exception:
        payload["cluster"]["refs"] = []
    if node:
        scope = {"kind": "node", "node": node}
        payload["nodes"][node] = {
            "options": _clean(pve_api(firewall_scope_path(scope, "options")) or {}),
            "rules": _clean(pve_api(firewall_scope_path(scope, "rules")) or []),
        }
    if vnet:
        scope = {"kind": "vnet", "vnet": vnet}
        payload["vnets"][vnet] = {
            "options": _clean(pve_api(firewall_scope_path(scope, "options")) or {}),
            "rules": _clean(pve_api(firewall_scope_path(scope, "rules")) or []),
        }
    if node and vmid:
        scope = {"kind": "qemu", "node": node, "vmid": vmid}
        vm_key = f"{node}/{vmid}"
        payload["vms"][vm_key] = {
            "options": _clean(pve_api(firewall_scope_path(scope, "options")) or {}),
            "rules": _clean(pve_api(firewall_scope_path(scope, "rules")) or []),
            "aliases": _clean(pve_api(firewall_scope_path(scope, "aliases")) or []),
            "ipset": _clean(pve_api(firewall_scope_path(scope, "ipset")) or []),
        }
    return payload


def firewall_create_rule(pve_api, scope: dict, body: dict) -> dict:
    return _call(pve_api, firewall_scope_path(scope, "rules"), data=body)


def firewall_update_rule(pve_put, scope: dict, pos: int, body: dict) -> dict:
    return _put(pve_put, firewall_scope_path(scope, f"rules/{_part(pos)}"), body)


def firewall_delete_rule(pve_delete, scope: dict, pos: int) -> dict:
    return _delete(pve_delete, firewall_scope_path(scope, f"rules/{_part(pos)}"))


def firewall_set_options(pve_put, scope: dict, body: dict) -> dict:
    return _put(pve_put, firewall_scope_path(scope, "options"), body)


def firewall_create_group(pve_api, body: dict) -> dict:
    return _call(pve_api, "/cluster/firewall/groups", data=body)


def firewall_update_group(pve_put, group: str, body: dict) -> dict:
    return _put(pve_put, f"/cluster/firewall/groups/{_part(group)}", body)


def firewall_delete_group(pve_delete, group: str) -> dict:
    return _delete(pve_delete, f"/cluster/firewall/groups/{_part(group)}")


def firewall_create_alias(pve_api, scope: dict, body: dict) -> dict:
    return _call(pve_api, firewall_scope_path(scope, "aliases"), data=body)


def firewall_update_alias(pve_put, scope: dict, name: str, body: dict) -> dict:
    return _put(pve_put, firewall_scope_path(scope, f"aliases/{_part(name)}"), body)


def firewall_delete_alias(pve_delete, scope: dict, name: str) -> dict:
    return _delete(pve_delete, firewall_scope_path(scope, f"aliases/{_part(name)}"))


def firewall_create_ipset(pve_api, scope: dict, body: dict) -> dict:
    return _call(pve_api, firewall_scope_path(scope, "ipset"), data=body)


def firewall_update_ipset(pve_put, scope: dict, name: str, body: dict) -> dict:
    return _put(pve_put, firewall_scope_path(scope, f"ipset/{_part(name)}"), body)


def firewall_delete_ipset(pve_delete, scope: dict, name: str) -> dict:
    return _delete(pve_delete, firewall_scope_path(scope, f"ipset/{_part(name)}"))
