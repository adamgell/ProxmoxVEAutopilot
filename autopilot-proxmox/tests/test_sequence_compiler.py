"""Tests for web.sequence_compiler — resolves a task sequence into Ansible vars."""
import pytest


def _make_sequence(steps, *, name="S", is_default=False, produces_hash=True):
    """Build a sequence dict in the shape get_sequence() returns."""
    return {
        "id": 1,
        "name": name,
        "description": "",
        "is_default": is_default,
        "produces_autopilot_hash": produces_hash,
        "steps": [
            {"id": i + 1, "sequence_id": 1, "order_index": i,
             "step_type": step["step_type"], "params": step.get("params", {}),
             "enabled": step.get("enabled", True)}
            for i, step in enumerate(steps)
        ],
    }


def test_empty_sequence_returns_empty_dict():
    from web import sequence_compiler
    result = sequence_compiler.compile(_make_sequence([]))
    assert result.ansible_vars == {}
    assert result.autopilot_enabled is False


def test_set_oem_hardware_produces_vm_oem_profile():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "dell-latitude-5540"}},
    ])
    result = sequence_compiler.compile(seq)
    assert result.ansible_vars["vm_oem_profile"] == "dell-latitude-5540"


def test_set_oem_hardware_blank_profile_is_absent():
    """A blank oem_profile (the seeded default uses '') inherits vars.yml —
    the compiler must NOT emit an empty string which would override."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware", "params": {"oem_profile": ""}},
    ])
    result = sequence_compiler.compile(seq)
    assert "vm_oem_profile" not in result.ansible_vars


def test_disabled_step_is_ignored():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "dell-latitude-5540"}, "enabled": False},
    ])
    result = sequence_compiler.compile(seq)
    assert "vm_oem_profile" not in result.ansible_vars


def test_unknown_step_type_raises():
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "bogus_step", "params": {}}])
    with pytest.raises(sequence_compiler.UnknownStepType) as exc:
        sequence_compiler.compile(seq)
    assert "bogus_step" in str(exc.value)


def test_hybrid_stub_refuses_to_compile():
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "autopilot_hybrid", "params": {}}])
    with pytest.raises(sequence_compiler.StepNotImplemented) as exc:
        sequence_compiler.compile(seq)
    assert "autopilot_hybrid" in str(exc.value)


def test_autopilot_entra_sets_flag_and_var():
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "autopilot_entra", "params": {}}])
    result = sequence_compiler.compile(seq)
    assert result.autopilot_enabled is True
    assert result.ansible_vars["autopilot_enabled"] == "true"


def test_seeded_entra_default_compiles_to_expected_vars():
    """Byte-identical check: the Phase A seed 'Entra Join (default)' must
    produce exactly these Ansible vars."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware", "params": {"oem_profile": ""}},
        {"step_type": "local_admin",
         "params": {"credential_id": 1}, "enabled": False},
        {"step_type": "autopilot_entra", "params": {}},
    ], is_default=True)
    result = sequence_compiler.compile(seq)
    assert "vm_oem_profile" not in result.ansible_vars
    assert result.ansible_vars == {"autopilot_enabled": "true"}
    assert result.autopilot_enabled is True


def test_multiple_steps_merge_vars():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14"}},
        {"step_type": "autopilot_entra", "params": {}},
    ])
    result = sequence_compiler.compile(seq)
    assert result.ansible_vars == {
        "vm_oem_profile": "lenovo-t14",
        "autopilot_enabled": "true",
    }


def test_precedence_ui_over_sequence_over_varsyml():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "dell-latitude-5540"}},
    ])
    compiled = sequence_compiler.compile(seq)

    # UI override wins
    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={"vm_oem_profile": "lenovo-t14"},
        vars_yml={"vm_oem_profile": "generic-desktop"},
    )
    assert resolved["vm_oem_profile"] == "lenovo-t14"

    # Blank UI falls through to sequence
    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={"vm_oem_profile": ""},
        vars_yml={"vm_oem_profile": "generic-desktop"},
    )
    assert resolved["vm_oem_profile"] == "dell-latitude-5540"

    # Missing key in UI also falls through
    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={},
        vars_yml={"vm_oem_profile": "generic-desktop"},
    )
    assert resolved["vm_oem_profile"] == "dell-latitude-5540"


def test_precedence_sequence_missing_falls_through_to_varsyml():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware", "params": {"oem_profile": ""}},
    ])
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={},
        vars_yml={"vm_oem_profile": "generic-desktop"},
    )
    assert resolved["vm_oem_profile"] == "generic-desktop"


def test_precedence_autopilot_enabled_from_sequence_wins():
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "autopilot_entra", "params": {}}])
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled, form_overrides={}, vars_yml={"autopilot_skip": "true"},
    )
    assert resolved["autopilot_enabled"] == "true"


def test_precedence_empty_sequence_preserves_legacy_varsyml():
    from web import sequence_compiler
    seq = _make_sequence([])
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled, form_overrides={}, vars_yml={},
    )
    assert "autopilot_enabled" not in resolved
