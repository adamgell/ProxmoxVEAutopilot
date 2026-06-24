"""VM evidence detail API and retired legacy device route contracts."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path: Path, pg_conn):
    from fastapi.testclient import TestClient
    from web import app as app_module, device_history_pg
    db_path = tmp_path / "device_monitor.db"
    device_history_pg.reset_for_tests(pg_conn)
    device_history_pg.init(pg_conn)
    with TestClient(app_module.app) as c:
        yield c, db_path


def _seed_healthy_vm(db, vmid=116):
    from web import device_history_pg
    sweep1 = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep1, {
        "vmid": vmid, "status": "stopped", "node": "pve2",
        "name": "Gell-EC41E7EB", "config_digest": "d1",
        "checked_at": "2026-04-20T23:41:00+00:00",
    })
    device_history_pg.insert_device_probe(sweep1, {
        "vmid": vmid, "win_name": "", "serial": "",
        "ad_matches_json": "[]", "entra_matches_json": "[]",
        "intune_matches_json": "[]",
        "checked_at": "2026-04-20T23:41:00+00:00",
    })
    device_history_pg.finish_sweep(sweep1, vm_count=1)
    sweep2 = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep2, {
        "vmid": vmid, "status": "running", "node": "pve2",
        "name": "Gell-EC41E7EB", "config_digest": "d1",
        "checked_at": "2026-04-20T23:42:00+00:00",
    })
    device_history_pg.insert_device_probe(sweep2, {
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
    device_history_pg.finish_sweep(sweep2, vm_count=1)


def test_device_detail_happy_path_renders_all_columns(client):
    c, db = client
    _seed_healthy_vm(db, vmid=116)
    r = c.get("/legacy/devices/116", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/react/vms/116"

    r = c.get("/api/vms/116/detail")
    assert r.status_code == 200
    body = r.json()
    assert body["pve"]["name"] == "Gell-EC41E7EB"
    assert body["probe"]["win_name"] == "GELL-EC41E7EB"
    assert body["entra_matches"][0]["trustType"] == "ServerAd"
    assert body["intune_matches"][0]["complianceState"] == "compliant"
    linkage = {row["label"]: row for row in body["linkage"]}
    assert linkage["SMBIOS.serial → Intune.serialNumber"]["ok"] is True
    assert linkage["AD.objectSid → Entra.onPremSecurityIdentifier"]["ok"] is True
    assert {event["type"] for event in body["timeline"]} >= {"power-on", "hybrid-synced"}


def test_device_detail_shows_known_credentials(client, monkeypatch):
    from web import app as app_module

    c, db = client
    _seed_healthy_vm(db, vmid=105)
    monkeypatch.setattr(
        app_module,
        "_known_credentials_for_vmid",
        lambda vmid: [{
            "source": "CloudOSD",
            "label": "Local admin",
            "username": "localadmin",
            "password": "Mep7!Qav2",
            "vm_name": "WrkGrp-8F47E090",
            "run_url": "/cloudosd/runs/b5c5f393-82e8-41a1-849d-d5c3636ee5c5",
            "updated_at": "2026-05-18T17:10:00+00:00",
            "note": "Visible workgroup credential from the deployment run.",
        }] if vmid == 105 else [],
    )

    r = c.get("/api/vms/105/detail")

    assert r.status_code == 200
    body = r.json()
    assert body["known_credentials"] == [{
        "source": "CloudOSD",
        "label": "Local admin",
        "username": "localadmin",
        "password_available": True,
        "password_mask": "********",
        "vm_name": "WrkGrp-8F47E090",
        "run_id": "",
        "run_url": "/cloudosd/runs/b5c5f393-82e8-41a1-849d-d5c3636ee5c5",
        "updated_at": "2026-05-18T17:10:00+00:00",
        "note": "Visible workgroup credential from the deployment run.",
    }]


def test_vm_detail_api_returns_masked_evidence_and_latest_screenshot(client, monkeypatch, tmp_path):
    from web import app as app_module

    c, db = client
    _seed_healthy_vm(db, vmid=105)
    monkeypatch.setattr(app_module, "SCREENSHOT_STORE_DIR", tmp_path / "screenshots")
    stored = app_module._store_vm_screenshot_record(
        vmid=105,
        png_bytes=b"\x89PNG\r\n\x1a\nlatest",
        source="collector",
    )
    monkeypatch.setattr(
        app_module,
        "_known_credentials_for_vmid",
        lambda vmid: [{
            "source": "CloudOSD",
            "label": "Local admin",
            "username": "localadmin",
            "password": "Mep7!Qav2",
            "vm_name": "WrkGrp-8F47E090",
            "run_url": "/cloudosd/runs/b5c5f393-82e8-41a1-849d-d5c3636ee5c5",
            "updated_at": "2026-05-18T17:10:00+00:00",
            "note": "Visible workgroup credential from the deployment run.",
        }] if vmid == 105 else [],
    )

    r = c.get("/api/vms/105/detail")

    assert r.status_code == 200
    body = r.json()
    assert body["vmid"] == 105
    assert body["pve"]["name"] == "Gell-EC41E7EB"
    assert body["probe"]["win_name"] == "GELL-EC41E7EB"
    assert body["latest_screenshot"]["image_url"] == stored["image_url"]
    assert body["latest_screenshot"]["source"] == "collector"
    assert body["known_credentials"] == [{
        "source": "CloudOSD",
        "label": "Local admin",
        "username": "localadmin",
        "password_available": True,
        "password_mask": "********",
        "vm_name": "WrkGrp-8F47E090",
        "run_id": "",
        "run_url": "/cloudosd/runs/b5c5f393-82e8-41a1-849d-d5c3636ee5c5",
        "updated_at": "2026-05-18T17:10:00+00:00",
        "note": "Visible workgroup credential from the deployment run.",
    }]
    assert "password" not in body["known_credentials"][0]
    assert any(event["type"] == "screenshot-captured" for event in body["timeline"])
    assert any(event["type"] == "credential-discovered" for event in body["timeline"])
    assert any(event["type"] == "identity-sync" for event in body["timeline"])
    assert body["identity_sync"]["source"] == "monitoring_sweep"


def test_vm_credentials_reveal_api_returns_vm_bound_passwords(client, monkeypatch):
    from web import app as app_module

    c, db = client
    _seed_healthy_vm(db, vmid=105)
    monkeypatch.setattr(
        app_module,
        "_known_credentials_for_vmid",
        lambda vmid: [{
            "source": "CloudOSD",
            "label": "Local admin",
            "username": "localadmin",
            "password": "Mep7!Qav2",
            "vm_name": "WrkGrp-8F47E090",
            "run_id": "run-1",
            "run_url": "/cloudosd/runs/run-1",
            "updated_at": "2026-05-18T17:10:00+00:00",
            "note": "Visible workgroup credential from the deployment run.",
        }] if vmid == 105 else [],
    )

    detail = c.get("/api/vms/105/detail")
    assert detail.status_code == 200
    assert "password" not in detail.json()["known_credentials"][0]

    revealed = c.post("/api/vms/105/credentials/reveal")

    assert revealed.status_code == 200
    assert revealed.json()["credentials"] == [{
        "source": "CloudOSD",
        "label": "Local admin",
        "username": "localadmin",
        "password": "Mep7!Qav2",
        "vm_name": "WrkGrp-8F47E090",
        "run_id": "run-1",
        "run_url": "/cloudosd/runs/run-1",
        "updated_at": "2026-05-18T17:10:00+00:00",
        "note": "Visible workgroup credential from the deployment run.",
    }]


def test_vm_latest_screenshot_api_serves_shared_store(client, monkeypatch, tmp_path):
    from web import app as app_module

    c, _ = client
    monkeypatch.setattr(app_module, "SCREENSHOT_STORE_DIR", tmp_path / "screenshots")
    stored = app_module._store_vm_screenshot_record(
        vmid=116,
        png_bytes=b"\x89PNG\r\n\x1a\npreview",
        source="manual",
    )

    latest = c.get("/api/vms/116/screenshots/latest")
    image = c.get(stored["image_url"])

    assert latest.status_code == 200
    assert latest.json()["image_url"] == stored["image_url"]
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == b"\x89PNG\r\n\x1a\npreview"


def test_vm_latest_screenshot_api_returns_404_when_empty(client, monkeypatch, tmp_path):
    from web import app as app_module

    c, _ = client
    monkeypatch.setattr(app_module, "SCREENSHOT_STORE_DIR", tmp_path / "screenshots")

    assert c.get("/api/vms/116/screenshots/latest").status_code == 404


def test_known_credentials_includes_osdeploy_local_admin(pg_conn):
    from web import app as app_module, cloudosd_pg, osdeploy_pg, ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn)
    cloudosd_pg.init(pg_conn)
    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact = osdeploy_pg.create_artifact(
        pg_conn,
        architecture="amd64",
        osdeploy_module_version="26.1.30.5",
        osdbuilder_module_version="24.10.8.1",
        adk_version="10.1.26100.1",
        build_sha="servercredtest",
        iso_path="/app/output/osdeploy.iso",
        wim_path="/app/output/osdeploy.wim",
        manifest_path="/app/output/osdeploy.json",
        iso_sha256="c" * 64,
        wim_sha256="d" * 64,
        source_media="Windows Server 2025",
        image_name="Windows Server 2025 Datacenter",
        image_index=4,
        os_version="Windows Server 2025",
        os_edition="Datacenter",
        os_language="en-us",
        built_by_host="builder",
        proxmox_volid="local:iso/osdeploy.iso",
    )
    run = osdeploy_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="FS01",
        requested_vmid=9109,
    )

    known = app_module._known_credentials_for_vmid(9109)

    assert known == [{
        "source": "OSDeploy",
        "label": "Local admin",
        "username": run["local_admin"]["username"],
        "password": run["local_admin"]["password"],
        "vm_name": "FS01",
        "run_id": run["run_id"],
        "run_url": f"/osdeploy/runs/{run['run_id']}",
        "updated_at": run["updated_at"],
        "note": "Visible local administrator credential from the deployment run.",
    }]


def test_device_detail_404_for_unknown_vmid(client):
    c, _ = client
    r = c.get("/legacy/devices/99999", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/react/vms/99999"

    r = c.get("/api/vms/99999/detail")
    assert r.status_code == 404


def test_device_detail_shows_link_broken_warning(client):
    """When AD.objectSid ≠ Entra.onPremSID, the detail page surfaces
    the link-broken event on the timeline AND the linkage strip shows
    ✗ for that row."""
    c, db = client
    from web import device_history_pg
    sweep = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep, {
        "vmid": 42, "status": "running", "node": "pve2",
        "name": "Broken", "config_digest": "x",
        "checked_at": "2026-04-20T23:00:00+00:00",
    })
    device_history_pg.insert_device_probe(sweep, {
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
    device_history_pg.finish_sweep(sweep, vm_count=1)
    # Second probe so the detector can diff and emit link-broken.
    sweep2 = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep2, {
        "vmid": 42, "status": "running", "node": "pve2",
        "name": "Broken", "config_digest": "x",
        "checked_at": "2026-04-20T23:20:00+00:00",
    })
    device_history_pg.insert_device_probe(sweep2, {
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
    device_history_pg.finish_sweep(sweep2, vm_count=1)
    r = c.get("/api/vms/42/detail")
    assert r.status_code == 200
    assert any(event["type"] == "link-broken" for event in r.json()["timeline"])
    linkage = {row["label"]: row for row in r.json()["linkage"]}
    assert linkage["AD.objectSid → Entra.onPremSecurityIdentifier"]["ok"] is False


def test_device_detail_ignores_in_progress_sweep_for_latest_pair(client):
    c, _ = client
    from web import device_history_pg

    completed = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(completed, {
        "vmid": 77,
        "status": "running",
        "node": "pve2",
        "name": "COMPLETED-NAME",
        "config_digest": "complete",
        "checked_at": "2026-05-07T15:00:00+00:00",
    })
    device_history_pg.insert_device_probe(completed, {
        "vmid": 77,
        "win_name": "COMPLETED-WIN",
        "serial": "COMPLETED-SERIAL",
        "checked_at": "2026-05-07T15:00:05+00:00",
    })
    device_history_pg.finish_sweep(completed, vm_count=1)

    in_progress = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(in_progress, {
        "vmid": 77,
        "status": "running",
        "node": "pve2",
        "name": "IN-PROGRESS-NAME",
        "config_digest": "in-progress",
        "checked_at": "2026-05-07T15:10:00+00:00",
    })

    r = c.get("/api/vms/77/detail")

    assert r.status_code == 200
    body = r.json()
    assert body["pve"]["name"] == "COMPLETED-NAME"
    assert body["probe"]["win_name"] == "COMPLETED-WIN"
    assert body["pve"]["name"] != "IN-PROGRESS-NAME"


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
