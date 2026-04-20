"""Byte-identical guard for the unattend template.

Rendering the Phase A seed-1 "Entra Join (default)" sequence through
the compiler + renderer must produce output that exactly matches
``files/autounattend.xml``. Any drift breaks every existing install.
"""
from pathlib import Path


_FILES_DIR = Path(__file__).resolve().parent.parent / "files"


def test_default_sequence_renders_byte_identical_to_static_file():
    from web import sequence_compiler, unattend_renderer

    seed = {
        "id": 1, "name": "Entra Join (default)", "description": "",
        "is_default": True, "produces_autopilot_hash": True,
        "steps": [
            {"id": 1, "sequence_id": 1, "order_index": 0,
             "step_type": "set_oem_hardware",
             "params": {"oem_profile": ""}, "enabled": True},
            # local_admin step exists in the seed but is disabled by
            # default so defaults render unchanged.
            {"id": 2, "sequence_id": 1, "order_index": 1,
             "step_type": "local_admin",
             "params": {"credential_id": 1}, "enabled": False},
            {"id": 3, "sequence_id": 1, "order_index": 2,
             "step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }
    compiled = sequence_compiler.compile(seed)
    rendered = unattend_renderer.render_unattend(compiled)

    expected = (_FILES_DIR / "autounattend.xml").read_text()
    assert rendered == expected, (
        "unattend template drifted from files/autounattend.xml. "
        "Either update the template + defaults together, or fix the "
        "regression."
    )


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


def test_rename_computer_appends_first_logon_commands():
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

    # New command lands with Order=4 (after the three hardcoded defaults).
    assert "<Order>4</Order>" in out
    assert "Rename computer and reboot" in out
    # XML special chars in the PowerShell command get escaped so the
    # XML stays well-formed.
    assert "&quot;" not in out  # Quote inside <CommandLine> stays literal.
    # But & < > should be escaped.
    assert "<CommandLine>powershell.exe" in out


def test_no_sequence_steps_still_renders_default_bytes():
    """Empty steps list → identical to the static file too."""
    from web import sequence_compiler, unattend_renderer

    seq = {
        "id": 99, "name": "empty", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [],
    }
    compiled = sequence_compiler.compile(seq)
    rendered = unattend_renderer.render_unattend(compiled)
    expected = (_FILES_DIR / "autounattend.xml").read_text()
    assert rendered == expected
