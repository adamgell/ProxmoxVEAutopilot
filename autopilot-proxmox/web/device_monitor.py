"""Device state monitor — probes + sweep orchestrator.

Probe functions are **pure**: they take already-fetched raw data
(Proxmox config dict, LDAP entries list, Graph response dict) and
return a normalised result. That keeps them trivially testable
without mocking HTTP / LDAP libraries.

The sweep function does all the I/O: fetches data, invokes the
probes, writes rows to the DB. Live I/O backends sit in
:func:`build_live_context` and can be swapped for fakes in tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from web import device_history_db

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context — injection point for I/O backends
# ---------------------------------------------------------------------------

@dataclass
class MonitorContext:
    """Bag of callables the sweep uses for I/O. Swap for fakes in
    tests. All callables raise :class:`Exception` on failure; the
    sweep catches per-source and records in probe_errors_json."""
    db_path: Path
    # Proxmox
    list_pve_vms: Callable[[], list[dict]]
    """Return a list of ``{vmid, name, node, status, tags, ...}`` dicts
    covering every VM in the cluster. Used to decide which VMs the
    sweep touches."""
    fetch_pve_config: Callable[[int, str], dict]
    """``(vmid, node) -> config_dict`` — raw Proxmox API ``qm config``
    response. Keys are strings; values may be strings or dicts."""
    fetch_guest_details: Callable[[int, str], Optional[dict]]
    """``(vmid, node) -> {win_name, serial, uuid, os_build, dsreg}`` or
    ``None`` when the guest agent is down / not running Windows."""
    # LDAP
    ad_search: Callable[[str, str], list[dict]]
    """``(search_base_dn, win_name) -> list of AD entry dicts``. One
    call per (OU, VM). Raise on bind failure or transient errors; the
    sweep catches per-OU."""
    # Graph
    graph_find_entra_device: Callable[[str], list[dict]]
    """``(win_name) -> list of Graph device objects`` (may be empty)."""
    graph_find_intune_device: Callable[[str], list[dict]]
    """``(serial_number) -> list of managedDevice objects``."""
    # Testability
    now: Callable[[], str] = field(
        default_factory=lambda: lambda:
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Pure probe functions
# ---------------------------------------------------------------------------

def _parse_int(val: Any) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


_DISK_KEY_RE = re.compile(r"^(scsi|ide|virtio|sata|nvme)(\d+)$")
_NET_KEY_RE = re.compile(r"^net(\d+)$")


def _parse_proxmox_disk(key: str, spec: str) -> dict:
    """Parse a Proxmox disk config string like
    ``nvmepool:vm-109-disk-1,discard=on,size=64G,serial=APHV000109…``.
    Size is normalised to bytes."""
    m = _DISK_KEY_RE.match(key)
    parts = spec.split(",")
    # The first part is always <storage>:<volid-or-size>.
    volid = parts[0]
    storage = volid.split(":", 1)[0] if ":" in volid else ""
    attrs = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            attrs[k] = v
    size_bytes = None
    size_str = attrs.get("size", "")
    if size_str:
        unit_mul = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
        mult = 1
        num = size_str
        if size_str and size_str[-1] in unit_mul:
            mult = unit_mul[size_str[-1]]
            num = size_str[:-1]
        try:
            size_bytes = int(num) * mult
        except ValueError:
            size_bytes = None
    return {
        "bus": m.group(1) if m else key,
        "index": int(m.group(2)) if m else 0,
        "storage": storage,
        "volid": volid,
        "size_bytes": size_bytes,
        "serial": attrs.get("serial"),
        "media": attrs.get("media"),
        "raw": spec,
    }


def _parse_proxmox_net(key: str, spec: str) -> dict:
    m = _NET_KEY_RE.match(key)
    parts = spec.split(",")
    # First part: ``<model>=<mac>`` or just ``<model>``.
    model_part = parts[0]
    model = model_part.split("=", 1)[0] if "=" in model_part else model_part
    mac = model_part.split("=", 1)[1] if "=" in model_part else None
    attrs = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            attrs[k] = v
    return {
        "index": int(m.group(1)) if m else 0,
        "model": model,
        "mac": mac,
        "bridge": attrs.get("bridge"),
        "firewall": attrs.get("firewall"),
        "vlan": _parse_int(attrs.get("tag")),
        "raw": spec,
    }


def probe_pve(vmid: int, node: str, config: dict, *,
              vm_list_entry: Optional[dict] = None,
              now: Optional[str] = None) -> dict:
    """Normalise a raw Proxmox config dict into the pve_snapshots shape.

    ``config`` is what Proxmox's ``/nodes/<node>/qemu/<vmid>/config``
    endpoint returns. ``vm_list_entry`` supplies fields the config
    call doesn't carry (status, lock mode, tags) — optional, for cases
    where the caller already has them.
    """
    disks = []
    nets = []
    for k, v in sorted(config.items()):
        if not isinstance(v, str):
            continue
        if _DISK_KEY_RE.match(k):
            parsed = _parse_proxmox_disk(k, v)
            # Skip CD-ROM entries from the disks array — they're not
            # persistent storage and would churn on answer-ISO swaps.
            if parsed.get("media") == "cdrom":
                continue
            disks.append(parsed)
        elif _NET_KEY_RE.match(k):
            nets.append(_parse_proxmox_net(k, v))

    tags = sorted((config.get("tags") or "").split(","))
    tags = [t for t in tags if t]  # drop empties
    tags_csv = ",".join(tags)

    # config_digest excludes volatile-but-uninteresting fields so a
    # routine agent-triggered update doesn't churn the digest. Things
    # like QEMU guest agent's boot-time IP reports are not in Proxmox's
    # config so we don't need to filter them, but we do exclude the
    # listing-entry-only fields (status/lock) so a power transition
    # doesn't read as a config change.
    digest_fields = {
        "name": config.get("name"),
        "cores": _parse_int(config.get("cores")),
        "sockets": _parse_int(config.get("sockets")),
        "memory": _parse_int(config.get("memory")),
        "balloon": _parse_int(config.get("balloon")),
        "machine": config.get("machine"),
        "bios": config.get("bios"),
        "smbios1": config.get("smbios1"),
        "args": config.get("args"),
        "vmgenid": config.get("vmgenid"),
        "disks": [{"bus": d["bus"], "index": d["index"],
                   "storage": d["storage"], "size_bytes": d["size_bytes"],
                   "serial": d.get("serial")} for d in disks],
        "nets": [{"index": n["index"], "model": n["model"],
                  "bridge": n["bridge"], "vlan": n.get("vlan")}
                 for n in nets],
        "tags": tags,
    }
    digest = hashlib.sha256(
        json.dumps(digest_fields, sort_keys=True).encode()
    ).hexdigest()

    status = None
    lock_mode = None
    if vm_list_entry:
        status = vm_list_entry.get("status")
        lock_mode = vm_list_entry.get("lock")

    return {
        "checked_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vmid": int(vmid),
        "present": 1,
        "node": node,
        "name": config.get("name"),
        "status": status,
        "tags_csv": tags_csv,
        "lock_mode": lock_mode,
        "cores": _parse_int(config.get("cores")),
        "sockets": _parse_int(config.get("sockets")),
        "memory_mb": _parse_int(config.get("memory")),
        "balloon_mb": _parse_int(config.get("balloon")),
        "machine": config.get("machine"),
        "bios": config.get("bios"),
        "smbios1": config.get("smbios1"),
        "args": config.get("args"),
        "vmgenid": config.get("vmgenid"),
        "disks_json": json.dumps(disks, sort_keys=True),
        "net_json": json.dumps(nets, sort_keys=True),
        "config_digest": digest,
    }


def probe_ad_for_win_name(ctx: MonitorContext, win_name: str,
                          search_ous: list) -> tuple[list[dict], dict]:
    """Run one AD search per enabled OU, union results, tag each match
    with its source OU. Returns ``(matches, errors_by_ou_dn)``.

    Per-OU failures are isolated — a permission denial on one DN
    doesn't block the others. The sweep records the errors dict on
    the probe row.
    """
    matches: list[dict] = []
    errors: dict[str, str] = {}
    for ou in search_ous:
        try:
            entries = ctx.ad_search(ou.dn, win_name)
        except Exception as e:
            errors[ou.dn] = f"{type(e).__name__}: {e}"
            continue
        for entry in entries:
            matches.append({
                **entry,
                "source_ou_dn": ou.dn,
                "source_ou_label": ou.label,
            })
    return matches, errors


def probe_entra_for_win_name(ctx: MonitorContext,
                             win_name: str) -> list[dict]:
    """Return raw Graph device objects matching ``displayName``.

    Empty list is a valid answer — the regression detector decides
    whether that's "not yet synced" (⏳) or "regression" based on AD
    history."""
    return ctx.graph_find_entra_device(win_name)


def probe_intune_for_serial(ctx: MonitorContext,
                            serial: str) -> list[dict]:
    """Return raw managedDevice objects matching ``serialNumber``."""
    return ctx.graph_find_intune_device(serial)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _build_probe_row(vmid: int, vm_name: Optional[str],
                     guest: Optional[dict],
                     ad_matches: list, ad_errors: dict,
                     entra: list, entra_err: Optional[str],
                     intune: list, intune_err: Optional[str],
                     guest_err: Optional[str],
                     now: str) -> dict:
    errors: dict[str, Any] = {}
    if guest_err:
        errors["guest"] = guest_err
    if ad_errors:
        errors["ad_per_ou"] = ad_errors
    if entra_err:
        errors["entra"] = entra_err
    if intune_err:
        errors["intune"] = intune_err
    return {
        "vmid": int(vmid),
        "checked_at": now,
        "vm_name": vm_name,
        "win_name": (guest or {}).get("win_name"),
        "serial": (guest or {}).get("serial"),
        "uuid": (guest or {}).get("uuid"),
        "os_build": (guest or {}).get("os_build"),
        "dsreg_status": json.dumps((guest or {}).get("dsreg") or {}, sort_keys=True),
        "ad_found": 1 if ad_matches else 0,
        "ad_match_count": len(ad_matches),
        "ad_matches_json": json.dumps(ad_matches, sort_keys=True, default=str),
        "entra_found": 1 if entra else 0,
        "entra_match_count": len(entra),
        "entra_matches_json": json.dumps(entra, sort_keys=True, default=str),
        "intune_found": 1 if intune else 0,
        "intune_match_count": len(intune),
        "intune_matches_json": json.dumps(intune, sort_keys=True, default=str),
        "probe_errors_json": json.dumps(errors, sort_keys=True),
    }


def _is_autopilot_vm(entry: dict) -> bool:
    """In-scope == tagged ``autopilot``. (Membership in
    vm_provisioning is added later in the wiring layer.)"""
    tags = entry.get("tags") or ""
    return "autopilot" in [t.strip() for t in tags.split(";") if t.strip()] or \
           "autopilot" in [t.strip() for t in tags.split(",") if t.strip()]


def sweep(ctx: MonitorContext,
          *,
          extra_in_scope_vmids: Optional[set] = None) -> int:
    """Run one full monitoring pass.

    ``extra_in_scope_vmids`` is the set of VMIDs from
    ``vm_provisioning`` — VMs that were autopilot-provisioned even if
    the tag was later removed still show up in the dashboard. Caller
    (the FastAPI wiring layer) builds this from the sequences DB.

    Returns the sweep_id. Never raises: per-VM and per-source errors
    are recorded in probe_errors_json on the probe row, and any
    unrecoverable I/O error (can't list VMs, can't bind LDAP) is
    recorded in monitoring_sweeps.errors_json and the sweep completes
    as a no-op.
    """
    extra = extra_in_scope_vmids or set()
    sweep_id = device_history_db.start_sweep(ctx.db_path)
    errors: dict[str, Any] = {}
    vm_count = 0

    try:
        vms = ctx.list_pve_vms()
    except Exception as e:
        log.exception("sweep: list_pve_vms failed")
        errors["pve_list"] = f"{type(e).__name__}: {e}"
        device_history_db.finish_sweep(
            ctx.db_path, sweep_id, vm_count=0, errors=errors,
        )
        return sweep_id

    search_ous = device_history_db.list_enabled_search_ous(ctx.db_path)
    if not search_ous:
        # The DAL guarantees at least one enabled OU; this path only
        # triggers in tests that seed the DB by hand.
        errors["search_ous"] = "no enabled search OUs configured"

    in_scope = [v for v in vms
                if _is_autopilot_vm(v) or int(v.get("vmid", 0)) in extra]

    for vm in in_scope:
        vmid = int(vm["vmid"])
        node = vm.get("node") or ""
        vm_name = vm.get("name")

        # ---- PVE snapshot ----
        snap: Optional[dict] = None
        try:
            config = ctx.fetch_pve_config(vmid, node)
            snap = probe_pve(vmid, node, config, vm_list_entry=vm,
                             now=ctx.now())
            device_history_db.insert_pve_snapshot(ctx.db_path, sweep_id, snap)
        except Exception as e:
            log.exception("sweep vm=%s: pve config failed", vmid)
            # Still record a row so the timeline shows the probe tried.
            device_history_db.insert_pve_snapshot(ctx.db_path, sweep_id, {
                "checked_at": ctx.now(), "vmid": vmid,
                "present": 1, "node": node, "name": vm_name,
                "status": vm.get("status"),
                "config_digest": "",
                "probe_error": f"{type(e).__name__}: {e}",
            })

        # ---- Directory probes ----
        guest: Optional[dict] = None
        guest_err: Optional[str] = None
        if vm.get("status") == "running":
            try:
                guest = ctx.fetch_guest_details(vmid, node)
            except Exception as e:
                guest_err = f"{type(e).__name__}: {e}"
        else:
            guest_err = f"vm-not-running ({vm.get('status')})"

        ad_matches: list = []
        ad_errors: dict = {}
        entra: list = []
        entra_err: Optional[str] = None
        intune: list = []
        intune_err: Optional[str] = None

        win_name = (guest or {}).get("win_name") or vm_name or ""
        serial = (guest or {}).get("serial") or ""

        if win_name and search_ous:
            ad_matches, ad_errors = probe_ad_for_win_name(
                ctx, win_name, search_ous,
            )
            try:
                entra = probe_entra_for_win_name(ctx, win_name)
            except Exception as e:
                entra_err = f"{type(e).__name__}: {e}"
        if serial:
            try:
                intune = probe_intune_for_serial(ctx, serial)
            except Exception as e:
                intune_err = f"{type(e).__name__}: {e}"

        probe_row = _build_probe_row(
            vmid, vm_name, guest,
            ad_matches, ad_errors,
            entra, entra_err,
            intune, intune_err,
            guest_err,
            ctx.now(),
        )
        device_history_db.insert_device_probe(
            ctx.db_path, sweep_id, probe_row,
        )
        vm_count += 1

    device_history_db.finish_sweep(
        ctx.db_path, sweep_id, vm_count=vm_count, errors=errors,
    )
    return sweep_id


# ---------------------------------------------------------------------------
# Live I/O backends (wired in step 3)
# ---------------------------------------------------------------------------

_GRAPH_TOKEN_CACHE: dict = {"token": "", "expires_at": 0.0}


def _graph_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Return a cached Graph token, refreshing when <5 min from expiry."""
    import requests
    now = time.time()
    if _GRAPH_TOKEN_CACHE["token"] and _GRAPH_TOKEN_CACHE["expires_at"] - now > 300:
        return _GRAPH_TOKEN_CACHE["token"]
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    _GRAPH_TOKEN_CACHE["token"] = body["access_token"]
    _GRAPH_TOKEN_CACHE["expires_at"] = now + int(body.get("expires_in", 3600))
    return _GRAPH_TOKEN_CACHE["token"]


def _graph_get(url: str, *, tenant_id: str, client_id: str,
               client_secret: str) -> dict:
    import requests
    token = _graph_token(tenant_id, client_id, client_secret)
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _quote_graph_string(value: str) -> str:
    """Escape a single-quoted string inside an OData $filter. Single
    quotes are doubled per the OData 4.0 spec."""
    return value.replace("'", "''")
