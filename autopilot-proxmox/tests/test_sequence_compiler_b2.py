"""Phase B.2 compiler handler tests: local_admin, join_ad_domain, rename_computer."""
import pytest


def _make_sequence(steps):
    return {
        "id": 1, "name": "S", "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "steps": [
            {"id": i + 1, "sequence_id": 1, "order_index": i,
             "step_type": s["step_type"], "params": s.get("params", {}),
             "enabled": s.get("enabled", True)}
            for i, s in enumerate(steps)
        ],
    }


def _resolver(mapping):
    def _r(cid):
        return mapping.get(int(cid))
    return _r


# ---------------------------------------------------------------------------
# local_admin
# ---------------------------------------------------------------------------


def test_local_admin_emits_user_accounts_and_auto_logon():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin", "params": {"credential_id": 7}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        7: {"username": "Administrator", "password": "hunter2!!"},
    }))
    ua = result.unattend_blocks["oobe_user_accounts"]
    al = result.unattend_blocks["oobe_auto_logon"]
    assert "<Name>Administrator</Name>" in ua
    assert "<Value>hunter2!!</Value>" in ua
    assert "<Group>Administrators</Group>" in ua
    assert "<Username>Administrator</Username>" in al
    assert "<Value>hunter2!!</Value>" in al
    assert "<LogonCount>1</LogonCount>" in al


def test_local_admin_escapes_xml_special_chars_in_password():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin", "params": {"credential_id": 1}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        1: {"username": "Admin", "password": "p<a&ss>\""},
    }))
    ua = result.unattend_blocks["oobe_user_accounts"]
    assert "&lt;" in ua and "&amp;" in ua and "&gt;" in ua
    # Plaintext special chars must not leak into the XML.
    assert "p<a&ss>" not in ua


def test_local_admin_autologon_opt_out():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin",
         "params": {"credential_id": 1, "autologon": False}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        1: {"username": "Admin", "password": "x"},
    }))
    assert "oobe_user_accounts" in result.unattend_blocks
    assert "oobe_auto_logon" not in result.unattend_blocks


def test_local_admin_requires_credential_id():
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "local_admin", "params": {}}])
    with pytest.raises(sequence_compiler.CredentialMissing) as exc:
        sequence_compiler.compile(seq, resolve_credential=_resolver({}))
    assert "credential_id" in str(exc.value)


def test_local_admin_requires_resolver_when_credential_present():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin", "params": {"credential_id": 1}},
    ])
    with pytest.raises(sequence_compiler.CredentialMissing) as exc:
        sequence_compiler.compile(seq)
    assert "resolver" in str(exc.value)


def test_local_admin_errors_when_credential_deleted():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin", "params": {"credential_id": 99}},
    ])
    with pytest.raises(sequence_compiler.CredentialMissing) as exc:
        sequence_compiler.compile(seq, resolve_credential=_resolver({}))
    assert "99" in str(exc.value)


def test_local_admin_errors_when_password_missing():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin", "params": {"credential_id": 1}},
    ])
    with pytest.raises(sequence_compiler.CredentialMissing):
        sequence_compiler.compile(seq, resolve_credential=_resolver({
            1: {"username": "Admin", "password": ""},
        }))


# ---------------------------------------------------------------------------
# join_ad_domain
# ---------------------------------------------------------------------------


def test_join_ad_domain_emits_identification_block():
    """A credential stored as ``DOMAIN\\user`` must land in the
    unattend with Domain=DOMAIN and Username=user separated — Windows
    concatenates them, so leaving `DOMAIN\\user` in <Username> plus
    <Domain>target-domain</Domain> produces 'target\\DOMAIN\\user'
    and djoin fails with ERROR_BAD_USERNAME (0x89a)."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 3}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        3: {"domain_fqdn": "home.gell.com", "username": "home\\joiner",
            "password": "P@ss", "ou_hint": "OU=Workstations,DC=home,DC=gell,DC=com"},
    }))
    idf = result.unattend_blocks["specialize_identification"]
    # Credential's DOMAIN\user format → Domain=home, Username=joiner.
    assert "<Domain>home</Domain>" in idf
    assert "<Username>joiner</Username>" in idf
    assert "<Password>P@ss</Password>" in idf
    assert "<JoinDomain>home.gell.com</JoinDomain>" in idf
    # OU hint fell through when the step didn't set ou_path explicitly.
    assert "<MachineObjectOU>OU=Workstations,DC=home,DC=gell,DC=com</MachineObjectOU>" in idf


def test_join_ad_domain_parses_upn_username():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 3}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        3: {"domain_fqdn": "home.gell.com",
            "username": "joiner@home.gell.com", "password": "P@ss"},
    }))
    idf = result.unattend_blocks["specialize_identification"]
    # UPN form → Domain=home.gell.com, Username=joiner
    assert "<Domain>home.gell.com</Domain>" in idf
    assert "<Username>joiner</Username>" in idf


def test_join_ad_domain_bare_username_defaults_credential_domain_to_join_domain():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 3}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        3: {"domain_fqdn": "home.gell.com", "username": "joiner",
            "password": "P@ss"},
    }))
    idf = result.unattend_blocks["specialize_identification"]
    # Bare username → credential <Domain> defaults to the join target.
    assert "<Domain>home.gell.com</Domain>" in idf
    assert "<Username>joiner</Username>" in idf


def test_join_ad_domain_step_ou_path_overrides_credential_hint():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain",
         "params": {"credential_id": 3, "ou_path": "OU=Lab,DC=home,DC=gell,DC=com"}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        3: {"domain_fqdn": "home.gell.com", "username": "u", "password": "p",
            "ou_hint": "OU=Workstations,DC=home,DC=gell,DC=com"},
    }))
    idf = result.unattend_blocks["specialize_identification"]
    assert "<MachineObjectOU>OU=Lab,DC=home,DC=gell,DC=com</MachineObjectOU>" in idf
    # The hint from the credential must NOT leak through when the step
    # explicitly set its own OU path.
    assert "OU=Workstations" not in idf


def test_join_ad_domain_omits_ou_when_neither_set():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 3}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        3: {"domain_fqdn": "home.gell.com", "username": "u", "password": "p"},
    }))
    idf = result.unattend_blocks["specialize_identification"]
    assert "<MachineObjectOU>" not in idf


def test_join_ad_domain_errors_on_incomplete_credential():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 3}},
    ])
    # Missing password → clean error, not KeyError or silent misconfig.
    with pytest.raises(sequence_compiler.CredentialMissing):
        sequence_compiler.compile(seq, resolve_credential=_resolver({
            3: {"domain_fqdn": "home.gell.com", "username": "u", "password": ""},
        }))


def test_join_ad_domain_does_not_add_to_causes_reboot_count():
    """The specialize→OOBE reboot that Windows already performs carries
    the domain join — the compiler must NOT insert an extra waiter."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 3}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        3: {"domain_fqdn": "d", "username": "u", "password": "p"},
    }))
    assert result.causes_reboot_count == 0


# ---------------------------------------------------------------------------
# rename_computer
# ---------------------------------------------------------------------------


def test_rename_computer_defaults_to_serial():
    """Renaming during specialize — not at first-logon — so the machine
    joins AD with the right name on the first try. A post-join
    Rename-Computer would need -DomainCredential; specialize avoids
    that complication entirely."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "rename_computer", "params": {}},
    ])
    result = sequence_compiler.compile(seq)
    # No FLC rename (that path fails silently on domain-joined hosts).
    assert result.first_logon_commands == []
    # Sentinel: inject_unattend.yml substitutes %AUTOPILOT_SERIAL% with
    # _vm_serial after mcopy extracts the cached floppy.
    assert result.unattend_blocks["specialize_computer_name"] == "%AUTOPILOT_SERIAL%"
    # Specialize rename is part of Windows's normal setup reboots.
    assert result.causes_reboot_count == 0


def test_rename_computer_pattern_emits_sentinel_placeholders():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "rename_computer",
         "params": {"name_source": "pattern", "pattern": "DEV-{vmid}-{serial}"}},
    ])
    result = sequence_compiler.compile(seq)
    assert (
        result.unattend_blocks["specialize_computer_name"]
        == "DEV-%AUTOPILOT_VMID%-%AUTOPILOT_SERIAL%"
    )
    assert result.first_logon_commands == []


def test_rename_computer_pattern_requires_nonempty_pattern():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "rename_computer",
         "params": {"name_source": "pattern", "pattern": ""}},
    ])
    with pytest.raises(sequence_compiler.CompilerError):
        sequence_compiler.compile(seq)


def test_rename_computer_xml_escapes_pattern():
    """Pattern lands inside an XML element; ampersands, angle-brackets
    must be entity-encoded so the unattend parses. (Literal apostrophes
    are fine inside element content.)"""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "rename_computer",
         "params": {"name_source": "pattern", "pattern": "A&B<c>"}},
    ])
    result = sequence_compiler.compile(seq)
    assert (
        result.unattend_blocks["specialize_computer_name"]
        == "A&amp;B&lt;c&gt;"
    )


# ---------------------------------------------------------------------------
# End-to-end: seed 2 "AD Domain Join — Local Admin"
# ---------------------------------------------------------------------------


def test_seeded_ad_domain_sequence_end_to_end():
    """Compile the seed-2 sequence with real-looking credentials and check
    every bucket is populated as the provision flow expects."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14"}},
        {"step_type": "local_admin", "params": {"credential_id": 1}},
        {"step_type": "join_ad_domain",
         "params": {"credential_id": 2,
                    "ou_path": "OU=Workstations,DC=home,DC=gell,DC=com"}},
        {"step_type": "rename_computer", "params": {"name_source": "serial"}},
    ])
    resolver = _resolver({
        1: {"username": "Administrator", "password": "LocalPass!!"},
        2: {"domain_fqdn": "home.gell.com",
            "username": "home\\joiner", "password": "JoinPass!!"},
    })
    result = sequence_compiler.compile(seq, resolve_credential=resolver)

    assert result.ansible_vars == {"vm_oem_profile": "lenovo-t14"}
    assert result.autopilot_enabled is False
    assert "oobe_user_accounts" in result.unattend_blocks
    assert "oobe_auto_logon" in result.unattend_blocks
    assert "specialize_identification" in result.unattend_blocks
    # rename_computer is handled in specialize via <ComputerName>, so it
    # emits no FLC; only the auto-logon finalizer (reboot to the login
    # screen) contributes a command.
    assert result.unattend_blocks["specialize_computer_name"] == "%AUTOPILOT_SERIAL%"
    assert len(result.first_logon_commands) == 1
    assert result.causes_reboot_count == 1
    final = result.first_logon_commands[-1]
    assert "logon screen" in final["description"].lower()
    assert "shutdown" in final["command"].lower()


def test_autologon_finalizer_appends_reboot_to_login_screen():
    """A sequence whose only mutation is a local_admin with autologon
    ends at the login screen — the compiler adds one FirstLogonCommand
    that does `shutdown /r` after all others."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin", "params": {"credential_id": 1}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        1: {"username": "Administrator", "password": "x"},
    }))
    assert len(result.first_logon_commands) == 1
    assert result.causes_reboot_count == 1
    assert "logon screen" in result.first_logon_commands[0]["description"].lower()


def test_no_finalizer_when_autologon_disabled():
    """If autologon is explicitly off, FirstLogonCommands don't run,
    so appending a reboot-to-login finalizer would wait forever. Skip it."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "local_admin",
         "params": {"credential_id": 1, "autologon": False}},
    ])
    result = sequence_compiler.compile(seq, resolve_credential=_resolver({
        1: {"username": "Administrator", "password": "x"},
    }))
    assert result.first_logon_commands == []
    assert result.causes_reboot_count == 0


def test_resolver_not_called_for_stepless_or_credless_steps():
    """Handlers that don't need credentials must not invoke the resolver
    (so a broken resolver doesn't break sequences that happen not to
    touch credentials)."""
    from web import sequence_compiler

    calls: list = []
    def _bomb(cid):
        calls.append(cid)
        raise AssertionError("resolver should not be called")

    seq = _make_sequence([
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14"}},
        {"step_type": "autopilot_entra", "params": {}},
        {"step_type": "rename_computer", "params": {}},
    ])
    sequence_compiler.compile(seq, resolve_credential=_bomb)
    assert calls == []
