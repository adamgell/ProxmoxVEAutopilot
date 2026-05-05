"""Tests for compile_winpe and CompiledWinPEPhase."""
import pytest


def test_compiled_winpe_phase_default_fields():
    from web.sequence_compiler import CompiledWinPEPhase
    p = CompiledWinPEPhase()
    assert p.actions == []
    assert p.requires_windows_iso is True
    assert p.requires_virtio_iso is False
    assert p.expected_reboot_count == 1
    assert p.autopilot_enabled is False


def test_compiled_winpe_phase_actions_is_independent_per_instance():
    from web.sequence_compiler import CompiledWinPEPhase
    a = CompiledWinPEPhase()
    b = CompiledWinPEPhase()
    a.actions.append({"kind": "x"})
    assert b.actions == []


def _seq(name="s", steps=None, autopilot_enabled=False, hash_phase="oobe"):
    """Build a minimal sequence dict matching sequences_db.get_sequence's shape."""
    return {
        "id": 1, "name": name, "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "target_os": "windows",
        "hash_capture_phase": hash_phase,
        "steps": steps or [],
    }


def test_compile_winpe_baseline_action_order():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    kinds = [a["kind"] for a in p.actions]
    assert kinds == [
        "partition_disk",
        "apply_wim",
        "bake_boot_entry",
        "stage_unattend",
    ]


def test_compile_winpe_inserts_stage_autopilot_config_when_enabled():
    from web.sequence_compiler import compile_winpe
    seq = _seq(steps=[{
        "step_type": "autopilot_entra",
        "params_json": "{}",
        "enabled": True, "order_index": 0,
    }])
    p = compile_winpe(seq)
    kinds = [a["kind"] for a in p.actions]
    assert "stage_autopilot_config" in kinds
    # Must come after apply_wim (writes to V:\) but before bake_boot_entry
    assert kinds.index("stage_autopilot_config") > kinds.index("apply_wim")
    assert kinds.index("stage_autopilot_config") < kinds.index("bake_boot_entry")


def test_compile_winpe_omits_stage_autopilot_config_when_not_enabled():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    kinds = [a["kind"] for a in p.actions]
    assert "stage_autopilot_config" not in kinds


def test_compile_winpe_appends_capture_hash_when_phase_winpe():
    from web.sequence_compiler import compile_winpe
    seq = _seq(hash_phase="winpe")
    seq["produces_autopilot_hash"] = True
    p = compile_winpe(seq)
    kinds = [a["kind"] for a in p.actions]
    # capture_hash runs first because it must read SMBIOS before disk is touched
    assert kinds[0] == "capture_hash"


def test_compile_winpe_omits_capture_hash_when_phase_oobe():
    from web.sequence_compiler import compile_winpe
    seq = _seq(hash_phase="oobe")
    seq["produces_autopilot_hash"] = True
    p = compile_winpe(seq)
    kinds = [a["kind"] for a in p.actions]
    assert "capture_hash" not in kinds


def test_compile_winpe_partition_disk_carries_layout_param():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    pd = next(a for a in p.actions if a["kind"] == "partition_disk")
    assert pd["params"]["layout"] == "recovery_before_c"


def test_compile_winpe_avoids_installed_windows_virtio_driver_injection():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    kinds = [a["kind"] for a in p.actions]
    assert "inject_drivers" not in kinds
    assert "validate_boot_drivers" not in kinds


def test_compile_winpe_marks_autopilot_when_enabled():
    """The compiler signals autopilot via the presence of the
    stage_autopilot_config action; the actual JSON bytes are loaded
    by the Flask endpoint from autopilot_config_path at request time
    (not embedded in the compiled phase, since the file may change
    between compile and serve)."""
    from web.sequence_compiler import compile_winpe
    seq = _seq(steps=[{
        "step_type": "autopilot_entra",
        "params_json": "{}",
        "enabled": True, "order_index": 0,
    }])
    p = compile_winpe(seq)
    assert any(a["kind"] == "stage_autopilot_config" for a in p.actions)


def test_create_sequence_persists_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="winpe-seq", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False, hash_capture_phase="winpe",
    )
    seq = sequences_db.get_sequence(db, sid)
    assert seq["hash_capture_phase"] == "winpe"


def test_update_sequence_changes_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="oobe-seq", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False,
    )
    sequences_db.update_sequence(
        db, seq_id=sid,
        hash_capture_phase="winpe",
    )
    seq = sequences_db.get_sequence(db, sid)
    assert seq["hash_capture_phase"] == "winpe"


def test_create_sequence_rejects_unknown_hash_capture_phase(tmp_path):
    import pytest
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    with pytest.raises(ValueError):
        sequences_db.create_sequence(
            db, name="bad", description="",
            target_os="windows", produces_autopilot_hash=False,
            is_default=False, hash_capture_phase="bogus",
        )


def test_duplicate_sequence_preserves_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="src-winpe", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False, hash_capture_phase="winpe",
    )
    new_id = sequences_db.duplicate_sequence(
        db, sid, new_name="src-winpe-copy",
    )
    assert sequences_db.get_sequence(db, new_id)["hash_capture_phase"] == "winpe"


def test_api_sequences_create_persists_hash_capture_phase(web_client):
    r = web_client.post(
        "/api/sequences",
        json={
            "name": "winpe-via-api",
            "description": "",
            "target_os": "windows",
            "is_default": False,
            "produces_autopilot_hash": True,
            "hash_capture_phase": "winpe",
            "steps": [],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = web_client.get(f"/api/sequences/{body['id']}")
    assert r2.json()["hash_capture_phase"] == "winpe"
