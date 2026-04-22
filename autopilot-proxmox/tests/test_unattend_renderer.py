"""Contract tests for the per-clone unattend renderer.

Pre-Phase-B, sysprepped clones had no per-VM unattend and OOBE ran
fresh on first boot. The Phase B renderer originally defaulted to a
byte-identical copy of files/autounattend.xml — which carried the
template-build's <AutoLogon> block onto every clone, auto-completing
OOBE as Administrator and silently breaking Autopilot self-deploying
mode (Autopilot reads its config DURING OOBE; if OOBE is bypassed,
the JSON file sits unused on disk).

These tests pin the corrected contract: clones land at OOBE by
default. Sequences that explicitly want auto-logon (FLC chains, AD
domain join's local_admin step) opt in via _handle_local_admin which
sets oobe_user_accounts + oobe_auto_logon when the step is enabled.
"""
from pathlib import Path


_FILES_DIR = Path(__file__).resolve().parent.parent / "files"


def test_default_sequence_omits_autologin_so_oobe_stays_open_for_autopilot():
    """The Phase A seed-1 'Entra Join (default)' sequence (with
    local_admin disabled) must render an unattend with NO AutoLogon
    and NO UserAccounts blocks — that's what lets Autopilot
    self-deploy during a fresh OOBE on first boot of the clone."""
    from web import sequence_compiler, unattend_renderer

    seed = {
        "id": 1, "name": "Entra Join (default)", "description": "",
        "is_default": True, "produces_autopilot_hash": True,
        "steps": [
            {"id": 1, "sequence_id": 1, "order_index": 0,
             "step_type": "set_oem_hardware",
             "params": {"oem_profile": ""}, "enabled": True},
            {"id": 2, "sequence_id": 1, "order_index": 1,
             "step_type": "local_admin",
             "params": {"credential_id": 1}, "enabled": False},
            {"id": 3, "sequence_id": 1, "order_index": 2,
             "step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }
    compiled = sequence_compiler.compile(seed)
    rendered = unattend_renderer.render_unattend(compiled)

    # No AutoLogon block — OOBE must not be auto-completed.
    assert "<AutoLogon>" not in rendered
    assert "AutoAdminLogon" not in rendered
    # No baked-in Administrator account from the legacy default.
    assert "<Name>Administrator</Name>" not in rendered
    assert "Nsta1200!!" not in rendered
    # OOBE Hide* flags removed — clones run interactively starting at
    # the Region screen. Operator drives OOBE manually for hash
    # capture + Autopilot enrollment workflows.
    assert "<HideOnlineAccountScreens>" not in rendered
    assert "<HideEULAPage>" not in rendered
    # Region/keyboard auto-fill removed from oobeSystem so OOBE stops
    # at the very first screen (the WinPE-pass International-Core for
    # template install is unaffected).
    rendered_oobe = rendered.split('pass="oobeSystem"', 1)[-1]
    assert "Microsoft-Windows-International-Core" not in rendered_oobe


def test_local_admin_sequence_substitutes_user_accounts_and_auto_logon():
    from web import sequence_compiler, unattend_renderer

    seq = {
        "id": 2, "name": "with-local-admin", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [
            {"id": 1, "sequence_id": 2, "order_index": 0,
             "step_type": "local_admin",
             "params": {"credential_id": 7}, "enabled": True},
        ],
    }
    resolver = lambda cid: {
        "username": "svc_admin", "password": "DifferentPwd123!",
    }
    compiled = sequence_compiler.compile(seq, resolve_credential=resolver)
    out = unattend_renderer.render_unattend(compiled)

    assert "<Name>svc_admin</Name>" in out
    assert "<Value>DifferentPwd123!</Value>" in out
    assert "<Username>svc_admin</Username>" in out  # in auto-logon block
    # Default hardcoded values must not leak through.
    assert "Nsta1200!!" not in out
    assert "<Name>Administrator</Name>" not in out


def test_join_ad_domain_sequence_inserts_identification_component():
    from web import sequence_compiler, unattend_renderer

    seq = {
        "id": 3, "name": "with-join", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [
            {"id": 1, "sequence_id": 3, "order_index": 0,
             "step_type": "join_ad_domain",
             "params": {"credential_id": 5,
                        "ou_path": "OU=Lab,DC=home,DC=gell,DC=com"},
             "enabled": True},
        ],
    }
    resolver = lambda cid: {
        "domain_fqdn": "home.gell.com", "username": "home\\joiner",
        "password": "JoinPass",
    }
    compiled = sequence_compiler.compile(seq, resolve_credential=resolver)
    out = unattend_renderer.render_unattend(compiled)

    # Component wrapper present in the specialize pass.
    assert ('<component name="Microsoft-Windows-UnattendedJoin"'
            in out)
    assert "<JoinDomain>home.gell.com</JoinDomain>" in out
    assert "<MachineObjectOU>OU=Lab,DC=home,DC=gell,DC=com</MachineObjectOU>" in out


def test_rename_computer_sets_specialize_computer_name_with_sentinel():
    """rename_computer emits <ComputerName> into the specialize pass so
    the machine has the right name BEFORE join_ad_domain creates the
    computer object in AD. The value contains %AUTOPILOT_SERIAL% /
    %AUTOPILOT_VMID% sentinels that the clone role substitutes per-VM
    after extracting the cached floppy."""
    from web import sequence_compiler, unattend_renderer

    seq = {
        "id": 4, "name": "with-rename", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [
            {"id": 1, "sequence_id": 4, "order_index": 0,
             "step_type": "rename_computer",
             "params": {"name_source": "serial"}, "enabled": True},
        ],
    }
    compiled = sequence_compiler.compile(seq)
    out = unattend_renderer.render_unattend(compiled)

    assert "<ComputerName>%AUTOPILOT_SERIAL%</ComputerName>" in out
    # No FLC-rename — that path silently failed on domain-joined hosts
    # (needed -DomainCredential) and has been dropped.
    assert "Rename-Computer" not in out
    assert "Rename computer and reboot" not in out


def test_default_unattend_leaves_computer_name_wildcard():
    """Sequences without rename_computer fall back to <ComputerName>*</ComputerName>
    so Windows auto-generates WIN-<random> — keeps the byte-identical
    regression against files/autounattend.xml passing."""
    from web import sequence_compiler, unattend_renderer

    seq = {
        "id": 5, "name": "no-rename", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [],
    }
    compiled = sequence_compiler.compile(seq)
    out = unattend_renderer.render_unattend(compiled)
    assert "<ComputerName>*</ComputerName>" in out


def test_no_sequence_steps_renders_clean_oobe():
    """Empty steps list → no AutoLogon, no UserAccounts. Same contract
    as the default-sequence test above; this is the truly-empty
    canary."""
    from web import sequence_compiler, unattend_renderer

    seq = {
        "id": 99, "name": "empty", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [],
    }
    compiled = sequence_compiler.compile(seq)
    rendered = unattend_renderer.render_unattend(compiled)
    assert "<AutoLogon>" not in rendered
    assert "<UserAccounts>" not in rendered
    assert "<Name>Administrator</Name>" not in rendered
