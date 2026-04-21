"""Tests for web.monitoring_view — badge classification + dashboard assembly."""
import json

import pytest


def _probe(ad=0, ad_matches=None, entra=0, entra_matches=None,
           intune=0, intune_matches=None, errors=None):
    return {
        "ad_found": ad, "ad_match_count": len(ad_matches or []),
        "ad_matches_json": json.dumps(ad_matches or []),
        "entra_found": entra, "entra_match_count": len(entra_matches or []),
        "entra_matches_json": json.dumps(entra_matches or []),
        "intune_found": intune, "intune_match_count": len(intune_matches or []),
        "intune_matches_json": json.dumps(intune_matches or []),
        "probe_errors_json": json.dumps(errors or {}),
    }


def test_classify_ad_ok_for_single_active_match():
    from web.monitoring_view import classify_ad, BADGE_OK
    assert classify_ad(_probe(ad=1, ad_matches=[
        {"distinguishedName": "CN=A", "userAccountControl": 4096},
    ])) == BADGE_OK


def test_classify_ad_warn_on_disabled_match():
    from web.monitoring_view import classify_ad, BADGE_WARN
    # UAC bit 0x2 set = ACCOUNTDISABLE.
    assert classify_ad(_probe(ad=1, ad_matches=[
        {"distinguishedName": "CN=A", "userAccountControl": 4098},
    ])) == BADGE_WARN


def test_classify_ad_warn_on_duplicate_matches():
    from web.monitoring_view import classify_ad, BADGE_WARN
    assert classify_ad(_probe(ad=1, ad_matches=[
        {"distinguishedName": "CN=A,OU=Devices", "userAccountControl": 4096},
        {"distinguishedName": "CN=A,OU=OldDevices", "userAccountControl": 4098},
    ])) == BADGE_WARN


def test_classify_ad_missing_when_no_matches():
    from web.monitoring_view import classify_ad, BADGE_MISSING
    assert classify_ad(_probe()) == BADGE_MISSING


def test_classify_ad_warn_on_per_ou_errors():
    from web.monitoring_view import classify_ad, BADGE_WARN
    assert classify_ad(_probe(
        ad=1, ad_matches=[{"distinguishedName": "CN=A", "userAccountControl": 4096}],
        errors={"ad_per_ou": {"OU=broken,DC=x": "PermissionError"}},
    )) == BADGE_WARN


def test_classify_ad_unknown_for_missing_probe():
    from web.monitoring_view import classify_ad, BADGE_UNKNOWN
    assert classify_ad(None) == BADGE_UNKNOWN


def test_classify_entra_pending_within_sync_window():
    """No Entra match + AD first seen 10 min ago → pending (⏳), not missing."""
    from web.monitoring_view import classify_entra, BADGE_PENDING
    assert classify_entra(
        _probe(),
        ad_first_seen_at="2026-04-20T23:00:00+00:00",
        now_iso="2026-04-20T23:10:00+00:00",
    ) == BADGE_PENDING


def test_classify_entra_missing_after_sync_window():
    from web.monitoring_view import classify_entra, BADGE_MISSING
    assert classify_entra(
        _probe(),
        ad_first_seen_at="2026-04-20T23:00:00+00:00",
        now_iso="2026-04-21T01:00:00+00:00",
    ) == BADGE_MISSING


def test_classify_entra_missing_without_any_ad_history():
    from web.monitoring_view import classify_entra, BADGE_MISSING
    assert classify_entra(_probe()) == BADGE_MISSING


def test_classify_entra_ok_when_single_match():
    from web.monitoring_view import classify_entra, BADGE_OK
    assert classify_entra(_probe(
        entra=1,
        entra_matches=[{"displayName": "X", "trustType": "ServerAd"}],
    )) == BADGE_OK


def test_classify_entra_warn_on_duplicates():
    from web.monitoring_view import classify_entra, BADGE_WARN
    assert classify_entra(_probe(
        entra=1,
        entra_matches=[
            {"displayName": "X", "trustType": "ServerAd"},
            {"displayName": "X", "trustType": "AzureAd"},
        ],
    )) == BADGE_WARN


def test_classify_intune_warn_on_noncompliant():
    from web.monitoring_view import classify_intune, BADGE_WARN
    assert classify_intune(_probe(
        intune=1,
        intune_matches=[{"complianceState": "noncompliant"}],
    )) == BADGE_WARN


def test_classify_intune_ok_on_compliant():
    from web.monitoring_view import classify_intune, BADGE_OK
    assert classify_intune(_probe(
        intune=1,
        intune_matches=[{"complianceState": "compliant"}],
    )) == BADGE_OK


# ---------------------------------------------------------------------------
# Dashboard assembly
# ---------------------------------------------------------------------------


def test_build_dashboard_rows_end_to_end():
    from web.monitoring_view import build_dashboard_rows
    latest = [
        {
            "vmid": 116, "last_checked": "2026-04-20T23:55:00+00:00",
            "pve": {"name": "Gell-EC41E7EB", "node": "pve2", "status": "running"},
            "probe": _probe(
                ad=1, ad_matches=[{"distinguishedName": "CN=GELL-EC41E7EB",
                                    "userAccountControl": 4096}],
                entra=1, entra_matches=[{"trustType": "ServerAd"}],
                intune=1, intune_matches=[{"complianceState": "compliant"}],
            ) | {"win_name": "GELL-EC41E7EB", "serial": "Gell-EC41E7EB"},
        },
    ]
    rows = build_dashboard_rows(
        latest, ad_first_seen={116: "2026-04-20T23:50:00+00:00"},
        now_iso="2026-04-20T23:55:00+00:00",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.vmid == 116
    assert r.pve_status == "running"
    assert r.ad_badge == "ok"
    assert r.entra_badge == "ok"
    assert r.entra_trust_type == "ServerAd"
    assert r.intune_badge == "ok"
    assert r.intune_compliance == "compliant"


def test_build_dashboard_rows_surfaces_pending_entra():
    """VM that hit AD 10min ago but hasn't shown in Entra → ⏳."""
    from web.monitoring_view import build_dashboard_rows
    latest = [{
        "vmid": 117, "last_checked": "2026-04-20T23:10:00+00:00",
        "pve": {"name": "Gell-NEW", "node": "pve2", "status": "running"},
        "probe": _probe(
            ad=1, ad_matches=[{"distinguishedName": "CN=GELL-NEW",
                               "userAccountControl": 4096}],
        ) | {"win_name": "GELL-NEW", "serial": "Gell-NEW"},
    }]
    rows = build_dashboard_rows(
        latest, ad_first_seen={117: "2026-04-20T23:00:00+00:00"},
        now_iso="2026-04-20T23:10:00+00:00",
    )
    assert rows[0].entra_badge == "pending"
