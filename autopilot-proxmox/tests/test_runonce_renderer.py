"""Tests for web.runonce_renderer — resolves RunOnce step templates."""
from pathlib import Path

import pytest


def _default_brand():
    return {"name": "ProxmoxVEAutopilot",
            "event_source": "ProxmoxVEAutopilot",
            "registry_root": r"HKLM:\SOFTWARE\ProxmoxVEAutopilot"}


def test_render_join_ad_domain_substitutes_credential():
    from web import runonce_renderer
    step = {
        "step_type": "join_ad_domain",
        "ps_template": (
            "Add-Computer -DomainName '{{ cred.domain_fqdn | ps_escape }}' "
            "-Credential (New-Object PSCredential('{{ cred.username | ps_escape }}', "
            "(ConvertTo-SecureString '{{ cred.password | ps_escape }}' -AsPlainText -Force))) "
            "-OUPath '{{ params.ou_path | ps_escape }}' -Restart"
        ),
        "credential_id": 1,
        "params": {"ou_path": "OU=Workstations,DC=example,DC=local"},
    }
    creds_resolver = lambda cid: {
        "domain_fqdn": "example.local",
        "username": "EXAMPLE\\svc_join",
        "password": "p@ssword",
    } if cid == 1 else None
    rendered = runonce_renderer.render_step(step, creds_resolver=creds_resolver,
                                             vm_context={}, brand=_default_brand())
    assert "example.local" in rendered
    assert "EXAMPLE\\svc_join" in rendered
    assert "p@ssword" in rendered
    assert "OU=Workstations,DC=example,DC=local" in rendered


def test_render_rename_computer_expands_vm_tokens():
    """The `pattern` param can contain {serial}/{vmid}/{group_tag} tokens
    that get expanded at render time using vm_context."""
    from web import runonce_renderer
    step = {
        "step_type": "rename_computer",
        "ps_template": "Rename-Computer -NewName '{{ params.pattern | ps_escape }}' -Force -Restart",
        "credential_id": None,
        "params": {"pattern": "{serial}"},
    }
    rendered = runonce_renderer.render_step(
        step, creds_resolver=lambda cid: None,
        vm_context={"serial": "ABC-1234", "vmid": 105, "group_tag": ""},
        brand=_default_brand(),
    )
    assert "'ABC-1234'" in rendered


def test_render_escapes_powershell_single_quotes_in_password():
    """PS single-quoted strings treat '' as escaped single quote. A password
    containing ' must be doubled to avoid breaking out of the literal."""
    from web import runonce_renderer
    step = {
        "step_type": "join_ad_domain",
        "ps_template": "ConvertTo-SecureString '{{ cred.password | ps_escape }}' -AsPlainText",
        "credential_id": 1,
        "params": {},
    }
    creds_resolver = lambda cid: {"password": "p'wn3d", "domain_fqdn": "",
                                   "username": ""}
    rendered = runonce_renderer.render_step(step, creds_resolver=creds_resolver,
                                             vm_context={}, brand=_default_brand())
    # The raw single quote must not appear; it must be doubled.
    assert "'p''wn3d'" in rendered


def test_render_raises_when_credential_lookup_fails():
    from web import runonce_renderer
    step = {
        "step_type": "join_ad_domain",
        "ps_template": "{{ cred.password | ps_escape }}",
        "credential_id": 999,
        "params": {},
    }
    with pytest.raises(runonce_renderer.RenderError) as exc:
        runonce_renderer.render_step(step, creds_resolver=lambda cid: None,
                                      vm_context={}, brand=_default_brand())
    assert "999" in str(exc.value)


def test_render_does_not_require_credentials_when_id_is_none():
    from web import runonce_renderer
    step = {
        "step_type": "rename_computer",
        "ps_template": "Rename-Computer -NewName '{{ params.pattern | ps_escape }}'",
        "credential_id": None,
        "params": {"pattern": "X"},
        "causes_reboot": False,
    }
    rendered = runonce_renderer.render_step(
        step, creds_resolver=lambda cid: None, vm_context={},
        brand=_default_brand())
    # Core action is present; rendered output includes the branding wrap.
    assert "Rename-Computer -NewName 'X'" in rendered


def test_write_step_scripts_writes_files_with_0600(tmp_path):
    """write_step_scripts takes a compiled sequence + resolver + context
    + brand, writes one .ps1 per step to dest_dir, returns file info."""
    from web import runonce_renderer
    steps = [
        {"step_type": "rename_computer",
         "ps_template": "echo '{{ params.pattern | ps_escape }}'",
         "credential_id": None,
         "params": {"pattern": "hostname"},
         "causes_reboot": True},
    ]
    infos = runonce_renderer.write_step_scripts(
        steps=steps,
        dest_dir=tmp_path,
        creds_resolver=lambda cid: None,
        vm_context={"serial": "ABC", "vmid": 1, "group_tag": "",
                    "sequence_id": 7, "sequence_name": "test-seq"},
        brand=_default_brand(),
    )
    assert len(infos) == 1
    info = infos[0]
    assert info["step_type"] == "rename_computer"
    assert info["causes_reboot"] is True
    path = Path(info["path"])
    assert path.exists()
    content = path.read_text()
    # Core action made it through
    assert "echo 'hostname'" in content
    # Branding envelope is present
    assert "ProxmoxVEAutopilot" in content
    assert path.stat().st_mode & 0o777 == 0o600


def test_pattern_tokens_expand_in_params_not_just_template():
    """Pattern string can use {serial}/{vmid}/{group_tag} tokens. These
    expand BEFORE Jinja so the PS template sees a plain string."""
    from web import runonce_renderer
    step = {
        "step_type": "rename_computer",
        "ps_template": "Rename-Computer -NewName '{{ params.pattern | ps_escape }}'",
        "credential_id": None,
        "params": {"pattern": "{serial}-{vmid}"},
        "causes_reboot": True,
    }
    rendered = runonce_renderer.render_step(
        step, creds_resolver=lambda cid: None,
        vm_context={"serial": "ABC", "vmid": 105, "group_tag": "",
                    "sequence_id": 1, "sequence_name": "S"},
        brand=_default_brand(),
    )
    assert "'ABC-105'" in rendered


# --- Branding envelope tests ---

def test_branding_envelope_contains_header_event_log_and_registry(tmp_path):
    """Every rendered script carries: ASCII header with brand name,
    New-EventLog setup, Write-EventLog for start (1001), Registry stamp
    init with Status=Running, and try/catch with success (1002) and
    failure (1099) branches."""
    from web import runonce_renderer
    step = {
        "step_type": "join_ad_domain",
        "ps_template": "# core action here",
        "credential_id": None,
        "params": {},
        "causes_reboot": True,
    }
    rendered = runonce_renderer.render_step(
        step, creds_resolver=lambda cid: None,
        vm_context={"serial": "ABC", "vmid": 42,
                    "sequence_id": 3, "sequence_name": "my-seq"},
        brand=_default_brand(),
    )
    # Header
    assert "# ================================================" in rendered
    assert "ProxmoxVEAutopilot — Task Sequence RunOnce Step" in rendered
    assert "Sequence:  my-seq" in rendered
    assert "Step:      join_ad_domain" in rendered
    assert "VMID:      42" in rendered
    # Event Log setup + 3 EventIds
    assert "New-EventLog" in rendered
    assert "EventId 1001" in rendered  # start
    assert "EventId 1002" in rendered  # success
    assert "EventId 1099" in rendered  # failure
    assert "'ProxmoxVEAutopilot'" in rendered  # event source
    # Registry stamp
    assert r"HKLM:\SOFTWARE\ProxmoxVEAutopilot\Provisioning\3\join_ad_domain" in rendered
    assert '"Status"' in rendered and 'Running' in rendered
    # Try/catch wraps the core
    assert "$ErrorActionPreference = 'Stop'" in rendered
    assert "try {" in rendered and "} catch {" in rendered
    assert "# core action here" in rendered
    # Reboot at end of success branch (causes_reboot=True)
    assert "Restart-Computer -Force" in rendered


def test_branding_envelope_omits_reboot_when_step_doesnt_need_it():
    from web import runonce_renderer
    step = {
        "step_type": "rename_computer",
        "ps_template": "# core",
        "credential_id": None,
        "params": {},
        "causes_reboot": False,
    }
    rendered = runonce_renderer.render_step(
        step, creds_resolver=lambda cid: None,
        vm_context={"sequence_id": 1, "sequence_name": "S"},
        brand=_default_brand(),
    )
    assert "Restart-Computer" not in rendered


def test_branding_respects_custom_brand_name():
    """A customer-white-labeled brand name flows through verbatim."""
    from web import runonce_renderer
    step = {
        "step_type": "rename_computer",
        "ps_template": "# core",
        "credential_id": None,
        "params": {},
        "causes_reboot": False,
    }
    brand = {"name": "AcmeOSD",
             "event_source": "AcmeOSD",
             "registry_root": r"HKLM:\SOFTWARE\AcmeOSD"}
    rendered = runonce_renderer.render_step(
        step, creds_resolver=lambda cid: None,
        vm_context={"sequence_id": 1, "sequence_name": "S"},
        brand=brand,
    )
    assert "AcmeOSD" in rendered
    assert "ProxmoxVEAutopilot" not in rendered
    assert r"HKLM:\SOFTWARE\AcmeOSD\Provisioning" in rendered
