"""View-layer helpers for the /monitoring dashboard and device pages.

Classifies probe rows into status badges for rendering. Kept out of
the main app module so template rendering is trivially testable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


# Badge values map 1:1 to CSS classes + icons in the template.
BADGE_OK = "ok"          # ✅
BADGE_WARN = "warn"      # ⚠️
BADGE_MISSING = "missing"  # ❌
BADGE_PENDING = "pending"  # ⏳ (e.g., Entra hasn't synced the AD object yet)
BADGE_ERROR = "error"    # 🔴 probe itself errored
BADGE_UNKNOWN = "unknown"  # greyed-out when we haven't probed


def _parse_json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _ad_has_active_match(matches: list[dict]) -> bool:
    """Is at least one AD match enabled (no ACCOUNTDISABLE bit)?"""
    for m in matches:
        uac = m.get("userAccountControl")
        try:
            uac_int = int(uac) if uac is not None else 0
        except (TypeError, ValueError):
            uac_int = 0
        # Bit 0x2 = ACCOUNTDISABLE per [MS-ADTS]. If it's clear, the
        # account is active and usable.
        if not (uac_int & 0x2):
            return True
    return False


def classify_ad(probe_row: Optional[dict]) -> str:
    """Return a badge for the AD column of the dashboard."""
    if probe_row is None:
        return BADGE_UNKNOWN
    errs = _parse_json(probe_row.get("probe_errors_json"), {})
    if errs.get("ad_per_ou"):
        # Every configured OU errored? → red. Some subset? → warn.
        # We don't have the full enabled-OU list on the probe row so
        # presence of any ad error counts as warn — the detail page
        # has the per-OU breakdown.
        return BADGE_WARN
    count = int(probe_row.get("ad_match_count") or 0)
    if count == 0:
        return BADGE_MISSING
    matches = _parse_json(probe_row.get("ad_matches_json"), [])
    if count > 1:
        # Duplicates are expected after hybrid re-enrollment; warn, not
        # error.
        return BADGE_WARN
    return BADGE_OK if _ad_has_active_match(matches) else BADGE_WARN


def classify_entra(probe_row: Optional[dict],
                    *, ad_first_seen_at: Optional[str] = None,
                    now_iso: Optional[str] = None,
                    sync_window_minutes: int = 45) -> str:
    """Return a badge for the Entra column.

    When AD recently saw the object but Entra hasn't synced it yet
    (within ``sync_window_minutes``), return :data:`BADGE_PENDING`
    instead of :data:`BADGE_MISSING`. The caller passes
    ``ad_first_seen_at`` from the device_probes history (oldest row
    with ad_found=1 for the vmid)."""
    if probe_row is None:
        return BADGE_UNKNOWN
    errs = _parse_json(probe_row.get("probe_errors_json"), {})
    if errs.get("entra"):
        return BADGE_ERROR
    count = int(probe_row.get("entra_match_count") or 0)
    if count == 0:
        if ad_first_seen_at and now_iso:
            if _minutes_between(ad_first_seen_at, now_iso) < sync_window_minutes:
                return BADGE_PENDING
        return BADGE_MISSING
    if count > 1:
        return BADGE_WARN  # duplicates normal but surfaced
    return BADGE_OK


def classify_intune(probe_row: Optional[dict]) -> str:
    if probe_row is None:
        return BADGE_UNKNOWN
    errs = _parse_json(probe_row.get("probe_errors_json"), {})
    if errs.get("intune"):
        return BADGE_ERROR
    count = int(probe_row.get("intune_match_count") or 0)
    if count == 0:
        return BADGE_MISSING
    if count > 1:
        return BADGE_WARN
    matches = _parse_json(probe_row.get("intune_matches_json"), [])
    if matches and matches[0].get("complianceState") == "noncompliant":
        return BADGE_WARN
    return BADGE_OK


def _minutes_between(iso_a: str, iso_b: str) -> float:
    from datetime import datetime
    try:
        a = datetime.fromisoformat(iso_a)
        b = datetime.fromisoformat(iso_b)
    except ValueError:
        return 0.0
    return abs((b - a).total_seconds()) / 60.0


# ---------------------------------------------------------------------------
# Dashboard row assembly
# ---------------------------------------------------------------------------


@dataclass
class DashboardRow:
    vmid: int
    vm_name: str
    node: str
    pve_status: str
    last_checked: str
    win_name: str
    serial: str
    ad_badge: str
    ad_count: int
    entra_badge: str
    entra_count: int
    entra_trust_type: str
    intune_badge: str
    intune_count: int
    intune_compliance: str


def build_dashboard_rows(latest: list[dict],
                         *, ad_first_seen: dict,
                         now_iso: str) -> list[DashboardRow]:
    """Translate the output of ``device_history_db.latest_per_vmid``
    into dashboard rows with badges assigned.

    ``ad_first_seen`` is a ``{vmid: iso_timestamp}`` dict from the
    caller — used to apply the sync-pending window on the Entra
    badge."""
    out = []
    for entry in latest:
        pve = entry.get("pve") or {}
        probe = entry.get("probe")
        entra_matches = _parse_json(
            (probe or {}).get("entra_matches_json"), [],
        )
        intune_matches = _parse_json(
            (probe or {}).get("intune_matches_json"), [],
        )
        out.append(DashboardRow(
            vmid=int(entry["vmid"]),
            vm_name=pve.get("name") or "",
            node=pve.get("node") or "",
            pve_status=pve.get("status") or "",
            last_checked=entry.get("last_checked") or "",
            win_name=(probe or {}).get("win_name") or "",
            serial=(probe or {}).get("serial") or "",
            ad_badge=classify_ad(probe),
            ad_count=int((probe or {}).get("ad_match_count") or 0),
            entra_badge=classify_entra(
                probe,
                ad_first_seen_at=ad_first_seen.get(int(entry["vmid"])),
                now_iso=now_iso,
            ),
            entra_count=int((probe or {}).get("entra_match_count") or 0),
            entra_trust_type=(entra_matches[0].get("trustType")
                              if entra_matches else ""),
            intune_badge=classify_intune(probe),
            intune_count=int((probe or {}).get("intune_match_count") or 0),
            intune_compliance=(intune_matches[0].get("complianceState")
                               if intune_matches else ""),
        ))
    return out
