"""Tests for web.device_history_db — schema, DAL, and the
'at least one enabled search OU' invariant."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def db(tmp_path: Path):
    from web import device_history_db
    db_path = tmp_path / "device_monitor.db"
    device_history_db.init(db_path)
    return db_path


def test_init_creates_schema_and_seeds_default_ou(db):
    from web import device_history_db
    ous = device_history_db.list_search_ous(db)
    assert len(ous) == 1
    assert ous[0].dn == "OU=WorkspaceLabs,DC=home,DC=gell,DC=one"
    assert ous[0].enabled is True
    assert ous[0].label == "WorkspaceLabs"


def test_init_is_idempotent(tmp_path):
    from web import device_history_db
    db_path = tmp_path / "d.db"
    device_history_db.init(db_path)
    device_history_db.add_search_ou(db_path, dn="OU=OtherSite,DC=home,DC=gell,DC=one")
    device_history_db.init(db_path)  # must not re-seed, must not drop data
    ous = device_history_db.list_search_ous(db_path)
    assert {o.dn for o in ous} == {
        "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
        "OU=OtherSite,DC=home,DC=gell,DC=one",
    }


def test_init_without_seed_leaves_table_empty(tmp_path):
    from web import device_history_db
    db_path = tmp_path / "d.db"
    device_history_db.init(db_path, seed_default_ou=False)
    assert device_history_db.list_search_ous(db_path) == []


def test_settings_defaults(db):
    from web import device_history_db
    s = device_history_db.get_settings(db)
    assert s.enabled is True
    assert s.interval_seconds == 900


def test_settings_update(db):
    from web import device_history_db
    device_history_db.update_settings(
        db, interval_seconds=600, enabled=False, ad_credential_id=7,
    )
    s = device_history_db.get_settings(db)
    assert s.interval_seconds == 600
    assert s.enabled is False
    assert s.ad_credential_id == 7


def test_settings_interval_floor(db):
    from web import device_history_db
    with pytest.raises(ValueError):
        device_history_db.update_settings(db, interval_seconds=30)


# ---------------------------------------------------------------------------
# Search OU invariant
# ---------------------------------------------------------------------------


def test_add_search_ou_validates_dn(db):
    from web import device_history_db
    with pytest.raises(device_history_db.InvalidDn):
        device_history_db.add_search_ou(db, dn="not a dn")
    with pytest.raises(device_history_db.InvalidDn):
        device_history_db.add_search_ou(db, dn="OU=X;DC=bad")


def test_add_search_ou_appends(db):
    from web import device_history_db
    device_history_db.add_search_ou(
        db, dn="OU=OtherSite,DC=home,DC=gell,DC=one",
        label="OtherSite",
    )
    ous = device_history_db.list_search_ous(db)
    assert [o.dn for o in ous] == [
        "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
        "OU=OtherSite,DC=home,DC=gell,DC=one",
    ]


def test_cannot_delete_last_ou(db):
    from web import device_history_db
    ou = device_history_db.list_search_ous(db)[0]
    with pytest.raises(device_history_db.CannotDeleteLastOu):
        device_history_db.delete_search_ou(db, ou.id)


def test_cannot_disable_last_enabled_ou(db):
    """Only one row exists and it's enabled → disabling it must fail."""
    from web import device_history_db
    ou = device_history_db.list_search_ous(db)[0]
    with pytest.raises(device_history_db.CannotDeleteLastOu):
        device_history_db.update_search_ou(db, ou.id, enabled=False)
    # Confirm the row wasn't mutated by the rolled-back update.
    assert device_history_db.list_search_ous(db)[0].enabled is True


def test_can_delete_when_another_enabled_row_exists(db):
    from web import device_history_db
    device_history_db.add_search_ou(
        db, dn="OU=OtherSite,DC=home,DC=gell,DC=one",
    )
    first = device_history_db.list_search_ous(db)[0]
    device_history_db.delete_search_ou(db, first.id)
    remaining = device_history_db.list_search_ous(db)
    assert len(remaining) == 1
    assert remaining[0].dn == "OU=OtherSite,DC=home,DC=gell,DC=one"


def test_can_disable_when_another_enabled_row_exists(db):
    from web import device_history_db
    device_history_db.add_search_ou(
        db, dn="OU=OtherSite,DC=home,DC=gell,DC=one",
    )
    first = device_history_db.list_search_ous(db)[0]
    device_history_db.update_search_ou(db, first.id, enabled=False)
    enabled = device_history_db.list_enabled_search_ous(db)
    assert len(enabled) == 1
    assert enabled[0].dn == "OU=OtherSite,DC=home,DC=gell,DC=one"


def test_deleting_disabled_row_when_it_is_the_only_one_still_fails(db):
    """Edge case: only one row left, and it's already disabled. Still
    can't delete — the 'at least one row' rule is stricter than the
    'at least one enabled' rule."""
    from web import device_history_db
    # Add a second, disable the first, delete the second — now only
    # the disabled row remains.
    device_history_db.add_search_ou(
        db, dn="OU=Temporary,DC=home,DC=gell,DC=one",
    )
    first, second = device_history_db.list_search_ous(db)
    # Disable the second so we can delete the first.
    device_history_db.update_search_ou(db, second.id, enabled=False)
    # Now there are two rows (one enabled, one disabled).
    # Delete the enabled one — but that would leave zero enabled, so it
    # must fail.
    with pytest.raises(device_history_db.CannotDeleteLastOu):
        device_history_db.delete_search_ou(db, first.id)
    # Disable all-but-one? Flip roles: enable second, disable first,
    # delete the now-disabled first. That should succeed since the
    # second is enabled.
    device_history_db.update_search_ou(db, second.id, enabled=True)
    device_history_db.update_search_ou(db, first.id, enabled=False)
    device_history_db.delete_search_ou(db, first.id)
    assert len(device_history_db.list_search_ous(db)) == 1


def test_list_enabled_filters(db):
    from web import device_history_db
    device_history_db.add_search_ou(
        db, dn="OU=Disabled,DC=home,DC=gell,DC=one", enabled=False,
    )
    enabled = device_history_db.list_enabled_search_ous(db)
    assert [o.dn for o in enabled] == [
        "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
    ]


# ---------------------------------------------------------------------------
# Sweeps + probes + pve snapshots
# ---------------------------------------------------------------------------


def test_sweep_lifecycle(db):
    from web import device_history_db
    sweep_id = device_history_db.start_sweep(db)
    assert sweep_id >= 1
    device_history_db.finish_sweep(
        db, sweep_id, vm_count=3, errors={"graph_auth": "token refresh failed"},
    )


def test_insert_pve_snapshot_roundtrip(db):
    from web import device_history_db
    sweep = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep, {
        "vmid": 116, "present": 1, "node": "pve2",
        "name": "Gell-EC41E7EB", "status": "running",
        "tags_csv": "autopilot", "cores": 2, "memory_mb": 4096,
        "machine": "pc-q35-10.1", "bios": "ovmf",
        "args": "-smbios file=/var/lib/vz/snippets/autopilot-smbios-vm-116.bin",
        "disks_json": json.dumps([{"bus": "scsi", "index": 0, "size_bytes": 64 * 2**30}]),
        "net_json": json.dumps([{"index": 0, "bridge": "vmbr0"}]),
        "config_digest": "abc123",
    })
    row = device_history_db.latest_pve_snapshot(db, 116)
    assert row is not None
    assert row["name"] == "Gell-EC41E7EB"
    assert row["status"] == "running"
    assert row["cores"] == 2


def test_insert_device_probe_roundtrip(db):
    from web import device_history_db
    sweep = device_history_db.start_sweep(db)
    device_history_db.insert_device_probe(db, sweep, {
        "vmid": 116, "vm_name": "Gell-EC41E7EB",
        "win_name": "GELL-EC41E7EB",
        "serial": "Gell-EC41E7EB",
        "uuid": "B092D2EB-7D41-4FB4-B239-9CE6F29C1F96",
        "ad_found": 1, "ad_match_count": 1,
        "ad_matches_json": json.dumps([{
            "distinguishedName": "CN=GELL-EC41E7EB,OU=Devices,OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
            "source_ou_dn": "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
        }]),
        "entra_found": 0, "entra_match_count": 0,
        "entra_matches_json": "[]",
        "intune_found": 0, "intune_match_count": 0,
        "intune_matches_json": "[]",
        "probe_errors_json": "{}",
    })
    row = device_history_db.latest_device_probe(db, 116)
    assert row is not None
    assert row["win_name"] == "GELL-EC41E7EB"
    assert row["ad_match_count"] == 1
    assert row["entra_found"] == 0


def test_history_returns_newest_first(db):
    """History view used by the device-detail timeline needs ordering
    guarantees; sweep 2's rows must come before sweep 1's."""
    from web import device_history_db
    import time as _time

    sweep1 = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep1, {
        "vmid": 42, "status": "stopped",
        "config_digest": "d1",
        "checked_at": "2026-04-20T10:00:00+00:00",
    })
    # Force a different checked_at so ORDER BY is deterministic.
    sweep2 = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep2, {
        "vmid": 42, "status": "running",
        "config_digest": "d2",
        "checked_at": "2026-04-20T10:15:00+00:00",
    })
    hist = device_history_db.history_for_vmid(db, 42, limit=10)
    statuses = [r["status"] for r in hist["pve_snapshots"]]
    assert statuses == ["running", "stopped"]


def test_latest_per_vmid_joins_pve_and_probe(db):
    from web import device_history_db
    sweep = device_history_db.start_sweep(db)
    device_history_db.insert_pve_snapshot(db, sweep, {
        "vmid": 109, "status": "running", "name": "Gell-C368D9DC",
        "config_digest": "x",
        "checked_at": "2026-04-20T10:00:00+00:00",
    })
    device_history_db.insert_device_probe(db, sweep, {
        "vmid": 109, "win_name": "GELL-C368D9DC", "ad_found": 1,
        "ad_match_count": 1, "ad_matches_json": "[{}]",
        "entra_found": 0, "entra_matches_json": "[]",
        "intune_found": 0, "intune_matches_json": "[]",
        "checked_at": "2026-04-20T10:00:00+00:00",
    })
    # A second VM with only PVE data (guest agent down) still appears.
    device_history_db.insert_pve_snapshot(db, sweep, {
        "vmid": 107, "status": "stopped", "name": "Gell-0BFC6075",
        "config_digest": "y",
        "checked_at": "2026-04-20T10:00:00+00:00",
    })
    rows = device_history_db.latest_per_vmid(db)
    by_vmid = {r["vmid"]: r for r in rows}
    assert by_vmid[109]["probe"] is not None
    assert by_vmid[109]["probe"]["ad_found"] == 1
    assert by_vmid[107]["probe"] is None
    assert by_vmid[107]["pve"]["status"] == "stopped"
