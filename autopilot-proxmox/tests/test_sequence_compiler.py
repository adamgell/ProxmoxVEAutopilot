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


def test_set_oem_hardware_emits_chassis_type_override_when_set():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14", "chassis_type": 10}},
    ])
    result = sequence_compiler.compile(seq)
    assert result.ansible_vars["chassis_type_override"] == "10"


def test_set_oem_hardware_omits_chassis_type_when_missing():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware", "params": {"oem_profile": "lenovo-t14"}},
    ])
    result = sequence_compiler.compile(seq)
    assert "chassis_type_override" not in result.ansible_vars


def test_set_oem_hardware_ignores_zero_chassis_type():
    """0 means 'inherit from profile', not 'override with 0'."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14", "chassis_type": 0}},
    ])
    result = sequence_compiler.compile(seq)
    assert "chassis_type_override" not in result.ansible_vars


def test_precedence_chassis_type_form_wins_over_sequence_over_varsyml():
    """Spec §12: vars.yml < sequence step < provision-form field.
    When all three disagree on chassis_type_override, form wins."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14", "chassis_type": 10}},
    ])
    compiled = sequence_compiler.compile(seq)
    # Sanity: the sequence contributed 10
    assert compiled.ansible_vars["chassis_type_override"] == "10"

    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={"chassis_type_override": "31"},
        vars_yml={"chassis_type_override": "3"},
    )
    # Form value beats sequence beats vars.yml.
    assert resolved["chassis_type_override"] == "31"


def test_precedence_chassis_type_sequence_wins_when_form_blank():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14", "chassis_type": 10}},
    ])
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={},  # form blank → inherit from sequence
        vars_yml={"chassis_type_override": "3"},
    )
    assert resolved["chassis_type_override"] == "10"


def test_precedence_chassis_type_varsyml_wins_when_sequence_and_form_blank():
    from web import sequence_compiler
    seq = _make_sequence([])   # no chassis from sequence
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled,
        form_overrides={},
        vars_yml={"chassis_type_override": "3"},
    )
    assert resolved["chassis_type_override"] == "3"


# ---------------------------------------------------------------------------
# run_script + install_module — FirstLogonCommand-producing handlers
# ---------------------------------------------------------------------------

def _decode_encoded_ps(command: str) -> str:
    """Helper: pull the -EncodedCommand b64 payload out and decode it."""
    import base64
    marker = "-EncodedCommand "
    idx = command.index(marker) + len(marker)
    b64 = command[idx:].strip()
    return base64.b64decode(b64).decode("utf-16-le")


def test_run_script_emits_first_logon_command_with_encoded_body():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "run_script",
         "params": {"name": "firewall-off",
                    "script": "Set-NetFirewallProfile -Profile Domain -Enabled False"}},
    ])
    result = sequence_compiler.compile(seq)
    assert len(result.first_logon_commands) == 1
    flc = result.first_logon_commands[0]
    assert flc["description"] == "firewall-off"
    assert flc["command"].startswith("powershell.exe -NoProfile -ExecutionPolicy Bypass")
    assert "-EncodedCommand " in flc["command"]
    # The encoded payload round-trips to exactly what the operator wrote.
    assert _decode_encoded_ps(flc["command"]) == (
        "Set-NetFirewallProfile -Profile Domain -Enabled False"
    )
    # Reboot default is False — no cycle bump.
    assert result.causes_reboot_count == 0


def test_run_script_causes_reboot_flag_bumps_count():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "run_script",
         "params": {"script": "shutdown /r /t 5", "causes_reboot": True}},
    ])
    result = sequence_compiler.compile(seq)
    assert result.causes_reboot_count == 1


def test_run_script_empty_body_is_compile_error():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "run_script", "params": {"script": "   \n  "}},
    ])
    with pytest.raises(sequence_compiler.CompilerError):
        sequence_compiler.compile(seq)


def test_run_script_preserves_special_characters():
    """Quotes, newlines, ampersands in the script body must survive the
    XML → cmd.exe → PowerShell triple-parser through -EncodedCommand."""
    from web import sequence_compiler
    body = (
        "$x = 'hello \"world\"'\n"
        "Write-Host $x & echo done <nothing>\n"
        "if ($true) { 'nested ''quotes''' }"
    )
    seq = _make_sequence([
        {"step_type": "run_script", "params": {"script": body}},
    ])
    result = sequence_compiler.compile(seq)
    flc = result.first_logon_commands[0]
    # None of the metacharacters leak into the raw command line.
    assert "&" not in flc["command"]
    assert "<" not in flc["command"]
    # And the decoded payload is byte-identical to what we passed in.
    assert _decode_encoded_ps(flc["command"]) == body


def test_install_module_minimal_emits_flc():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "install_module", "params": {"module": "PSWindowsUpdate"}},
    ])
    result = sequence_compiler.compile(seq)
    flc = result.first_logon_commands[0]
    assert flc["description"] == "Install-Module PSWindowsUpdate"
    decoded = _decode_encoded_ps(flc["command"])
    # Contains the three bootstrap pre-reqs the renderer is responsible
    # for on a fresh Windows image.
    assert "Tls12" in decoded
    assert "Install-PackageProvider -Name NuGet" in decoded
    assert "Set-PSRepository -Name 'PSGallery'" in decoded
    # And the install invocation has the right defaults.
    assert "Install-Module -Name 'PSWindowsUpdate'" in decoded
    assert "-Repository 'PSGallery'" in decoded
    assert "-Scope 'AllUsers'" in decoded


def test_install_module_with_version_and_custom_scope():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "install_module",
         "params": {"module": "Az", "version": "11.2.0",
                    "repository": "PSGallery", "scope": "CurrentUser"}},
    ])
    result = sequence_compiler.compile(seq)
    flc = result.first_logon_commands[0]
    assert flc["description"] == "Install-Module Az 11.2.0"
    decoded = _decode_encoded_ps(flc["command"])
    assert "-RequiredVersion '11.2.0'" in decoded
    assert "-Scope 'CurrentUser'" in decoded


def test_install_module_requires_name():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "install_module", "params": {"module": ""}},
    ])
    with pytest.raises(sequence_compiler.CompilerError):
        sequence_compiler.compile(seq)


def test_install_module_does_not_cause_reboot():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "install_module", "params": {"module": "Az"}},
    ])
    result = sequence_compiler.compile(seq)
    assert result.causes_reboot_count == 0


def test_run_script_and_install_module_coexist_in_order():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "install_module", "params": {"module": "Az"}},
        {"step_type": "run_script",
         "params": {"name": "after-install", "script": "Connect-AzAccount"}},
    ])
    result = sequence_compiler.compile(seq)
    assert [c["description"] for c in result.first_logon_commands] == [
        "Install-Module Az", "after-install",
    ]
