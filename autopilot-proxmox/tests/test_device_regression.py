"""Tests for web.device_regression — PVE / AD / Entra / Intune
transition detection. Pure-data inputs, no DB."""
import json

import pytest


def _probe(at, *, ad=None, entra=None, intune=None):
    return {
        "checked_at": at,
        "ad_matches_json": json.dumps(ad or []),
        "entra_matches_json": json.dumps(entra or []),
        "intune_matches_json": json.dumps(intune or []),
    }


def _pve(at, **kw):
    return {"checked_at": at, **kw}


# ---------------------------------------------------------------------------
# PVE
# ---------------------------------------------------------------------------


def test_pve_power_transitions():
    from web.device_regression import pve_transitions
    events = pve_transitions([
        _pve("t1", vmid=1, status="stopped", node="pve2", config_digest="a"),
        _pve("t2", vmid=1, status="running", node="pve2", config_digest="a"),
        _pve("t3", vmid=1, status="stopped", node="pve2", config_digest="a"),
    ])
    types = [e.type for e in events]
    assert "first-observed" in types
    assert "power-on" in types
    assert "power-off" in types


def test_pve_migration_and_smbios_reconfig():
    from web.device_regression import pve_transitions
    events = pve_transitions([
        _pve("t1", vmid=1, status="running", node="pve1",
             config_digest="a", args="-smbios file=/a.bin"),
        _pve("t2", vmid=1, status="running", node="pve2",
             config_digest="b", args="-smbios file=/b.bin"),
    ])
    types = [e.type for e in events]
    assert "migration" in types
    assert "smbios-reconfig" in types


def test_pve_deletion_is_regression():
    from web.device_regression import pve_transitions, SEV_REGRESSION
    events = pve_transitions([
        _pve("t1", vmid=1, status="running", present=1, config_digest="a"),
        _pve("t2", vmid=1, status="stopped", present=0, config_digest="a"),
    ])
    deleted = [e for e in events if e.type == "deleted"]
    assert deleted
    assert deleted[0].severity == SEV_REGRESSION


# ---------------------------------------------------------------------------
# AD
# ---------------------------------------------------------------------------


def test_ad_rename_keeps_object_identity():
    """Renaming in AD keeps objectGUID — must emit 'renamed', not
    'object-removed + object-created'."""
    from web.device_regression import ad_transitions
    probes = [
        _probe("t1", ad=[{"objectGUID": "G1", "cn": "GELL-X",
                           "sAMAccountName": "GELL-X$",
                           "distinguishedName": "CN=GELL-X,OU=Devices,OU=WorkspaceLabs,DC=h,DC=g,DC=o",
                           "userAccountControl": 4096}]),
        _probe("t2", ad=[{"objectGUID": "G1", "cn": "gell-x",
                           "sAMAccountName": "gell-x$",
                           "distinguishedName": "CN=gell-x,OU=Devices,OU=WorkspaceLabs,DC=h,DC=g,DC=o",
                           "userAccountControl": 4096}]),
    ]
    events = ad_transitions(probes)
    types = [e.type for e in events]
    assert "renamed" in types
    assert "object-removed" not in types
    assert "object-created" in types  # the first one on t1


def test_ad_ou_move_emits_ou_move():
    from web.device_regression import ad_transitions
    probes = [
        _probe("t1", ad=[{"objectGUID": "G1", "cn": "X",
                           "distinguishedName": "CN=X,OU=Old,OU=WorkspaceLabs,DC=h,DC=g,DC=o",
                           "userAccountControl": 4096}]),
        _probe("t2", ad=[{"objectGUID": "G1", "cn": "X",
                           "distinguishedName": "CN=X,OU=Devices,OU=WorkspaceLabs,DC=h,DC=g,DC=o",
                           "userAccountControl": 4096}]),
    ]
    events = ad_transitions(probes)
    moves = [e for e in events if e.type == "ou-move"]
    assert moves
    assert "OU=Devices" in moves[0].summary


def test_ad_replacement_emits_removed_plus_created():
    """When objectGUID changes entirely, that's an AD object
    replacement — fire 'object-removed' on the old + 'object-created'
    on the new."""
    from web.device_regression import ad_transitions
    probes = [
        _probe("t1", ad=[{"objectGUID": "G1", "cn": "X",
                          "distinguishedName": "CN=X,OU=A,DC=h,DC=g,DC=o",
                          "userAccountControl": 4096}]),
        _probe("t2", ad=[{"objectGUID": "G2", "cn": "X",
                          "distinguishedName": "CN=X,OU=A,DC=h,DC=g,DC=o",
                          "userAccountControl": 4096}]),
    ]
    events = ad_transitions(probes)
    types = [e.type for e in events]
    assert "object-removed" in types
    assert "object-created" in types


def test_ad_disable_is_regression():
    from web.device_regression import ad_transitions, SEV_REGRESSION
    probes = [
        _probe("t1", ad=[{"objectGUID": "G1", "cn": "X",
                          "distinguishedName": "CN=X,OU=A,DC=h,DC=g,DC=o",
                          "userAccountControl": 4096}]),
        _probe("t2", ad=[{"objectGUID": "G1", "cn": "X",
                          "distinguishedName": "CN=X,OU=A,DC=h,DC=g,DC=o",
                          "userAccountControl": 4098}]),  # 0x2 set
    ]
    events = ad_transitions(probes)
    disabled = [e for e in events if e.type == "disabled"]
    assert disabled and disabled[0].severity == SEV_REGRESSION


# ---------------------------------------------------------------------------
# Entra
# ---------------------------------------------------------------------------


def test_entra_sync_pending_when_ad_seen_but_entra_empty():
    from web.device_regression import entra_transitions, SEV_SYNC_PENDING
    probes = [
        _probe("2026-04-20T10:00:00+00:00",
               ad=[{"objectGUID": "G1", "objectSid": "S-1-5-21-1-4601",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}]),
        _probe("2026-04-20T10:10:00+00:00",
               ad=[{"objectGUID": "G1", "objectSid": "S-1-5-21-1-4601",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}]),
    ]
    events = entra_transitions(probes)
    pending = [e for e in events if e.type == "sync-pending"]
    assert pending
    assert pending[0].severity == SEV_SYNC_PENDING


def test_entra_link_broken_when_sid_mismatch():
    from web.device_regression import entra_transitions, SEV_LINK_BROKEN
    probes = [
        _probe("t1",
               ad=[{"objectGUID": "G1", "objectSid": "S-AD",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}]),
        _probe("t2",
               ad=[{"objectGUID": "G1", "objectSid": "S-AD",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}],
               entra=[{"id": "E1", "trustType": "ServerAd",
                        "onPremisesSecurityIdentifier": "S-WRONG"}]),
    ]
    events = entra_transitions(probes)
    broken = [e for e in events if e.type == "link-broken"]
    assert broken and broken[0].severity == SEV_LINK_BROKEN


def test_entra_hybrid_synced_is_progression():
    from web.device_regression import entra_transitions, SEV_PROGRESSION
    probes = [
        _probe("t1",
               ad=[{"objectGUID": "G1", "objectSid": "S-AD",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}]),
        _probe("t2",
               ad=[{"objectGUID": "G1", "objectSid": "S-AD",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}],
               entra=[{"id": "E1", "trustType": "ServerAd",
                        "onPremisesSecurityIdentifier": "S-AD"}]),
    ]
    events = entra_transitions(probes)
    synced = [e for e in events if e.type == "hybrid-synced"]
    assert synced and synced[0].severity == SEV_PROGRESSION
    # Link-broken must NOT fire since SIDs match.
    assert not any(e.type == "link-broken" for e in events)


# ---------------------------------------------------------------------------
# Intune
# ---------------------------------------------------------------------------


def test_intune_enrollment_and_compliance_flip():
    from web.device_regression import intune_transitions, SEV_REGRESSION, SEV_PROGRESSION
    probes = [
        _probe("t1"),
        _probe("t2", intune=[{"id": "I1", "complianceState": "compliant"}]),
        _probe("t3", intune=[{"id": "I1", "complianceState": "noncompliant"}]),
        _probe("t4", intune=[{"id": "I1", "complianceState": "compliant"}]),
    ]
    events = intune_transitions(probes)
    types = [(e.type, e.severity) for e in events]
    assert ("enrolled", SEV_PROGRESSION) in types
    assert any(t == "compliance-changed" and s == SEV_REGRESSION
               for t, s in types)
    assert any(t == "compliance-changed" and s == SEV_PROGRESSION
               for t, s in types)


def test_intune_unenrolled_is_regression():
    from web.device_regression import intune_transitions, SEV_REGRESSION
    probes = [
        _probe("t1", intune=[{"id": "I1", "complianceState": "compliant"}]),
        _probe("t2"),
    ]
    events = intune_transitions(probes)
    assert any(e.type == "unenrolled" and e.severity == SEV_REGRESSION
               for e in events)


# ---------------------------------------------------------------------------
# Merged timeline
# ---------------------------------------------------------------------------


def test_build_timeline_is_newest_first_across_sources():
    from web.device_regression import build_timeline
    pve = [
        _pve("2026-04-20T23:41:00+00:00", vmid=1, status="stopped",
             config_digest="a"),
        _pve("2026-04-20T23:42:00+00:00", vmid=1, status="running",
             config_digest="a"),
    ]
    probes = [
        _probe("2026-04-20T23:47:00+00:00",
               ad=[{"objectGUID": "G1", "objectSid": "S",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}]),
        _probe("2026-04-20T23:52:00+00:00",
               ad=[{"objectGUID": "G1", "objectSid": "S",
                    "cn": "X", "distinguishedName": "CN=X",
                    "userAccountControl": 4096}],
               entra=[{"id": "E1", "trustType": "ServerAd",
                       "onPremisesSecurityIdentifier": "S"}]),
    ]
    events = build_timeline(pve, probes)
    # Newest (23:52) first.
    assert events[0].at == "2026-04-20T23:52:00+00:00"
    assert events[-1].at == "2026-04-20T23:41:00+00:00"
    # Every source is represented somewhere in the list.
    sources = {e.source for e in events}
    assert sources == {"pve", "ad", "entra"}
