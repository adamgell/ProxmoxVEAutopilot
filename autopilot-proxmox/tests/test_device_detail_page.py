"""GET /devices/<vmid> — renders four columns, linkage strip, timeline."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_db
    db_path = tmp_path / "device_monitor.db"
    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", db_path)
    device_history_db.init(db_path)
    with TestClient(app_module.app) as c:
        yield c, db_path


def _seed_healthy_vm(db, vmid=116):
    from web import device_history_db
    sweep1 = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep1, {
        "vmid": vmid, "status": "stopped", "node": "pve2",
        "name": "Gell-EC41E7EB", "config_digest": "d1",
        "checked_at": "2026-04-20T23:41:00+00:00",
    })
    device_history_db.insert_device_probe(db, sweep1, {
        "vmid": vmid, "win_name": "", "serial": "",
        "ad_matches_json": "[]", "entra_matches_json": "[]",
        "intune_matches_json": "[]",
        "checked_at": "2026-04-20T23:41:00+00:00",
    })
    sweep2 = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep2, {
        "vmid": vmid, "status": "running", "node": "pve2",
        "name": "Gell-EC41E7EB", "config_digest": "d1",
        "checked_at": "2026-04-20T23:42:00+00:00",
    })
    device_history_db.insert_device_probe(db, sweep2, {
        "vmid": vmid, "win_name": "GELL-EC41E7EB",
        "serial": "Gell-EC41E7EB",
        "ad_matches_json": json.dumps([{
            "objectGUID": "7d41fbb4-b239-9ce6-…",
            "objectSid": "S-1-5-21-4163347863-3329390546-514099273-4829",
            "distinguishedName":
                "CN=GELL-EC41E7EB,OU=Devices,OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
            "cn": "GELL-EC41E7EB",
            "sAMAccountName": "GELL-EC41E7EB$",
            "userAccountControl": 4096,
            "source_ou_label": "WorkspaceLabs",
            "source_ou_dn": "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
            "whenCreated": "2026-04-20T23:47:00+00:00",
            "whenChanged": "2026-04-21T00:01:00+00:00",
        }]),
        "entra_matches_json": json.dumps([{
            "id": "ab56021c-b01a-485e-a24c-9746decdba9b",
            "deviceId": "a6c91d4e-3f21-4891-9d44-…",
            "displayName": "GELL-EC41E7EB",
            "trustType": "ServerAd",
            "onPremisesSyncEnabled": True,
            "onPremisesSecurityIdentifier":
                "S-1-5-21-4163347863-3329390546-514099273-4829",
            "accountEnabled": True,
        }]),
        "intune_matches_json": json.dumps([{
            "id": "f8153fe8-24e7-4a98-8188-0feb606c292e",
            "deviceName": "GELL-EC41E7EB",
            "complianceState": "compliant",
            "serialNumber": "Gell-EC41E7EB",
            "azureADDeviceId": "a6c91d4e-3f21-4891-9d44-…",
        }]),
        "checked_at": "2026-04-20T23:52:00+00:00",
    })


def test_device_detail_happy_path_renders_all_columns(client):
    c, db = client
    _seed_healthy_vm(db, vmid=116)
    r = c.get("/devices/116")
    assert r.status_code == 200
    # All four column headings present.
    assert ">PVE<" in r.text
    assert "Active Directory" in r.text
    assert ">Entra" in r.text
    assert ">Intune" in r.text
    # Linkage health rows.
    assert "Linkage health" in r.text
    assert "SMBIOS.serial → Intune.serialNumber" in r.text
    assert "AD.objectSid → Entra.onPremSecurityIdentifier" in r.text
    # Concrete values surfaced.
    assert "Gell-EC41E7EB" in r.text
    assert "GELL-EC41E7EB" in r.text
    assert "ServerAd" in r.text
    assert "compliant" in r.text
    # Timeline shows events from more than one source.
    assert "power-on" in r.text
    assert "hybrid-synced" in r.text
    # Breadcrumb back to /monitoring.
    assert 'href="/monitoring"' in r.text


def test_device_detail_404_for_unknown_vmid(client):
    c, _ = client
    r = c.get("/devices/99999")
    assert r.status_code == 404


def test_device_detail_shows_link_broken_warning(client):
    """When AD.objectSid ≠ Entra.onPremSID, the detail page surfaces
    the link-broken event on the timeline AND the linkage strip shows
    ✗ for that row."""
    c, db = client
    from web import device_history_db
    sweep = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep, {
        "vmid": 42, "status": "running", "node": "pve2",
        "name": "Broken", "config_digest": "x",
        "checked_at": "2026-04-20T23:00:00+00:00",
    })
    device_history_db.insert_device_probe(db, sweep, {
        "vmid": 42, "win_name": "BROKEN", "serial": "S42",
        "ad_matches_json": json.dumps([{
            "objectGUID": "G1", "objectSid": "S-AD-CORRECT",
            "distinguishedName": "CN=BROKEN,OU=Devices,OU=WorkspaceLabs,DC=h,DC=g,DC=o",
            "cn": "BROKEN", "userAccountControl": 4096,
        }]),
        "entra_matches_json": json.dumps([{
            "id": "E1", "trustType": "ServerAd",
            "onPremisesSecurityIdentifier": "S-WRONG",
            "deviceId": "d1",
        }]),
        "intune_matches_json": "[]",
        "checked_at": "2026-04-20T23:05:00+00:00",
    })
    # Second probe so the detector can diff and emit link-broken.
    sweep2 = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep2, {
        "vmid": 42, "status": "running", "node": "pve2",
        "name": "Broken", "config_digest": "x",
        "checked_at": "2026-04-20T23:20:00+00:00",
    })
    device_history_db.insert_device_probe(db, sweep2, {
        "vmid": 42, "win_name": "BROKEN", "serial": "S42",
        "ad_matches_json": json.dumps([{
            "objectGUID": "G1", "objectSid": "S-AD-CORRECT",
            "distinguishedName": "CN=BROKEN,OU=Devices,OU=WorkspaceLabs,DC=h,DC=g,DC=o",
            "cn": "BROKEN", "userAccountControl": 4096,
        }]),
        "entra_matches_json": json.dumps([{
            "id": "E1", "trustType": "ServerAd",
            "onPremisesSecurityIdentifier": "S-WRONG",
            "deviceId": "d1",
        }]),
        "intune_matches_json": "[]",
        "checked_at": "2026-04-20T23:20:00+00:00",
    })
    r = c.get("/devices/42")
    assert r.status_code == 200
    assert "link-broken" in r.text
    # Linkage strip shows ✗ (the check is False for SID mismatch).
    assert "lk-bad" in r.text


# ---------------------------------------------------------------------------
# Identifier-conversion helpers (fixes the always-red SID linkage check)
# ---------------------------------------------------------------------------

def test_sid_bytes_to_str_canonical_form():
    """Raw NT SID blob must round-trip to 'S-1-5-21-...' string form.

    This is the fixture that makes AD.objectSid comparable to Entra's
    onPremisesSecurityIdentifier — before the fix, AD's SID came out as
    a hex dump and the linkage check was always red.
    """
    from web.app import _sid_bytes_to_str
    # S-1-5-21-4163347863-3329390546-514099273-4601 — one of the live
    # box's actual machine SIDs.
    blob = (
        bytes([1, 5])              # revision=1, subauthority_count=5
        + (5).to_bytes(6, "big")   # IdentifierAuthority=5 (NT Authority)
        + (21).to_bytes(4, "little")
        + (4163347863).to_bytes(4, "little")
        + (3329390546).to_bytes(4, "little")
        + (514099273).to_bytes(4, "little")
        + (4601).to_bytes(4, "little")
    )
    assert _sid_bytes_to_str(blob) == (
        "S-1-5-21-4163347863-3329390546-514099273-4601"
    )


def test_sid_bytes_to_str_empty_and_short():
    """Defensive: empty or truncated input must not raise."""
    from web.app import _sid_bytes_to_str
    assert _sid_bytes_to_str(b"") == ""
    assert _sid_bytes_to_str(b"\x01\x00\x00") == "010000"  # too short → hex fallback


def test_guid_bytes_to_str_matches_windows_mixed_endian():
    """Windows objectGUID is mixed-endian — UUID(bytes_le=...) handles it.
    Confirm the byte order matches what Graph/tools emit."""
    from web.app import _guid_bytes_to_str
    # Known example: 01020304-0506-0708-090a-0b0c0d0e0f10
    # With mixed-endian (Windows), the on-disk bytes are
    # 04 03 02 01 06 05 08 07 09 0a 0b 0c 0d 0e 0f 10
    raw = bytes([
        0x04, 0x03, 0x02, 0x01,  # Data1 (little-endian)
        0x06, 0x05,              # Data2 (little-endian)
        0x08, 0x07,              # Data3 (little-endian)
        0x09, 0x0a,              # Data4[0..1] (big-endian)
        0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10,  # Data4[2..7]
    ])
    assert _guid_bytes_to_str(raw) == "01020304-0506-0708-090a-0b0c0d0e0f10"


def test_linkage_windows_name_to_ad_cn_is_case_insensitive():
    """AD uppercases the CN (GELL-E9C0C757) while Windows reports the
    mixed-case hostname (Gell-E9C0C757). Should be a green check, not
    the old hourglass."""
    from web.app import _linkage_health
    import json
    probe = {
        "serial": "",
        "win_name": "Gell-E9C0C757",
        "ad_matches_json": json.dumps([{"cn": "GELL-E9C0C757"}]),
        "entra_matches_json": "[]",
        "intune_matches_json": "[]",
    }
    rows = _linkage_health({}, probe)
    hit = next(r for r in rows if r["label"] == "Windows.Name → AD.cn")
    assert hit["ok"] is True
