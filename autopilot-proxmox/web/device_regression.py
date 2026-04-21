"""Regression / progression detector for the per-device timeline.

Pure functions over two input streams (pve_snapshots and
device_probes, both oldest-first for a single VMID) — emit a merged
chronological list of events. The detail-page template renders each
event with a coloured badge; JSON blobs are passed through for the
drill-down drawer.

See ``docs/specs/2026-04-20-device-state-monitoring-design.md``
section "Regression detection" for the transition matrix.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional


# Event severities drive the UI colour. `regression` is red,
# `rename` / `ou-move` / `sync-pending` are yellow, `progression`
# is green, `event` is neutral.
SEV_REGRESSION = "regression"
SEV_PROGRESSION = "progression"
SEV_RENAME = "rename"
SEV_OU_MOVE = "ou-move"
SEV_REPLACEMENT = "replacement"
SEV_LINK_BROKEN = "link-broken"
SEV_SYNC_PENDING = "sync-pending"
SEV_EVENT = "event"


@dataclass
class Event:
    at: str                # ISO8601 UTC
    source: str            # pve / ad / entra / intune / provision
    type: str              # e.g. "power-on", "rename", "object-created"
    severity: str
    summary: str           # human-readable single-line description
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _j(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _first_ad_match_by_guid(matches: list[dict]) -> dict[str, dict]:
    """Index AD matches by objectGUID. When the same GUID appears in
    multiple OUs (overlapping search bases), the first wins — it
    doesn't matter for identity tracking which one."""
    out: dict[str, dict] = {}
    for m in matches:
        guid = m.get("objectGUID") or ""
        if guid and guid not in out:
            out[guid] = m
    return out


def _parent_ou(dn: str) -> str:
    """Return everything after the first comma, so
    'CN=X,OU=A,OU=B,DC=…' → 'OU=A,OU=B,DC=…'. Caller handles
    case-insensitive comparison."""
    if "," in dn:
        return dn.split(",", 1)[1]
    return ""


def _minutes(a: str, b: str) -> float:
    try:
        return abs(
            (datetime.fromisoformat(b) - datetime.fromisoformat(a))
            .total_seconds()
        ) / 60.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# PVE transitions
# ---------------------------------------------------------------------------


def pve_transitions(pve_rows: Iterable[dict]) -> list[Event]:
    """Walk PVE snapshots oldest→newest, emit events for each state
    change. Accepts iterable of dicts (from history_for_vmid)."""
    events: list[Event] = []
    prev: Optional[dict] = None
    for row in pve_rows:
        if prev is None:
            events.append(Event(
                at=row["checked_at"],
                source="pve", type="first-observed",
                severity=SEV_EVENT,
                summary=f"VM {row.get('vmid')} first observed on "
                        f"{row.get('node') or '?'}: status={row.get('status')}",
                details={"snapshot": row},
            ))
            prev = row
            continue
        if row.get("present") == 0 and prev.get("present", 1) == 1:
            events.append(Event(
                at=row["checked_at"], source="pve", type="deleted",
                severity=SEV_REGRESSION,
                summary="VM removed from Proxmox",
                details={"prev_node": prev.get("node")},
            ))
            prev = row
            continue
        # Power transitions.
        if prev.get("status") != row.get("status"):
            if row.get("status") == "running" and prev.get("status") == "stopped":
                etype = "power-on"
            elif row.get("status") == "stopped" and prev.get("status") == "running":
                etype = "power-off"
            else:
                etype = f"status-{prev.get('status')}-to-{row.get('status')}"
            events.append(Event(
                at=row["checked_at"], source="pve", type=etype,
                severity=SEV_EVENT,
                summary=f"status {prev.get('status')} → {row.get('status')}",
                details={"from": prev.get("status"), "to": row.get("status")},
            ))
        # Node migration.
        if prev.get("node") and row.get("node") and prev.get("node") != row.get("node"):
            events.append(Event(
                at=row["checked_at"], source="pve", type="migration",
                severity=SEV_EVENT,
                summary=f"migrated {prev.get('node')} → {row.get('node')}",
                details={"from_node": prev.get("node"),
                         "to_node": row.get("node")},
            ))
        # Config change.
        if prev.get("config_digest") and row.get("config_digest") \
                and prev["config_digest"] != row["config_digest"]:
            # Surface args separately if that's what moved — SMBIOS
            # swaps are interesting enough to call out.
            if prev.get("args") != row.get("args"):
                events.append(Event(
                    at=row["checked_at"], source="pve",
                    type="smbios-reconfig", severity=SEV_EVENT,
                    summary="QEMU args changed",
                    details={"from": prev.get("args"), "to": row.get("args")},
                ))
            else:
                events.append(Event(
                    at=row["checked_at"], source="pve",
                    type="config-changed", severity=SEV_EVENT,
                    summary="Proxmox config changed",
                    details={"digest_from": prev["config_digest"],
                             "digest_to": row["config_digest"]},
                ))
        prev = row
    return events


# ---------------------------------------------------------------------------
# Directory (AD / Entra / Intune) transitions
# ---------------------------------------------------------------------------


def ad_transitions(probe_rows: Iterable[dict]) -> list[Event]:
    """Compare consecutive probes' AD matches. Uses objectGUID as the
    stable identity so renames + OU moves don't read as
    delete+create."""
    events: list[Event] = []
    prev: Optional[dict] = None
    prev_by_guid: dict[str, dict] = {}
    for row in probe_rows:
        matches = _j(row.get("ad_matches_json"), [])
        current_by_guid = _first_ad_match_by_guid(matches)
        if prev is None:
            # First observation — progression if found, otherwise no
            # event (we haven't seen it before so there's no baseline
            # to regress from).
            if current_by_guid:
                events.append(Event(
                    at=row["checked_at"], source="ad",
                    type="object-created", severity=SEV_PROGRESSION,
                    summary=f"AD object appeared: "
                            f"{_short_guid(next(iter(current_by_guid)))}",
                    details={"matches": matches},
                ))
            prev = row
            prev_by_guid = current_by_guid
            continue
        # Compare GUIDs, not DN — a rename keeps the GUID.
        gone = set(prev_by_guid) - set(current_by_guid)
        new = set(current_by_guid) - set(prev_by_guid)
        same = set(prev_by_guid) & set(current_by_guid)
        for guid in same:
            prev_m = prev_by_guid[guid]
            now_m = current_by_guid[guid]
            # Rename: cn or sAMAccountName changed.
            if (prev_m.get("cn") != now_m.get("cn")) or \
               (prev_m.get("sAMAccountName") != now_m.get("sAMAccountName")):
                events.append(Event(
                    at=row["checked_at"], source="ad", type="renamed",
                    severity=SEV_RENAME,
                    summary=f"cn: {prev_m.get('cn')} → {now_m.get('cn')}",
                    details={"objectGUID": guid,
                             "from_cn": prev_m.get("cn"),
                             "to_cn": now_m.get("cn"),
                             "from_sam": prev_m.get("sAMAccountName"),
                             "to_sam": now_m.get("sAMAccountName")},
                ))
            # OU move: parent container changed.
            prev_parent = _parent_ou(prev_m.get("distinguishedName") or "")
            now_parent = _parent_ou(now_m.get("distinguishedName") or "")
            if prev_parent.lower() != now_parent.lower() and prev_parent and now_parent:
                events.append(Event(
                    at=row["checked_at"], source="ad", type="ou-move",
                    severity=SEV_OU_MOVE,
                    summary=f"moved OU: {prev_parent} → {now_parent}",
                    details={"objectGUID": guid,
                             "from_dn": prev_m.get("distinguishedName"),
                             "to_dn": now_m.get("distinguishedName")},
                ))
            # Enable/disable flip (UAC bit 0x2).
            pu = int(prev_m.get("userAccountControl") or 0)
            cu = int(now_m.get("userAccountControl") or 0)
            if (pu & 0x2) == 0 and (cu & 0x2) != 0:
                events.append(Event(
                    at=row["checked_at"], source="ad", type="disabled",
                    severity=SEV_REGRESSION,
                    summary="AD object disabled",
                    details={"objectGUID": guid},
                ))
            elif (pu & 0x2) != 0 and (cu & 0x2) == 0:
                events.append(Event(
                    at=row["checked_at"], source="ad", type="re-enabled",
                    severity=SEV_PROGRESSION,
                    summary="AD object re-enabled",
                    details={"objectGUID": guid},
                ))
        for guid in new:
            events.append(Event(
                at=row["checked_at"], source="ad",
                type="object-created", severity=SEV_PROGRESSION,
                summary=f"new AD object: {_short_guid(guid)}",
                details={"objectGUID": guid,
                         "match": current_by_guid[guid]},
            ))
        for guid in gone:
            # An object disappearing while others remain isn't always
            # a regression — but if the VM's ONLY AD object disappeared
            # we flag it red.
            severity = SEV_REGRESSION if not current_by_guid else SEV_EVENT
            events.append(Event(
                at=row["checked_at"], source="ad",
                type="object-removed", severity=severity,
                summary=f"AD object gone: {_short_guid(guid)}",
                details={"objectGUID": guid,
                         "prev_match": prev_by_guid[guid]},
            ))
        prev = row
        prev_by_guid = current_by_guid
    return events


def entra_transitions(probe_rows: Iterable[dict],
                      *, sync_window_minutes: int = 45) -> list[Event]:
    """Consecutive-probe diff for Entra. Includes the hybrid-link check
    (AD.objectSid vs Entra.onPremisesSecurityIdentifier)."""
    events: list[Event] = []
    prev: Optional[dict] = None
    ad_first_seen: Optional[str] = None
    emitted_sync_pending = False
    for row in probe_rows:
        matches = _j(row.get("entra_matches_json"), [])
        ad_matches = _j(row.get("ad_matches_json"), [])
        if ad_first_seen is None and ad_matches:
            ad_first_seen = row["checked_at"]

        if prev is None:
            prev = row
            continue
        prev_entra = _j(prev.get("entra_matches_json"), [])
        prev_ids = {m.get("id") for m in prev_entra if m.get("id")}
        now_ids = {m.get("id") for m in matches if m.get("id")}

        # ServerAd trust type appearing for the first time.
        prev_server = any(m.get("trustType") == "ServerAd" for m in prev_entra)
        now_server = any(m.get("trustType") == "ServerAd" for m in matches)
        if not prev_server and now_server:
            events.append(Event(
                at=row["checked_at"], source="entra",
                type="hybrid-synced", severity=SEV_PROGRESSION,
                summary="Entra hybrid device appeared (trustType=ServerAd)",
                details={"matches": matches},
            ))
        if prev_server and not now_server:
            events.append(Event(
                at=row["checked_at"], source="entra",
                type="hybrid-lost", severity=SEV_REGRESSION,
                summary="Hybrid Entra device gone",
                details={"prev_matches": prev_entra},
            ))
        # Replacement: same displayName but different id.
        replaced = (prev_ids and now_ids and not (prev_ids & now_ids))
        if replaced:
            events.append(Event(
                at=row["checked_at"], source="entra",
                type="replacement", severity=SEV_REPLACEMENT,
                summary="Entra deviceId changed — object was replaced",
                details={"from_ids": sorted(prev_ids),
                         "to_ids": sorted(now_ids)},
            ))

        # Sync-pending: AD just showed up, Entra hasn't followed.
        if (not emitted_sync_pending and not matches and ad_first_seen
                and _minutes(ad_first_seen, row["checked_at"]) < sync_window_minutes):
            events.append(Event(
                at=row["checked_at"], source="entra",
                type="sync-pending", severity=SEV_SYNC_PENDING,
                summary=f"Entra not yet synced (AD seen "
                        f"{_minutes(ad_first_seen, row['checked_at']):.0f}m ago)",
                details={"ad_first_seen_at": ad_first_seen},
            ))
            emitted_sync_pending = True

        # Hybrid linkage: ServerAd entries must have
        # onPremisesSecurityIdentifier matching AD.objectSid.
        ad_sids = {
            m.get("objectSid") for m in ad_matches
            if m.get("objectSid")
        }
        for em in matches:
            if em.get("trustType") != "ServerAd":
                continue
            sid = em.get("onPremisesSecurityIdentifier")
            if not sid:
                # Valid for a brief window right after hybrid join —
                # the sync completes in two phases. Treat as event, not
                # regression.
                continue
            if ad_sids and sid not in ad_sids:
                events.append(Event(
                    at=row["checked_at"], source="entra",
                    type="link-broken", severity=SEV_LINK_BROKEN,
                    summary="Entra.onPremisesSID ≠ AD.objectSid "
                            "(stale Entra device)",
                    details={"entra_id": em.get("id"),
                             "entra_sid": sid,
                             "ad_sids": sorted(ad_sids)},
                ))
        prev = row
    return events


def intune_transitions(probe_rows: Iterable[dict]) -> list[Event]:
    events: list[Event] = []
    prev: Optional[dict] = None
    for row in probe_rows:
        matches = _j(row.get("intune_matches_json"), [])
        if prev is None:
            if matches:
                events.append(Event(
                    at=row["checked_at"], source="intune",
                    type="enrolled", severity=SEV_PROGRESSION,
                    summary="Intune enrollment appeared "
                            f"({matches[0].get('complianceState')})",
                    details={"matches": matches},
                ))
            prev = row
            continue
        prev_matches = _j(prev.get("intune_matches_json"), [])
        if prev_matches and not matches:
            events.append(Event(
                at=row["checked_at"], source="intune",
                type="unenrolled", severity=SEV_REGRESSION,
                summary="Intune device gone",
                details={"prev_matches": prev_matches},
            ))
        elif not prev_matches and matches:
            events.append(Event(
                at=row["checked_at"], source="intune",
                type="enrolled", severity=SEV_PROGRESSION,
                summary="Intune enrollment appeared "
                        f"({matches[0].get('complianceState')})",
                details={"matches": matches},
            ))
        else:
            # Compliance flip.
            p_state = (prev_matches[0].get("complianceState")
                       if prev_matches else None)
            c_state = (matches[0].get("complianceState")
                       if matches else None)
            if p_state and c_state and p_state != c_state:
                sev = (SEV_REGRESSION
                       if c_state == "noncompliant" else SEV_PROGRESSION)
                events.append(Event(
                    at=row["checked_at"], source="intune",
                    type="compliance-changed", severity=sev,
                    summary=f"compliance: {p_state} → {c_state}",
                    details={"from": p_state, "to": c_state},
                ))
        prev = row
    return events


def _short_guid(g: str) -> str:
    return (g[:8] + "…") if g and len(g) > 8 else (g or "")


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def build_timeline(pve_rows_oldest_first: list[dict],
                   probe_rows_oldest_first: list[dict]) -> list[Event]:
    """Combine every transition source into one chronological list,
    newest first (ready for the template)."""
    events: list[Event] = []
    events.extend(pve_transitions(pve_rows_oldest_first))
    events.extend(ad_transitions(probe_rows_oldest_first))
    events.extend(entra_transitions(probe_rows_oldest_first))
    events.extend(intune_transitions(probe_rows_oldest_first))
    events.sort(key=lambda e: e.at, reverse=True)
    return events
