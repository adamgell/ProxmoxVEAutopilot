"""Proxmox network target normalization shared by provisioning surfaces."""
from __future__ import annotations


def normalize_network_targets(
    *,
    node: str | None,
    bridges: list[str],
    vnets: list[dict] | None = None,
) -> list[dict]:
    targets: list[dict] = []
    seen: set[str] = set()
    node_label = str(node or "").strip()
    for bridge in bridges:
        value = str(bridge or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        description = "Linux bridge"
        if node_label:
            description = f"Linux bridge on {node_label}"
        targets.append({
            "kind": "bridge",
            "value": value,
            "label": value,
            "description": description,
        })
    for row in vnets or []:
        if not isinstance(row, dict):
            continue
        value = _vnet_id(row)
        if not value or value in seen:
            continue
        seen.add(value)
        zone = str(row.get("zone") or "").strip()
        label = str(row.get("alias") or row.get("name") or value).strip() or value
        description = "Proxmox SDN VNet"
        if zone:
            description = f"Proxmox SDN VNet in {zone}"
        target = {
            "kind": "sdn_vnet",
            "value": value,
            "label": label,
            "description": description,
        }
        if zone:
            target["zone"] = zone
        tag = str(row.get("tag") or "").strip()
        if tag:
            target["tag"] = tag
        targets.append(target)
    return targets


def network_target_values(options: dict) -> set[str]:
    values = {str(item).strip() for item in options.get("bridges") or [] if str(item).strip()}
    for target in options.get("network_targets") or []:
        if not isinstance(target, dict):
            continue
        value = str(target.get("value") or "").strip()
        if value:
            values.add(value)
    return values


def _vnet_id(row: dict) -> str:
    for key in ("vnet", "id", "vnetid"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""
