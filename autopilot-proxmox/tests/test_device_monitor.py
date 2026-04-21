"""Tests for web.device_monitor — probe functions + sweep orchestrator.

Probes are pure functions of already-fetched data; the sweep is
exercised via a fake :class:`MonitorContext` whose callables return
canned data. No HTTP or LDAP is ever touched."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def db(tmp_path: Path):
    from web import device_history_db
    db_path = tmp_path / "device_monitor.db"
    device_history_db.init(db_path)
    return db_path


def _make_ctx(db_path, **overrides):
    """Build a MonitorContext with harmless no-op defaults; override
    individual callables as the test needs."""
    from web import device_monitor

    def _default_list_vms():
        return []

    def _default_fetch_config(vmid, node):
        raise AssertionError(f"no fake for fetch_pve_config({vmid})")

    def _default_guest(vmid, node):
        return None

    def _default_ad(dn, name):
        return []

    def _default_entra(name):
        return []

    def _default_intune(serial):
        return []

    callables = {
        "list_pve_vms": _default_list_vms,
        "fetch_pve_config": _default_fetch_config,
        "fetch_guest_details": _default_guest,
        "ad_search": _default_ad,
        "graph_find_entra_device": _default_entra,
        "graph_find_intune_device": _default_intune,
        "now": lambda: "2026-04-20T23:00:00+00:00",
    }
    callables.update(overrides)
    return device_monitor.MonitorContext(db_path=db_path, **callables)


# ---------------------------------------------------------------------------
# probe_pve — normalises raw Proxmox config
# ---------------------------------------------------------------------------


def test_probe_pve_extracts_disks_ignoring_cdrom():
    from web.device_monitor import probe_pve
    config = {
        "name": "Gell-EC41E7EB",
        "cores": "2", "memory": "4096", "machine": "pc-q35-10.1",
        "bios": "ovmf",
        "scsi0": "nvmepool:vm-116-disk-1,discard=on,size=64G,serial=APHV000116…",
        "ide2": "isos:iso/win11.iso,media=cdrom,size=8020412K",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,firewall=0",
        "tags": "autopilot",
        "args": "-smbios file=/var/lib/vz/snippets/autopilot-smbios-vm-116.bin",
    }
    snap = probe_pve(116, "pve2", config)
    disks = json.loads(snap["disks_json"])
    # CD-ROM (ide2) omitted; only scsi0 recorded.
    assert len(disks) == 1
    assert disks[0]["bus"] == "scsi"
    assert disks[0]["index"] == 0
    assert disks[0]["size_bytes"] == 64 * 1024 ** 3
    assert disks[0]["serial"].startswith("APHV000116")
    nets = json.loads(snap["net_json"])
    assert nets[0]["model"] == "virtio"
    assert nets[0]["bridge"] == "vmbr0"
    assert snap["cores"] == 2
    assert snap["memory_mb"] == 4096
    assert snap["tags_csv"] == "autopilot"
    assert "config_digest" in snap and len(snap["config_digest"]) == 64


def test_probe_pve_digest_stable_across_unrelated_reorderings():
    """Same inputs in different dict orderings produce the same digest.
    Proxmox returns fields in varying order — without this guarantee
    every sweep would register a spurious 'config-changed' event."""
    from web.device_monitor import probe_pve
    c1 = {"name": "A", "cores": "2", "memory": "4096",
          "scsi0": "p:vm-1-d0,size=10G",
          "net0": "virtio=MAC,bridge=vmbr0",
          "tags": "autopilot"}
    c2 = {"net0": "virtio=MAC,bridge=vmbr0",
          "tags": "autopilot",
          "memory": "4096", "cores": "2",
          "scsi0": "p:vm-1-d0,size=10G",
          "name": "A"}
    assert probe_pve(1, "pve2", c1)["config_digest"] == \
           probe_pve(1, "pve2", c2)["config_digest"]


def test_probe_pve_digest_changes_when_args_change():
    """SMBIOS file swap must register as a config change."""
    from web.device_monitor import probe_pve
    c1 = {"name": "A", "cores": "2", "memory": "4096",
          "args": "-smbios file=/a.bin"}
    c2 = {"name": "A", "cores": "2", "memory": "4096",
          "args": "-smbios file=/b.bin"}
    assert probe_pve(1, "pve2", c1)["config_digest"] != \
           probe_pve(1, "pve2", c2)["config_digest"]


def test_probe_pve_status_lock_come_from_vm_list_entry():
    from web.device_monitor import probe_pve
    snap = probe_pve(
        116, "pve2", {"name": "x"},
        vm_list_entry={"status": "running", "lock": "migrate"},
    )
    assert snap["status"] == "running"
    assert snap["lock_mode"] == "migrate"


# ---------------------------------------------------------------------------
# probe_ad_for_win_name — multi-OU union + per-OU error isolation
# ---------------------------------------------------------------------------


def test_ad_probe_unions_matches_from_two_ous(db):
    from web import device_history_db
    from web.device_monitor import probe_ad_for_win_name
    device_history_db.add_search_ou(
        db, dn="OU=OtherSite,DC=home,DC=gell,DC=one", label="OtherSite",
    )
    calls = []

    def fake_search(search_base, win_name):
        calls.append((search_base, win_name))
        if search_base.startswith("OU=WorkspaceLabs"):
            return [{"distinguishedName":
                     "CN=X,OU=Devices,OU=WorkspaceLabs,DC=home,DC=gell,DC=one"}]
        if search_base.startswith("OU=OtherSite"):
            return [{"distinguishedName":
                     "CN=X,OU=OtherSite,DC=home,DC=gell,DC=one"}]
        return []

    ctx = _make_ctx(db, ad_search=fake_search)
    ous = device_history_db.list_enabled_search_ous(db)
    matches, errors = probe_ad_for_win_name(ctx, "X", ous)
    assert len(matches) == 2
    assert errors == {}
    # Each match carries its source OU label.
    labels = {m["source_ou_label"] for m in matches}
    assert labels == {"WorkspaceLabs", "OtherSite"}
    # Both OUs were queried.
    assert {c[0] for c in calls} == {
        "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
        "OU=OtherSite,DC=home,DC=gell,DC=one",
    }


def test_ad_probe_isolates_per_ou_errors(db):
    """A permission error on one OU must not block the other."""
    from web import device_history_db
    from web.device_monitor import probe_ad_for_win_name
    device_history_db.add_search_ou(
        db, dn="OU=Forbidden,DC=home,DC=gell,DC=one", label="Forbidden",
    )

    def fake_search(search_base, win_name):
        if "Forbidden" in search_base:
            raise PermissionError("no access")
        return [{"distinguishedName": "CN=X,OU=Devices,OU=WorkspaceLabs,DC=home,DC=gell,DC=one"}]

    ctx = _make_ctx(db, ad_search=fake_search)
    ous = device_history_db.list_enabled_search_ous(db)
    matches, errors = probe_ad_for_win_name(ctx, "X", ous)
    assert len(matches) == 1
    assert matches[0]["source_ou_label"] == "WorkspaceLabs"
    assert "OU=Forbidden,DC=home,DC=gell,DC=one" in errors
    assert "PermissionError" in errors["OU=Forbidden,DC=home,DC=gell,DC=one"]


def test_ad_probe_empty_result_is_not_an_error(db):
    from web import device_history_db
    from web.device_monitor import probe_ad_for_win_name
    ctx = _make_ctx(db, ad_search=lambda dn, n: [])
    ous = device_history_db.list_enabled_search_ous(db)
    matches, errors = probe_ad_for_win_name(ctx, "Missing", ous)
    assert matches == []
    assert errors == {}


# ---------------------------------------------------------------------------
# sweep — orchestrator
# ---------------------------------------------------------------------------


def _fake_config(vmid):
    return {
        "name": f"VM-{vmid}",
        "cores": "2", "memory": "4096", "machine": "pc-q35-10.1",
        "bios": "ovmf",
        "scsi0": "nvmepool:vm-{}-disk-0,size=64G".format(vmid),
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        "tags": "autopilot",
    }


def test_sweep_writes_pve_and_probe_rows_for_autopilot_vms(db):
    from web import device_history_db, device_monitor
    vms = [
        {"vmid": 116, "name": "Gell-EC41E7EB", "node": "pve2",
         "status": "running", "tags": "autopilot"},
        {"vmid": 250, "name": "ubuntu-template", "node": "pve2",
         "status": "stopped", "tags": "template"},  # not in scope
    ]
    ctx = _make_ctx(
        db,
        list_pve_vms=lambda: vms,
        fetch_pve_config=lambda vmid, node: _fake_config(vmid),
        fetch_guest_details=lambda vmid, node: {
            "win_name": f"WIN-{vmid}", "serial": f"Gell-{vmid:08X}",
            "uuid": "00000000-0000-0000-0000-000000000000",
            "os_build": "26100", "dsreg": {"AzureAdJoined": True},
        },
        ad_search=lambda dn, name: [
            {"distinguishedName": f"CN={name},OU=Devices,{dn}"},
        ],
        graph_find_entra_device=lambda n: [{"displayName": n,
                                            "trustType": "ServerAd"}],
        graph_find_intune_device=lambda s: [{"serialNumber": s,
                                             "complianceState": "compliant"}],
    )
    sweep_id = device_monitor.sweep(ctx)
    assert sweep_id >= 1

    # Only the autopilot-tagged VM got a row.
    rows = device_history_db.latest_per_vmid(db)
    assert [r["vmid"] for r in rows] == [116]
    r = rows[0]
    # Name comes from the Proxmox config dict, not the VM list entry —
    # the config is canonical.
    assert r["pve"]["name"] == "VM-116"
    assert r["probe"]["ad_found"] == 1
    assert r["probe"]["ad_match_count"] == 1
    assert r["probe"]["entra_found"] == 1
    assert r["probe"]["intune_found"] == 1
    ad = json.loads(r["probe"]["ad_matches_json"])
    assert ad[0]["source_ou_dn"] == "OU=WorkspaceLabs,DC=home,DC=gell,DC=one"


def test_sweep_includes_vmids_from_extra_in_scope_even_without_tag(db):
    from web import device_history_db, device_monitor
    vms = [{"vmid": 999, "name": "Gell-LEGACY", "node": "pve2",
            "status": "running", "tags": "other"}]
    ctx = _make_ctx(
        db,
        list_pve_vms=lambda: vms,
        fetch_pve_config=lambda vmid, node: _fake_config(vmid),
        fetch_guest_details=lambda vmid, node: None,
        ad_search=lambda dn, name: [],
    )
    sweep_id = device_monitor.sweep(ctx, extra_in_scope_vmids={999})
    rows = device_history_db.latest_per_vmid(db)
    assert [r["vmid"] for r in rows] == [999]


def test_sweep_records_pve_list_failure_on_sweep_row(db):
    from web import device_history_db, device_monitor

    def boom():
        raise ConnectionError("pve down")

    ctx = _make_ctx(db, list_pve_vms=boom)
    sweep_id = device_monitor.sweep(ctx)
    # No per-VM rows written.
    assert device_history_db.latest_per_vmid(db) == []
    # Sweep row carries the error.
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT errors_json FROM monitoring_sweeps WHERE id = ?",
        (sweep_id,),
    ).fetchone()
    errors = json.loads(row[0])
    assert "pve_list" in errors
    assert "ConnectionError" in errors["pve_list"]


def test_sweep_records_per_source_errors_on_probe_row(db):
    """When Graph / LDAP fail on one VM, the probe row still gets
    written — with the failure captured in probe_errors_json."""
    from web import device_history_db, device_monitor
    vms = [{"vmid": 7, "name": "Gell-TEST", "node": "pve2",
            "status": "running", "tags": "autopilot"}]
    ctx = _make_ctx(
        db,
        list_pve_vms=lambda: vms,
        fetch_pve_config=lambda vmid, node: _fake_config(vmid),
        fetch_guest_details=lambda vmid, node: {
            "win_name": "GELL-TEST", "serial": "SERIAL7",
        },
        ad_search=lambda dn, name: [],  # empty, not error
        graph_find_entra_device=lambda n: (_ for _ in ()).throw(RuntimeError("graph 503")),
        graph_find_intune_device=lambda s: (_ for _ in ()).throw(RuntimeError("intune 503")),
    )
    device_monitor.sweep(ctx)
    row = device_history_db.latest_device_probe(db, 7)
    assert row is not None
    errs = json.loads(row["probe_errors_json"])
    assert "graph 503" in errs["entra"]
    assert "intune 503" in errs["intune"]
    assert row["ad_found"] == 0
    assert row["entra_found"] == 0


def test_sweep_skips_directory_probes_when_vm_stopped(db):
    """Stopped VM → PVE snapshot still taken, but AD/Entra/Intune
    skipped (no live guest to ask for serial) and probe_errors_json
    records why."""
    from web import device_history_db, device_monitor
    vms = [{"vmid": 8, "name": "Gell-STOP", "node": "pve2",
            "status": "stopped", "tags": "autopilot"}]
    ad_called = []
    ctx = _make_ctx(
        db,
        list_pve_vms=lambda: vms,
        fetch_pve_config=lambda vmid, node: _fake_config(vmid),
        fetch_guest_details=lambda vmid, node:
            (_ for _ in ()).throw(AssertionError("should not call")),
        ad_search=lambda dn, name: ad_called.append(name) or [],
    )
    device_monitor.sweep(ctx)
    # AD still called (we have the VM name) — that's deliberate, per
    # spec: stopped VMs are probed by last-known name.
    assert ad_called == ["Gell-STOP"]
    probe = device_history_db.latest_device_probe(db, 8)
    errs = json.loads(probe["probe_errors_json"])
    assert "vm-not-running" in errs["guest"]


def test_sweep_finalizes_vm_count(db):
    from web import device_history_db, device_monitor
    vms = [
        {"vmid": i, "name": f"A{i}", "node": "pve2",
         "status": "running", "tags": "autopilot"}
        for i in (1, 2, 3)
    ]
    ctx = _make_ctx(
        db,
        list_pve_vms=lambda: vms,
        fetch_pve_config=lambda vmid, node: _fake_config(vmid),
        fetch_guest_details=lambda vmid, node: None,
        ad_search=lambda dn, name: [],
    )
    sweep_id = device_monitor.sweep(ctx)
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT vm_count, ended_at FROM monitoring_sweeps WHERE id = ?",
        (sweep_id,),
    ).fetchone()
    assert row[0] == 3
    assert row[1] is not None
