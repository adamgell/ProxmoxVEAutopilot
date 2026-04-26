import pytest

from web.winpe_targets_db import (
    WinpeTarget,
    WinpeTargetsDb,
    UnknownVmError,
)


def _db(tmp_path):
    return WinpeTargetsDb(tmp_path / "index.db")


def test_init_creates_table(tmp_path):
    db = _db(tmp_path)
    assert db.list_uuids() == []


def test_register_and_lookup(tmp_path):
    db = _db(tmp_path)
    db.register(
        vm_uuid="11111111-2222-3333-4444-555555555555",
        install_wim_sha="a" * 64,
        template_id="win11-arm64-baseline",
        params={"computer_name": "AUTOPILOT-X1", "oem_profile": "Lenovo-ThinkPad"},
    )
    target = db.lookup("11111111-2222-3333-4444-555555555555")
    assert target is not None
    assert target.install_wim_sha == "a" * 64
    assert target.template_id == "win11-arm64-baseline"
    assert target.params == {"computer_name": "AUTOPILOT-X1", "oem_profile": "Lenovo-ThinkPad"}


def test_lookup_unknown_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.lookup("00000000-0000-0000-0000-000000000000") is None


def test_register_is_upsert_on_uuid(tmp_path):
    db = _db(tmp_path)
    db.register(vm_uuid="u1", install_wim_sha="a" * 64, template_id="t1", params={"k": 1})
    db.register(vm_uuid="u1", install_wim_sha="b" * 64, template_id="t2", params={"k": 2})
    target = db.lookup("u1")
    assert target.install_wim_sha == "b" * 64
    assert target.template_id == "t2"
    assert target.params == {"k": 2}
    assert len(db.list_uuids()) == 1


def test_touch_last_manifest_at(tmp_path):
    db = _db(tmp_path)
    db.register(vm_uuid="u1", install_wim_sha="a" * 64, template_id="t1", params={})
    before = db.lookup("u1").last_manifest_at
    assert before is None
    db.touch_last_manifest_at("u1")
    after = db.lookup("u1").last_manifest_at
    assert after is not None and after.endswith("Z")


def test_touch_unknown_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(UnknownVmError):
        db.touch_last_manifest_at("u-does-not-exist")
