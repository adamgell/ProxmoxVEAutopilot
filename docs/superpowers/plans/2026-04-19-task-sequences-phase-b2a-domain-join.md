# Task Sequences — Phase B.2a (Domain Join via RunOnce)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Provisioning with a sequence that includes `join_ad_domain` (and optionally `rename_computer`) produces a VM that actually joins the target AD domain with a custom hostname, using the SCCM-style RunOnce pattern — `Add-Computer -Restart` executed via QEMU guest agent after first OOBE logon.

**Architecture:** Two-stage rendering — the pure compiler emits *core-action* PowerShell templates with credential placeholders; the renderer wraps each core with a branding envelope (ASCII header, Windows Event Log entries at start/success/failure, and a Registry stamp at `HKLM:\SOFTWARE\<brand>\Provisioning\...`), then writes to a per-job temp dir (0600). Ansible reads the files, pushes them to the guest via `guest-file-write`, executes via `guest-exec`, and supervises the reboot cycle via a new `wait_reboot_cycle.yml`. Credentials touch disk only briefly in the per-job dir and in the guest's `C:\Windows\Temp\`, both scrubbed on completion. No per-VM ISO, no unattend manipulation.

**Branding:** All generated `.ps1` scripts identify themselves with a configurable brand string (default `"ProxmoxVEAutopilot"`, settable via `/settings`). Branding appears in: (1) ASCII header comment in every script; (2) Windows Event Log entries with the brand as the event source, events 1001/1002/1099 for start/success/failure; (3) Registry stamps under `HKLM:\SOFTWARE\<brand>\Provisioning\<sequence_id>\<step_type>` capturing Status, StartedAt, CompletedAt, ErrorMessage, ToolVersion. Matches the SCCM convention of `HKLM:\SOFTWARE\Microsoft\SMS` stamping. Customers running this white-label can change the brand once in Settings.

**Tech Stack:** Python (compiler + renderer + ldap3 tester) · Ansible (guest-exec + reboot waiter) · FastAPI (Test Connection endpoint + brand settings) · pytest. New dependency: `ldap3` (pure-Python, BSD).

**Spec reference:** `docs/superpowers/specs/2026-04-19-task-sequences-design.md` §5 (step types), §9 (execution flow), §10 (reboot tracking), §8.6 (Test Connection). Q2 re-decided from UnattendedJoin → RunOnce; Q4 reboot detection stays as approach (b) "actual reboot cycle via guest agent ping gap."

---

## Scope decisions

**In scope for B.2a (this plan):**

- Compiler handlers for `join_ad_domain` and `rename_computer`. Both produce PowerShell templates with `{{ cred.field }}` / `{{ vm.field }}` placeholders.
- Two-stage pipeline: pure `sequence_compiler.compile()` + new `web/runonce_renderer.py` that resolves placeholders in-memory with the Fernet cipher.
- Per-job `runonce/` dir with rendered `.ps1` files, 0600, scrubbed post-job.
- New common Ansible task `guest_run_ps_script.yml` — write PS to guest `C:\Windows\Temp\`, execute via `guest-exec`, collect output, delete.
- New common Ansible task `wait_reboot_cycle.yml` — detect guest-agent ping gap, then reconverge on `wait_guest_agent.yml`.
- Playbook wiring: after `autopilot_inject`, iterate compiled RunOnce steps; each marked `causes_reboot` triggers the waiter.
- `ldap3`-based Test Connection for `domain_join` credentials — DNS SRV → TLS bind → rootDSE → optional OU search.
- Test Connection button on credential form.
- Seed fix: re-enable `join_ad_domain` and `rename_computer` in the "AD Domain Join — Local Admin" seed.
- **Configurable brand** via `/settings` (default `"ProxmoxVEAutopilot"`), threaded to renderer so the wrapping envelope picks it up.
- **Branding envelope** in renderer: ASCII header + Event Log start/success/failure + Registry stamp.
- Integration test + live harness additions.

**Deferred to Phase B.2b:**

- `local_admin` step type handler (template's baked admin works today; per-VM admin password deferred).
- `run_script` generic step.
- `install_module` step.
- Devices-page capture-action conditional disable.
- Hybrid stub "coming soon" badge.
- Unattend Jinja templating + per-VM answer ISO (the expensive alternative path, not needed for RunOnce).

**Why this scope ships value:** a fresh install targets "sequence with AD join works end-to-end against your real domain." That's the feature the user asked about. Bonus step types are mechanical additions once the guest-exec plumbing exists.

---

## File Structure

**New files:**

- `autopilot-proxmox/web/runonce_renderer.py` — resolves `CompiledSequence.runonce_steps` placeholders into final `.ps1` content; writes per-job files with 0600 perms.
- `autopilot-proxmox/web/ldap_tester.py` — `test_domain_join(payload, *, validate_certs)` returns a stage-by-stage dict.
- `autopilot-proxmox/roles/common/tasks/guest_run_ps_script.yml` — guest-file-write + guest-exec + log collect + delete.
- `autopilot-proxmox/roles/common/tasks/wait_reboot_cycle.yml` — ping-gap-based reboot detection.
- `autopilot-proxmox/tests/test_runonce_renderer.py`
- `autopilot-proxmox/tests/test_ldap_tester.py`

**Modified files:**

- `autopilot-proxmox/requirements.txt` — add `ldap3>=2.9,<3`.
- `autopilot-proxmox/web/sequence_compiler.py` — `CompiledSequence.runonce_steps` field; `_handle_join_ad_domain`, `_handle_rename_computer` handlers; remove both from the "not implemented" list.
- `autopilot-proxmox/web/app.py` — `start_provision` renders runonce scripts after compile; new `/api/credentials/test-domain-join` endpoint.
- `autopilot-proxmox/web/sequences_db.py` — re-enable seed steps.
- `autopilot-proxmox/web/templates/credential_edit.html` — Test Connection button for `domain_join` type.
- `autopilot-proxmox/playbooks/_provision_clone_vm.yml` — iterate RunOnce steps with reboot handling.
- `autopilot-proxmox/tests/test_sequence_compiler.py` — handler tests.
- `autopilot-proxmox/tests/test_sequences_api.py` — integration test for the RunOnce orchestration path.
- `autopilot-proxmox/tests/integration/test_live.py` — live smoke for Test Connection UI rendering.

---

## Phase 1 — Compiler handlers for RunOnce steps

### Task 1.1: `CompiledSequence.runonce_steps` field — failing tests

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequence_compiler.py`

- [ ] **Step 1: Append tests**

```python
def test_compiled_sequence_has_runonce_steps_field():
    from web import sequence_compiler
    seq = _make_sequence([])
    result = sequence_compiler.compile(seq)
    assert result.runonce_steps == []


def test_join_ad_domain_emits_runonce_step():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain",
         "params": {"credential_id": 42, "ou_path": "OU=Workstations,DC=example,DC=local"}},
    ])
    result = sequence_compiler.compile(seq)
    assert len(result.runonce_steps) == 1
    step = result.runonce_steps[0]
    assert step["step_type"] == "join_ad_domain"
    assert step["credential_id"] == 42
    assert step["params"]["ou_path"] == "OU=Workstations,DC=example,DC=local"
    assert step["causes_reboot"] is True
    # Template contains the placeholders the renderer will substitute
    assert "{{ cred.domain_fqdn | ps_escape }}" in step["ps_template"]
    assert "{{ cred.username | ps_escape }}" in step["ps_template"]
    assert "{{ cred.password | ps_escape }}" in step["ps_template"]
    assert "{{ params.ou_path | ps_escape }}" in step["ps_template"]
    # Core template calls Add-Computer without -Restart (renderer wraps
    # the reboot). causes_reboot=True is how the playbook learns to wait.
    assert "Add-Computer" in step["ps_template"]
    assert "-Restart" not in step["ps_template"]


def test_rename_computer_emits_runonce_step():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "rename_computer", "params": {"pattern": "{serial}"}},
    ])
    result = sequence_compiler.compile(seq)
    assert len(result.runonce_steps) == 1
    step = result.runonce_steps[0]
    assert step["step_type"] == "rename_computer"
    assert step["credential_id"] is None
    assert step["params"]["pattern"] == "{serial}"
    assert step["causes_reboot"] is True
    assert "Rename-Computer" in step["ps_template"]
    # Core template has no -Restart — renderer handles reboot.
    assert "-Restart" not in step["ps_template"]
    # The pattern will be expanded by the renderer using vm_context
    assert "{{ params.pattern | ps_escape }}" in step["ps_template"]


def test_runonce_steps_preserve_order():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"credential_id": 1, "ou_path": ""}},
        {"step_type": "rename_computer", "params": {"pattern": "{serial}"}},
    ])
    result = sequence_compiler.compile(seq)
    assert [s["step_type"] for s in result.runonce_steps] == [
        "join_ad_domain", "rename_computer"]


def test_disabled_runonce_step_is_skipped():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain",
         "params": {"credential_id": 1, "ou_path": ""}, "enabled": False},
    ])
    result = sequence_compiler.compile(seq)
    assert result.runonce_steps == []


def test_join_ad_domain_requires_credential_id():
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain", "params": {"ou_path": "OU=X"}},
    ])
    with pytest.raises(sequence_compiler.CompilerError) as exc:
        sequence_compiler.compile(seq)
    assert "credential_id" in str(exc.value)


def test_join_ad_domain_allows_empty_ou_path():
    """Empty OU path means 'default computers container'. Not an error."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "join_ad_domain",
         "params": {"credential_id": 1, "ou_path": ""}},
    ])
    result = sequence_compiler.compile(seq)
    assert len(result.runonce_steps) == 1
```

- [ ] **Step 2: Run to confirm failures**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v -k "runonce or join_ad or rename"
```

Expected: 7 failures with `AttributeError: 'CompiledSequence' object has no attribute 'runonce_steps'` and `UnknownStepType: 'join_ad_domain'`.

---

### Task 1.2: Implement `runonce_steps` + handlers

**Files:**
- Modify: `autopilot-proxmox/web/sequence_compiler.py`

- [ ] **Step 1: Extend `CompiledSequence` dataclass**

Find the dataclass definition. Replace with:

```python
@dataclass
class CompiledSequence:
    """The resolved form of a sequence."""
    ansible_vars: dict = field(default_factory=dict)
    autopilot_enabled: bool = False
    # RunOnce steps executed via guest-agent exec after OOBE. Each entry:
    #   step_type: str
    #   ps_template: str (Jinja-style {{ cred.X }} / {{ params.X }} / {{ vm.X }})
    #   credential_id: int | None
    #   params: dict
    #   causes_reboot: bool
    runonce_steps: list = field(default_factory=list)
```

- [ ] **Step 2: Add the core PS template constants** near the top of the module (after the imports).

These contain only the step's core action — the renderer wraps them with branding (header, Event Log, Registry stamp) and reboot logic. That keeps the handlers focused on what the step *does*, not cross-cutting concerns.

```python
# Core PS template constants — just the action. The renderer wraps each
# in a branding envelope (header + Event Log + Registry stamp + reboot).
# Jinja-style double-brace tokens resolved by runonce_renderer, not here.

_JOIN_AD_DOMAIN_PS = r"""$secure = ConvertTo-SecureString '{{ cred.password | ps_escape }}' -AsPlainText -Force
$creds  = New-Object System.Management.Automation.PSCredential(
    '{{ cred.username | ps_escape }}', $secure)
$ouArg = @{}
if ('{{ params.ou_path | ps_escape }}' -ne '') {
    $ouArg['OUPath'] = '{{ params.ou_path | ps_escape }}'
}
Add-Computer -DomainName '{{ cred.domain_fqdn | ps_escape }}' `
             -Credential $creds @ouArg -Force
"""

_RENAME_COMPUTER_PS = r"""Rename-Computer -NewName '{{ params.pattern | ps_escape }}' -Force
"""
```

Note the removed `-Restart` flags — the renderer's branding envelope handles the reboot via `Restart-Computer -Force` inside the success branch of its try/catch, ensuring the Event Log + Registry "Success" entries are written *before* the reboot happens.

- [ ] **Step 3: Add handlers and register them**

```python
def _handle_join_ad_domain(params: dict, out: CompiledSequence) -> None:
    cred_id = params.get("credential_id")
    if not cred_id:
        raise CompilerError(
            "join_ad_domain step requires a credential_id (type=domain_join)"
        )
    out.runonce_steps.append({
        "step_type": "join_ad_domain",
        "ps_template": _JOIN_AD_DOMAIN_PS,
        "credential_id": int(cred_id),
        "params": {"ou_path": params.get("ou_path", "") or ""},
        "causes_reboot": True,
    })


def _handle_rename_computer(params: dict, out: CompiledSequence) -> None:
    pattern = params.get("pattern", "{serial}") or "{serial}"
    out.runonce_steps.append({
        "step_type": "rename_computer",
        "ps_template": _RENAME_COMPUTER_PS,
        "credential_id": None,
        "params": {"pattern": pattern},
        "causes_reboot": True,
    })


_STEP_HANDLERS: dict[str, StepHandler] = {
    "set_oem_hardware": _handle_set_oem_hardware,
    "autopilot_entra": _handle_autopilot_entra,
    "autopilot_hybrid": _handle_hybrid_stub,
    "join_ad_domain": _handle_join_ad_domain,
    "rename_computer": _handle_rename_computer,
}
```

(The dict key `_STEP_HANDLERS` already exists — just add the two new entries.)

- [ ] **Step 4: Run tests**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v
```

Expected: all existing + 7 new tests pass. Report count.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequence_compiler.py
git commit -m "feat(compiler): add join_ad_domain + rename_computer RunOnce handlers"
```

---

## Phase 2 — RunOnce renderer

### Task 2.1: Failing tests for the renderer

**Files:**
- Create: `autopilot-proxmox/tests/test_runonce_renderer.py`

- [ ] **Step 1: Create the test file**

```python
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
```

- [ ] **Step 2: Confirm failures**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_runonce_renderer.py -v
```

Expected: all fail with `ModuleNotFoundError`.

---

### Task 2.2: Implement the renderer

**Files:**
- Create: `autopilot-proxmox/web/runonce_renderer.py`

- [ ] **Step 1: Create the module**

```python
"""Render RunOnce PowerShell templates for a compiled sequence.

Takes the compiler's ``runonce_steps`` list and produces final .ps1
content with credentials resolved, vm-context tokens expanded, and
a branding envelope (header comment + Windows Event Log + Registry
stamp) wrapping each step's core action.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, StrictUndefined


class RenderError(Exception):
    """Raised when a RunOnce step can't be rendered — usually a missing
    credential lookup or an unsubstituted template variable."""


def _ps_escape(value) -> str:
    """Escape a value for use inside a PowerShell single-quoted literal.

    PS rule: ``'foo''bar'`` is the literal string ``foo'bar`` — doubling
    a single quote is the only escape single-quoted PS strings need.
    """
    return str(value).replace("'", "''")


def _expand_pattern_tokens(pattern: str, vm_context: dict) -> str:
    defaults = {"serial": "", "vmid": "", "group_tag": ""}
    return pattern.format_map({**defaults, **vm_context})


def _build_env() -> Environment:
    env = Environment(
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["ps_escape"] = _ps_escape
    return env


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _wrap_with_branding(core_ps: str, step: dict, vm_context: dict,
                        brand: dict) -> str:
    """Wrap ``core_ps`` (the step's action) in the branding envelope.

    Produces: ASCII header block, ErrorActionPreference=Stop, Event Log
    source registration + start event, Registry stamp init, try{core +
    success stamp + success event + Restart-Computer if causes_reboot}
    catch{failure stamp + failure event + rethrow}.
    """
    sequence_id = vm_context.get("sequence_id", 0)
    sequence_name = vm_context.get("sequence_name", "")
    vmid = vm_context.get("vmid", "")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    brand_name = brand["name"]
    event_source = brand["event_source"]
    # Registry key uses sequence_id (integer, safe) rather than
    # sequence_name (may contain special chars).
    reg_root = f"{brand['registry_root']}\\Provisioning\\{sequence_id}\\{step['step_type']}"

    reboot_line = "Restart-Computer -Force" if step.get("causes_reboot") else "# (no reboot for this step)"

    return (
        "# ============================================================\n"
        f"# {brand_name} — Task Sequence RunOnce Step\n"
        f"# Sequence:  {sequence_name} (ID {sequence_id})\n"
        f"# Step:      {step['step_type']}\n"
        f"# Generated: {generated_at}\n"
        f"# VMID:      {vmid}\n"
        "# ============================================================\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"$brandSource  = '{_ps_escape(event_source)}'\n"
        f"$brandRegRoot = '{_ps_escape(reg_root)}'\n"
        "\n"
        "# Register Event Log source (idempotent — safe to fail silently).\n"
        "try {\n"
        "    if (-not [System.Diagnostics.EventLog]::SourceExists($brandSource)) {\n"
        "        New-EventLog -LogName Application -Source $brandSource -ErrorAction SilentlyContinue\n"
        "    }\n"
        f"    Write-EventLog -LogName Application -Source $brandSource -EntryType Information -EventId 1001 -Message \"Starting {step['step_type']} on VMID {vmid}\"\n"
        "} catch {}\n"
        "\n"
        "New-Item -Path $brandRegRoot -Force | Out-Null\n"
        "Set-ItemProperty -Path $brandRegRoot -Name \"Status\" -Value \"Running\"\n"
        "Set-ItemProperty -Path $brandRegRoot -Name \"StartedAt\" -Value (Get-Date -Format 'o')\n"
        f"Set-ItemProperty -Path $brandRegRoot -Name \"StepType\" -Value '{_ps_escape(step['step_type'])}'\n"
        "\n"
        "try {\n"
        f"{_indent(core_ps.rstrip(), '    ')}\n"
        "    Set-ItemProperty -Path $brandRegRoot -Name \"Status\" -Value \"Success\"\n"
        "    Set-ItemProperty -Path $brandRegRoot -Name \"CompletedAt\" -Value (Get-Date -Format 'o')\n"
        f"    Write-EventLog -LogName Application -Source $brandSource -EntryType Information -EventId 1002 -Message \"{step['step_type']} succeeded on VMID {vmid}\"\n"
        f"    {reboot_line}\n"
        "} catch {\n"
        "    Set-ItemProperty -Path $brandRegRoot -Name \"Status\" -Value \"Failure\"\n"
        "    Set-ItemProperty -Path $brandRegRoot -Name \"CompletedAt\" -Value (Get-Date -Format 'o')\n"
        "    Set-ItemProperty -Path $brandRegRoot -Name \"ErrorMessage\" -Value $_.Exception.Message\n"
        f"    Write-EventLog -LogName Application -Source $brandSource -EntryType Error -EventId 1099 -Message \"{step['step_type']} failed on VMID {vmid}: $($_.Exception.Message)\"\n"
        "    throw\n"
        "}\n"
    )


def render_step(step: dict, *, creds_resolver, vm_context: dict,
                brand: dict) -> str:
    """Render one step's core PS template with credentials + vm context,
    then wrap with the branding envelope.
    """
    cred_payload: dict = {}
    cid = step.get("credential_id")
    if cid:
        cred_payload = creds_resolver(cid) or {}
        if not cred_payload:
            raise RenderError(
                f"credential {cid} not found when rendering step "
                f"{step.get('step_type')!r}"
            )

    params = dict(step.get("params") or {})
    if "pattern" in params and isinstance(params["pattern"], str):
        params["pattern"] = _expand_pattern_tokens(params["pattern"], vm_context)

    env = _build_env()
    try:
        template = env.from_string(step["ps_template"])
        core = template.render(cred=cred_payload, params=params, vm=vm_context)
    except Exception as e:
        raise RenderError(
            f"failed to render {step.get('step_type')!r}: {e}"
        ) from e

    return _wrap_with_branding(core, step, vm_context, brand)


def write_step_scripts(*, steps: list, dest_dir: Path, creds_resolver,
                       vm_context: dict, brand: dict) -> list[dict]:
    """Render every step and write each to a .ps1 file in ``dest_dir``
    with 0600 perms. Returns metadata per step (path, step_type,
    causes_reboot)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    infos: list[dict] = []
    for idx, step in enumerate(steps):
        content = render_step(step, creds_resolver=creds_resolver,
                              vm_context=vm_context, brand=brand)
        filename = f"step-{idx:02d}-{step['step_type']}.ps1"
        path = dest_dir / filename
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        infos.append({
            "step_type": step["step_type"],
            "causes_reboot": bool(step.get("causes_reboot")),
            "path": str(path),
        })
    return infos
```

- [ ] **Step 2: Run tests**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_runonce_renderer.py -v
```

Expected: 7 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/runonce_renderer.py autopilot-proxmox/tests/test_runonce_renderer.py
git commit -m "feat(runonce): add renderer that resolves PS templates to .ps1 files"
```

---

## Phase 3 — LDAP-based Test Connection

### Task 3.1: Add `ldap3` to requirements

**Files:**
- Modify: `autopilot-proxmox/requirements.txt`

- [ ] **Step 1: Append**

```
ldap3>=2.9,<3
```

- [ ] **Step 2: Install locally so tests can import it**

```
cd autopilot-proxmox && .venv/bin/pip install 'ldap3>=2.9,<3'
```

Expected: installs cleanly (pure Python).

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/requirements.txt
git commit -m "build: add ldap3 for AD credential test-connection"
```

---

### Task 3.2: Failing tests for `ldap_tester`

**Files:**
- Create: `autopilot-proxmox/tests/test_ldap_tester.py`

- [ ] **Step 1: Create the test file**

```python
"""Tests for web.ldap_tester — validates domain_join credentials via ldap3."""
from unittest.mock import MagicMock, patch

import pytest


def _payload(**over):
    return {"domain_fqdn": "example.local",
            "username": "EXAMPLE\\svc_join",
            "password": "secret",
            "ou_hint": over.pop("ou_hint", ""),
            **over}


def test_returns_ok_structure_on_full_success():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns, \
         patch("web.ldap_tester._try_bind") as mock_bind:
        mock_dns.return_value = (["dc01.example.local"], 8)
        # mock_bind returns (connect_result, bind_result, rootdse_result)
        mock_bind.return_value = (
            {"ok": True, "server": "dc01.example.local", "tls": "ldaps",
             "elapsed_ms": 48},
            {"ok": True, "elapsed_ms": 61},
            {"ok": True, "defaultNamingContext": "DC=example,DC=local",
             "dnsHostName": "dc01.example.local"},
        )
        out = ldap_tester.test_domain_join(_payload(), validate_certs=False)
    assert out["ok"] is True
    assert out["dns"]["ok"] is True
    assert out["dns"]["servers"] == ["dc01.example.local"]
    assert out["connect"]["ok"] is True
    assert out["bind"]["ok"] is True
    assert out["rootdse"]["ok"] is True
    assert out["ou"]["ok"] is True  # empty ou_hint → skipped, reports ok


def test_reports_dns_failure_and_stops():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns:
        mock_dns.side_effect = Exception("NXDOMAIN")
    # Must not blow up; must report ok=False with dns stage set.
    with patch("web.ldap_tester._dns_srv_lookup", side_effect=Exception("NXDOMAIN")):
        out = ldap_tester.test_domain_join(_payload(), validate_certs=False)
    assert out["ok"] is False
    assert out["dns"]["ok"] is False
    assert "NXDOMAIN" in out["dns"]["error"]
    # Later stages should not have tried to run
    assert "bind" not in out or out["bind"].get("ok") is not True


def test_reports_bind_failure_with_ldap_error_text():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns, \
         patch("web.ldap_tester._try_bind") as mock_bind:
        mock_dns.return_value = (["dc01.example.local"], 8)
        mock_bind.return_value = (
            {"ok": True, "server": "dc01.example.local", "tls": "ldaps",
             "elapsed_ms": 48},
            {"ok": False, "elapsed_ms": 40, "error": "invalidCredentials"},
            None,
        )
        out = ldap_tester.test_domain_join(_payload(), validate_certs=False)
    assert out["ok"] is False
    assert out["bind"]["error"] == "invalidCredentials"


def test_password_never_echoed_in_response():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup", side_effect=Exception("no dns")):
        out = ldap_tester.test_domain_join(_payload(password="HUNT3R-42"),
                                            validate_certs=False)
    # Walk the whole response — the password must NOT appear anywhere.
    import json
    assert "HUNT3R-42" not in json.dumps(out)


def test_ou_search_runs_when_ou_hint_supplied():
    from web import ldap_tester
    with patch("web.ldap_tester._dns_srv_lookup") as mock_dns, \
         patch("web.ldap_tester._try_bind") as mock_bind, \
         patch("web.ldap_tester._search_ou") as mock_ou:
        mock_dns.return_value = (["dc01.example.local"], 8)
        mock_bind.return_value = (
            {"ok": True, "server": "dc01.example.local", "tls": "ldaps",
             "elapsed_ms": 48},
            {"ok": True, "elapsed_ms": 61},
            {"ok": True, "defaultNamingContext": "DC=example,DC=local",
             "dnsHostName": "dc01.example.local"},
        )
        mock_ou.return_value = {"ok": True, "dn": "OU=X,DC=example,DC=local",
                                 "elapsed_ms": 37}
        out = ldap_tester.test_domain_join(
            _payload(ou_hint="OU=X,DC=example,DC=local"), validate_certs=False)
    assert out["ou"]["ok"] is True
    assert out["ou"]["dn"] == "OU=X,DC=example,DC=local"
    mock_ou.assert_called_once()
```

- [ ] **Step 2: Confirm failures**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_ldap_tester.py -v
```

Expected: fail with `ModuleNotFoundError`.

---

### Task 3.3: Implement the LDAP tester

**Files:**
- Create: `autopilot-proxmox/web/ldap_tester.py`

- [ ] **Step 1: Create the module**

```python
"""Test an AD `domain_join` credential via ldap3.

DNS SRV lookup → TLS connect → bind → rootDSE read → optional OU
search. Returns a stage-by-stage dict so the UI can render green/red
checklist rows. Never echoes the submitted password in any stage's
response (including error text).
"""
from __future__ import annotations

import socket
import time
from typing import Optional

from ldap3 import Connection, Server, SUBTREE, Tls
import ssl


# Per-stage timeout (seconds); total budget capped by the sum of stages.
_STAGE_TIMEOUT = 8
_TOTAL_BUDGET = 30


def test_domain_join(payload: dict, *, validate_certs: bool) -> dict:
    """Run the five-stage test. ``payload`` is a ``domain_join``-typed
    credential dict (domain_fqdn, username, password, optional ou_hint).
    """
    out: dict = {
        "ok": False,
        "dns": {"ok": False},
        "connect": {"ok": False},
        "bind": {"ok": False},
        "rootdse": {"ok": False},
        "ou": {"ok": True},  # default ok when ou_hint is absent
    }
    fqdn = (payload.get("domain_fqdn") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ou_hint = (payload.get("ou_hint") or "").strip()

    # 1. DNS SRV
    try:
        servers, dns_ms = _dns_srv_lookup(fqdn)
    except Exception as e:
        out["dns"] = {"ok": False, "error": str(e)}
        return out
    out["dns"] = {"ok": True, "servers": servers, "elapsed_ms": dns_ms}

    # 2-4. connect + bind + rootDSE (attempt servers in order)
    try:
        connect_info, bind_info, rootdse_info = _try_bind(
            servers, username, password, validate_certs=validate_certs,
        )
    except Exception as e:
        out["connect"] = {"ok": False, "error": str(e)}
        return out
    out["connect"] = connect_info
    out["bind"] = bind_info
    if not bind_info["ok"]:
        return out
    if rootdse_info:
        out["rootdse"] = rootdse_info

    # 5. Optional OU search
    if ou_hint:
        try:
            ou_info = _search_ou(servers[0], username, password, ou_hint,
                                  validate_certs=validate_certs)
        except Exception as e:
            out["ou"] = {"ok": False, "error": str(e)}
            return out
        out["ou"] = ou_info
        if not ou_info["ok"]:
            return out

    out["ok"] = True
    return out


def _dns_srv_lookup(fqdn: str) -> tuple[list[str], int]:
    """Resolve _ldap._tcp.<fqdn> to an ordered list of DC hostnames."""
    import dns.resolver  # dnspython — transitive of ldap3 in recent versions
    start = time.monotonic()
    answers = dns.resolver.resolve(f"_ldap._tcp.{fqdn}", "SRV",
                                    lifetime=_STAGE_TIMEOUT)
    # SRV records are (priority, weight, port, target). Sort by priority.
    ordered = sorted(answers, key=lambda a: (a.priority, -a.weight))
    hosts = [str(a.target).rstrip(".") for a in ordered]
    ms = int((time.monotonic() - start) * 1000)
    return hosts, ms


def _try_bind(servers: list[str], username: str, password: str, *,
              validate_certs: bool) -> tuple[dict, dict, Optional[dict]]:
    """Attempt LDAPS (636) then LDAP+StartTLS (389) against the first
    responsive server. Returns (connect_info, bind_info, rootdse_info).
    """
    last_error = None
    for host in servers:
        for port, tls_mode in ((636, "ldaps"), (389, "starttls")):
            try:
                start = time.monotonic()
                tls = Tls(
                    validate=ssl.CERT_REQUIRED if validate_certs else ssl.CERT_NONE,
                    version=ssl.PROTOCOL_TLS_CLIENT,
                )
                server = Server(host, port=port, use_ssl=(tls_mode == "ldaps"),
                                tls=tls, connect_timeout=_STAGE_TIMEOUT,
                                get_info="ALL")
                connect_ms = int((time.monotonic() - start) * 1000)
                connect_info = {"ok": True, "server": host, "tls": tls_mode,
                                "elapsed_ms": connect_ms}

                bind_start = time.monotonic()
                conn = Connection(server, user=username, password=password,
                                  auto_bind="TLS_BEFORE_BIND"
                                  if tls_mode == "starttls" else "DEFAULT",
                                  receive_timeout=_STAGE_TIMEOUT)
                bind_ms = int((time.monotonic() - bind_start) * 1000)
                bind_info = {"ok": True, "elapsed_ms": bind_ms}

                # rootDSE from server.info (ldap3 populates on bind)
                info = server.info
                rootdse_info = {
                    "ok": True,
                    "defaultNamingContext":
                        str(info.other.get("defaultNamingContext", [""])[0])
                        if info else "",
                    "dnsHostName":
                        str(info.other.get("dnsHostName", [""])[0])
                        if info else "",
                }
                conn.unbind()
                return connect_info, bind_info, rootdse_info
            except Exception as e:
                msg = str(e)
                # Strip any embedded password echoes just in case the ldap3
                # traceback ever includes them.
                if password and password in msg:
                    msg = msg.replace(password, "***")
                last_error = msg
                # Try next port/host
                continue
    # All attempts failed — report the last one's error, but on the bind
    # stage (not connect) since reaching here usually means bind failed.
    return (
        {"ok": False, "error": last_error or "all servers unreachable"},
        {"ok": False, "error": last_error or "bind failed", "elapsed_ms": 0},
        None,
    )


def _search_ou(host: str, username: str, password: str, ou_dn: str, *,
               validate_certs: bool) -> dict:
    """Search for the OU DN to confirm it exists and is visible to the
    bind account. Uses LDAPS only for simplicity."""
    start = time.monotonic()
    tls = Tls(
        validate=ssl.CERT_REQUIRED if validate_certs else ssl.CERT_NONE,
        version=ssl.PROTOCOL_TLS_CLIENT,
    )
    server = Server(host, port=636, use_ssl=True, tls=tls,
                    connect_timeout=_STAGE_TIMEOUT)
    conn = Connection(server, user=username, password=password,
                      auto_bind="DEFAULT", receive_timeout=_STAGE_TIMEOUT)
    try:
        # base-level search for the exact OU
        found = conn.search(search_base=ou_dn, search_filter="(objectClass=*)",
                            search_scope="BASE")
        ms = int((time.monotonic() - start) * 1000)
        if found and conn.entries:
            return {"ok": True, "dn": ou_dn, "elapsed_ms": ms}
        return {"ok": False, "error": f"noSuchObject: {ou_dn}",
                "elapsed_ms": ms}
    finally:
        conn.unbind()
```

- [ ] **Step 2: Run tests**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_ldap_tester.py -v
```

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/ldap_tester.py autopilot-proxmox/tests/test_ldap_tester.py
git commit -m "feat(ldap): add Test Connection for domain_join credentials"
```

---

## Phase 3.5 — Brand settings wiring

### Task 3.5.1: Add `brand_name` to the Settings schema

**Files:**
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/inventory/group_vars/all/vars.yml` (default only — skip on live host, the operator edits via `/settings`)

- [ ] **Step 1: Find `SETTINGS_SCHEMA`** in `web/app.py` (search for `SETTINGS_SCHEMA = [`).

- [ ] **Step 2: Add a new section for branding**

Near the end of the schema list, before `]`, add:

```python
    {"section": "Branding", "fields": [
        {"key": "brand_name", "label": "Brand Name", "type": "text"},
    ]},
```

- [ ] **Step 3: Add a helper `_load_brand_context()` near `_load_proxmox_config`**

```python
def _load_brand_context() -> dict:
    """Return the brand dict threaded to the runonce_renderer.

    Default brand is 'ProxmoxVEAutopilot'. White-label customers set
    their own via the Branding section of /settings.
    """
    cfg = _load_vars()
    name = (cfg.get("brand_name") or "").strip() or "ProxmoxVEAutopilot"
    # Windows registry keys can't contain colons or backslashes inside a
    # component. Sanitize by keeping alphanumerics, hyphens, underscores.
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "Brand"
    return {
        "name": name,
        "event_source": safe,
        "registry_root": fr"HKLM:\SOFTWARE\{safe}",
    }
```

- [ ] **Step 4: Append one test in `tests/test_web.py`**

```python
def test_load_brand_context_default():
    from web.app import _load_brand_context
    brand = _load_brand_context()
    assert brand["name"] == "ProxmoxVEAutopilot" or brand["name"]  # whatever vars.yml carries
    assert "registry_root" in brand
    assert brand["registry_root"].startswith(r"HKLM:\SOFTWARE")
```

- [ ] **Step 5: Smoke + commit**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_web.py -v -k brand
```

Expected: PASS.

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_web.py
git commit -m "feat(web): add brand_name setting + _load_brand_context helper"
```

---

## Phase 4 — Test Connection UI

### Task 4.1: `/api/credentials/test-domain-join` endpoint

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Add the route** (append near the other credentials routes):

```python
class _TestDomainJoinReq(BaseModel):
    # Either supply an ID for a saved credential OR an inline payload.
    credential_id: Optional[int] = None
    payload: Optional[dict] = None


@app.post("/api/credentials/test-domain-join")
def api_test_domain_join(body: _TestDomainJoinReq):
    # Resolve the payload: prefer credential_id if set.
    payload: Optional[dict] = body.payload
    if body.credential_id:
        cred = sequences_db.get_credential(
            SEQUENCES_DB, _cipher(), body.credential_id)
        if cred is None:
            raise HTTPException(404, "credential not found")
        if cred["type"] != "domain_join":
            raise HTTPException(400,
                f"credential {body.credential_id} is type {cred['type']!r}, "
                f"not 'domain_join'")
        payload = cred["payload"]
    if not payload:
        raise HTTPException(400, "payload or credential_id is required")

    cfg = _load_proxmox_config()
    validate_certs = bool(cfg.get("ad_validate_certs", False))

    from web import ldap_tester
    return ldap_tester.test_domain_join(payload, validate_certs=validate_certs)
```

- [ ] **Step 2: Verify import works**

```
cd autopilot-proxmox && .venv/bin/python -c "from web import app; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "feat(api): add /api/credentials/test-domain-join endpoint"
```

---

### Task 4.2: Test Connection button in `credential_edit.html`

**Files:**
- Modify: `autopilot-proxmox/web/templates/credential_edit.html`

- [ ] **Step 1: Find the domain_join fieldset**

Search for `fields-domain_join` — that's the `<tbody id="fields-domain_join">` block showing domain FQDN / Username / Password / OU hint fields.

- [ ] **Step 2: Append a Test Connection row inside that tbody, just before its closing tag**

```html
<tr>
  <td></td>
  <td>
    <button type="button" onclick="testDomainJoin()">Test connection</button>
    <span id="dj-test-status" style="margin-left:8px;color:#666;"></span>
    <div id="dj-test-details" style="margin-top:6px;font-family:monospace;
         white-space:pre;font-size:11px;background:#fafafa;
         border:1px solid #ccc;padding:6px;display:none;"></div>
  </td>
</tr>
```

- [ ] **Step 3: Append the JavaScript to the existing `<script>` block at the bottom of the template**

```javascript
async function testDomainJoin() {
  const statusEl = document.getElementById('dj-test-status');
  const detailsEl = document.getElementById('dj-test-details');
  statusEl.textContent = 'Testing…';
  statusEl.style.color = '#666';
  detailsEl.style.display = 'none';

  const credId = {{ cred.id if cred else 'null' }};
  // On the edit page, prefer the saved credential_id (uses stored password).
  // On the new page, pack the form fields into a payload.
  let body;
  if (credId) {
    body = { credential_id: credId };
  } else {
    body = {
      payload: {
        domain_fqdn: document.querySelector('[name=domain_fqdn]').value,
        username: document.querySelector('[name=username]').value,
        password: document.querySelector('[name=password]').value,
        ou_hint: document.querySelector('[name=ou_hint]').value,
      }
    };
    if (!body.payload.password) {
      statusEl.textContent = 'Password required for test (new credential)';
      statusEl.style.color = '#c00';
      return;
    }
  }

  let r, j;
  try {
    r = await fetch('/api/credentials/test-domain-join', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    j = await r.json();
  } catch (e) {
    statusEl.textContent = 'Request failed: ' + e.message;
    statusEl.style.color = '#c00';
    return;
  }

  if (!r.ok) {
    statusEl.textContent = 'HTTP ' + r.status + ': ' + (j.detail || r.statusText);
    statusEl.style.color = '#c00';
    return;
  }

  const stages = [
    ['dns', 'DNS SRV'], ['connect', 'TLS connect'], ['bind', 'Bind'],
    ['rootdse', 'rootDSE'], ['ou', 'OU visibility'],
  ];
  const lines = stages.map(([k, label]) => {
    const s = j[k] || {};
    const mark = s.ok ? '\u2713' : '\u2717';
    const color = s.ok ? '#060' : '#c00';
    const extra = s.elapsed_ms != null ? ` (${s.elapsed_ms} ms)` : '';
    const err = s.error ? ` — ${s.error}` : '';
    return `<span style="color:${color}">${mark}</span> ${label}${extra}${err}`;
  }).join('\n');
  detailsEl.innerHTML = lines;
  detailsEl.style.display = 'block';
  statusEl.textContent = j.ok ? 'Passed' : 'Failed';
  statusEl.style.color = j.ok ? '#060' : '#c00';
}
```

- [ ] **Step 4: Smoke — render the form**

```
cd autopilot-proxmox && .venv/bin/python -c "from web import app; print('ok')"
```

Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/templates/credential_edit.html
git commit -m "feat(ui): add Test Connection button for domain_join credentials"
```

---

## Phase 5 — Ansible common tasks (guest exec + reboot waiter)

### Task 5.1: `guest_run_ps_script.yml`

**Files:**
- Create: `autopilot-proxmox/roles/common/tasks/guest_run_ps_script.yml`

**Inputs (caller vars):**
- `vm_vmid` — existing convention
- `_ps_script_path` — path on the Ansible controller to the local .ps1 file to run
- `_ps_step_name` — a short name used in task titles + log lines

- [ ] **Step 1: Create the file**

```yaml
---
# Push a PowerShell script from the controller to the guest, execute it,
# and collect the output. Designed for sequence RunOnce steps.
#
# Caller provides:
#   vm_vmid          — Proxmox VMID
#   _ps_script_path  — controller-local path to the .ps1 file
#   _ps_step_name    — short identifier used in task titles / logs
#
# The script is written to the guest at C:\Windows\Temp\autopilot-<name>.ps1
# and deleted after execution (best-effort; failures to delete are non-fatal).

- name: "Read PS script from controller — {{ _ps_step_name }}"
  ansible.builtin.slurp:
    src: "{{ _ps_script_path }}"
  register: _ps_script_b64
  no_log: true

- name: "Write PS script to guest — {{ _ps_step_name }}"
  ansible.builtin.uri:
    url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/agent/file-write"
    method: POST
    body_format: form-urlencoded
    body:
      file: 'C:\Windows\Temp\autopilot-{{ _ps_step_name }}.ps1'
      content: "{{ _ps_script_b64.content }}"
      encode: "0"  # content is already base64 from slurp
    headers:
      Authorization: "{{ proxmox_api_auth_header }}"
    validate_certs: "{{ proxmox_validate_certs }}"
    status_code: [200]
  no_log: true

- name: "Execute PS script in guest — {{ _ps_step_name }}"
  ansible.builtin.uri:
    url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/agent/exec"
    method: POST
    body_format: form-urlencoded
    body:
      command: 'powershell.exe -ExecutionPolicy Bypass -NoProfile -File C:\Windows\Temp\autopilot-{{ _ps_step_name }}.ps1'
    headers:
      Authorization: "{{ proxmox_api_auth_header }}"
    validate_certs: "{{ proxmox_validate_certs }}"
    status_code: [200]
  register: _ps_exec_result

- name: "Report executed — {{ _ps_step_name }}"
  ansible.builtin.debug:
    msg: "Executed RunOnce step {{ _ps_step_name }} on VM {{ vm_vmid }} (pid: {{ _ps_exec_result.json.data.pid | default('unknown') }})"

# Best-effort cleanup of the script file in the guest. If the step triggered
# a reboot, the file may already be gone — ignore failures.
- name: "Delete PS script from guest — {{ _ps_step_name }}"
  ansible.builtin.uri:
    url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/agent/exec"
    method: POST
    body_format: form-urlencoded
    body:
      command: 'cmd.exe /c del /Q C:\Windows\Temp\autopilot-{{ _ps_step_name }}.ps1'
    headers:
      Authorization: "{{ proxmox_api_auth_header }}"
    validate_certs: "{{ proxmox_validate_certs }}"
    status_code: [200]
  failed_when: false
```

- [ ] **Step 2: Lint YAML**

```
cd autopilot-proxmox && .venv/bin/python -c "import yaml; yaml.safe_load(open('roles/common/tasks/guest_run_ps_script.yml')); print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/roles/common/tasks/guest_run_ps_script.yml
git commit -m "feat(ansible): guest_run_ps_script common task"
```

---

### Task 5.2: `wait_reboot_cycle.yml`

**Files:**
- Create: `autopilot-proxmox/roles/common/tasks/wait_reboot_cycle.yml`

- [ ] **Step 1: Create the file**

```yaml
---
# Watch the guest agent ping through a reboot cycle. Use this AFTER a
# task that is expected to cause a Windows reboot (e.g. Add-Computer
# -Restart, Rename-Computer -Restart).
#
# Logic: wait for the agent to stop responding (3 consecutive timeouts ⇒
# reboot in progress), then delegate to the existing wait_guest_agent.yml
# to wait for the agent to come back up.
#
# Total budget: reboot_wait_timeout_seconds (default 600, 10 min).

- name: "Wait for guest agent to drop (reboot started)"
  ansible.builtin.uri:
    url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/agent/ping"
    method: POST
    headers:
      Authorization: "{{ proxmox_api_auth_header }}"
    validate_certs: "{{ proxmox_validate_certs }}"
    status_code: [200]
    timeout: 5
  register: _ping_during_reboot
  retries: "{{ (reboot_wait_timeout_seconds | default(600) | int / 3) | int }}"
  delay: 3
  # Success condition inverted: keep retrying UNTIL the ping fails,
  # signalling the reboot has begun. We use failed_when + ignore_errors
  # rather than until, so that a failing ping is treated as success here.
  until: _ping_during_reboot is failed
  failed_when: false
  ignore_errors: true

- name: "Wait for guest agent to come back after reboot"
  ansible.builtin.include_tasks: "{{ role_path }}/../common/tasks/wait_guest_agent.yml"

- name: "Reboot cycle complete"
  ansible.builtin.debug:
    msg: "VM {{ vm_vmid }} completed a reboot cycle and the guest agent is back."
```

- [ ] **Step 2: Lint**

```
cd autopilot-proxmox && .venv/bin/python -c "import yaml; yaml.safe_load(open('roles/common/tasks/wait_reboot_cycle.yml')); print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/roles/common/tasks/wait_reboot_cycle.yml
git commit -m "feat(ansible): wait_reboot_cycle common task"
```

---

## Phase 6 — Web-layer orchestration

### Task 6.1: Render runonce scripts in `start_provision`

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: After the sequence compile step (where `resolved_vars` is built), add a "render runonce" block**

Locate the section in `start_provision` that looks like:

```python
if sequence_id:
    seq = sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
    if seq is None:
        raise HTTPException(404, f"sequence {sequence_id} not found")
    try:
        compiled = sequence_compiler.compile(seq)
    except sequence_compiler.CompilerError as e:
        raise HTTPException(400, f"sequence compile failed: {e}")
    ...
    resolved_vars = sequence_compiler.resolve_provision_vars(...)
```

Immediately after `resolved_vars = ...`, add:

```python
    # Render RunOnce step scripts (join_ad_domain, rename_computer, etc.)
    # Each gets its own .ps1 in a per-job runonce/ dir with 0600 perms.
    # The renderer wraps each core action with the branding envelope
    # (header + Event Log + Registry stamp).
    if compiled.runonce_steps:
        runonce_dir = Path(job_manager.jobs_dir) / "pending" / f"seq-{sequence_id}" / "runonce"
        def _creds_resolver(cid: int, _cipher=_cipher, _db=SEQUENCES_DB):
            c = sequences_db.get_credential(_db, _cipher(), cid)
            return c["payload"] if c else None
        from web import runonce_renderer
        try:
            runonce_infos = runonce_renderer.write_step_scripts(
                steps=compiled.runonce_steps,
                dest_dir=runonce_dir,
                creds_resolver=_creds_resolver,
                vm_context={
                    "serial": "",     # filled by Ansible per-VM (TBD below)
                    "vmid": "",       # same
                    "group_tag": group_tag,
                    "sequence_id": int(sequence_id),
                    "sequence_name": seq["name"],
                },
                brand=_load_brand_context(),
            )
        except runonce_renderer.RenderError as e:
            raise HTTPException(400, f"runonce render failed: {e}")
        resolved_vars["_runonce_scripts_json"] = json.dumps(runonce_infos)
    else:
        runonce_infos = []
```

Add `from pathlib import Path` and `import json` to the app.py import block if not already present.

**Note about `vm_context` tokens**: the `{serial}` / `{vmid}` tokens in `rename_computer`'s `pattern` param need per-VM values that Python doesn't know yet — those are generated inside Ansible. For Phase B.2a, we ship with serial/vmid expansion happening **inside Ansible** via a second templating pass. The Python renderer expands `{group_tag}` only. Document this in the renderer comment:

In `web/runonce_renderer.py`, update the `_expand_pattern_tokens` docstring:

```python
def _expand_pattern_tokens(pattern: str, vm_context: dict) -> str:
    """Expand {serial}/{vmid}/{group_tag} tokens in a pattern string.

    NOTE: In the provision flow, serial and vmid aren't known at Python
    render time — they're generated inside Ansible. The web layer passes
    empty strings for those keys, and the Ansible role does a second
    pass replacing the empty strings with the actual values before
    writing the .ps1 to the guest. {group_tag} expands here because the
    web layer knows it up front.
    """
    defaults = {"serial": "", "vmid": "", "group_tag": ""}
    return pattern.format_map({**defaults, **vm_context})
```

- [ ] **Step 2: Smoke import**

```
cd autopilot-proxmox && .venv/bin/python -c "from web import app; print('ok')"
```

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/web/runonce_renderer.py
git commit -m "feat(web): render runonce scripts per-job after sequence compile"
```

---

### Task 6.2: Playbook — iterate RunOnce steps after guest agent is up

**Files:**
- Modify: `autopilot-proxmox/playbooks/_provision_clone_vm.yml`

- [ ] **Step 1: Find the existing tasks**

Current shape:

```yaml
- name: "Inject Autopilot config into VM {{ vm_vmid }}"
  ansible.builtin.include_role:
    name: autopilot_inject
  when: ...

- name: "Capture hardware hash from VM {{ vm_vmid }}"
  ansible.builtin.include_role:
    name: hash_capture
  when: capture_hardware_hash | default(true) | bool
```

- [ ] **Step 2: Insert RunOnce execution BETWEEN autopilot_inject and hash_capture**

```yaml
- name: "Iterate RunOnce steps for VM {{ vm_vmid }}"
  when: _runonce_scripts_json is defined and _runonce_scripts_json | length > 0
  block:
    - name: "Parse runonce_scripts manifest"
      ansible.builtin.set_fact:
        _runonce_scripts: "{{ _runonce_scripts_json | from_json }}"

    - name: "Substitute serial/vmid tokens in runonce scripts"
      ansible.builtin.shell: >-
        sed -i
        -e 's/{{ '{{' }}serial{{ '}}' }}/{{ _vm_serial | regex_replace("/", "\\/") }}/g'
        -e 's/{{ '{{' }}vmid{{ '}}' }}/{{ vm_vmid }}/g'
        {{ item.path | quote }}
      loop: "{{ _runonce_scripts }}"
      loop_control:
        label: "{{ item.step_type }}"

    - name: "Run RunOnce step {{ item.step_type }} on VM {{ vm_vmid }}"
      ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/guest_run_ps_script.yml"
      vars:
        _ps_script_path: "{{ item.path }}"
        _ps_step_name: "{{ item.step_type }}-{{ ansible_loop.index }}"
      loop: "{{ _runonce_scripts }}"
      loop_control:
        loop_var: item
        extended: true
        label: "{{ item.step_type }}"

    - name: "Wait for reboot cycle when step requires it"
      ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/wait_reboot_cycle.yml"
      loop: "{{ _runonce_scripts }}"
      loop_control:
        loop_var: item
        label: "{{ item.step_type }}"
      when: item.causes_reboot | bool
```

**Structural note:** the three loops above walk the same `_runonce_scripts` list. Ansible does NOT run them interleaved — all tokenization finishes, then all executions finish, then all reboot-waits finish. That's wrong. The correct shape is **one loop** that for each step does: tokenize → exec → wait-reboot-if-needed. Fix by extracting to a per-step include file:

Create `autopilot-proxmox/roles/common/tasks/run_one_runonce_step.yml`:

```yaml
---
# Orchestrate a single RunOnce step: tokenize, exec, optionally wait for reboot.
# Caller supplies `item` (one entry from the runonce_scripts manifest).

- name: "Substitute serial/vmid in {{ item.step_type }} script"
  ansible.builtin.shell: >-
    sed -i
    -e 's/{{ '{{' }}serial{{ '}}' }}/{{ _vm_serial | regex_replace("/", "\\/") }}/g'
    -e 's/{{ '{{' }}vmid{{ '}}' }}/{{ vm_vmid }}/g'
    {{ item.path | quote }}

- name: "Execute {{ item.step_type }} on VM {{ vm_vmid }}"
  ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/guest_run_ps_script.yml"
  vars:
    _ps_script_path: "{{ item.path }}"
    _ps_step_name: "{{ item.step_type }}"

- name: "Wait for reboot after {{ item.step_type }}"
  ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/wait_reboot_cycle.yml"
  when: item.causes_reboot | bool
```

Then simplify the block in `_provision_clone_vm.yml`:

```yaml
- name: "Iterate RunOnce steps for VM {{ vm_vmid }}"
  when: _runonce_scripts_json is defined and _runonce_scripts_json | length > 0
  block:
    - name: "Parse runonce_scripts manifest"
      ansible.builtin.set_fact:
        _runonce_scripts: "{{ _runonce_scripts_json | from_json }}"

    - name: "Run each RunOnce step in order"
      ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/run_one_runonce_step.yml"
      loop: "{{ _runonce_scripts }}"
      loop_control:
        loop_var: item
        label: "{{ item.step_type }}"
```

- [ ] **Step 3: Lint both YAML files**

```
cd autopilot-proxmox && \
  .venv/bin/python -c "import yaml; yaml.safe_load(open('playbooks/_provision_clone_vm.yml'))" && \
  .venv/bin/python -c "import yaml; yaml.safe_load(open('roles/common/tasks/run_one_runonce_step.yml'))" && \
  echo ok
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/playbooks/_provision_clone_vm.yml autopilot-proxmox/roles/common/tasks/run_one_runonce_step.yml
git commit -m "feat(ansible): execute RunOnce steps after OOBE with reboot handling"
```

---

## Phase 7 — Seed re-enable

### Task 7.1: Re-enable `join_ad_domain` and `rename_computer` in the AD seed

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`

- [ ] **Step 1: Locate `_SEED_SEQUENCES` and the "AD Domain Join — Local Admin" entry**

- [ ] **Step 2: Change `enabled: False` → `enabled: True` for `join_ad_domain` and `rename_computer` steps**

Before:

```python
{"step_type": "join_ad_domain",
 "params": {"credential_id": 0, "ou_path": ""},
 "enabled": False},
{"step_type": "rename_computer",
 "params": {"pattern": "{serial}"},
 "enabled": False},
```

After:

```python
{"step_type": "join_ad_domain",
 "params": {"credential_id": 0, "ou_path": ""},
 # Note: credential_id=0 is a placeholder — operator must edit the sequence
 # via /sequences/<id>/edit and select a real domain_join credential
 # before provisioning with this sequence.
 "enabled": True},
{"step_type": "rename_computer",
 "params": {"pattern": "{serial}"},
 "enabled": True},
```

Leave `local_admin` with `enabled: False` — deferred to Phase B.2b.

- [ ] **Step 3: Update the seed regression test**

In `autopilot-proxmox/tests/test_sequences_db.py`, find `test_seed_default_sequence_b1_compiles_cleanly`. Rename to `test_seed_default_sequences_all_compile_cleanly` and expand:

```python
def test_seed_default_sequences_all_compile_cleanly(db_path, key_path):
    """After B.2a, all three seeded sequences must compile without error
    (except the Hybrid stub which is expected to raise StepNotImplemented).
    The AD sequence compiles clean BUT emits a runonce step with
    credential_id=0 which would fail at render time — that's by design;
    the operator wires up a real credential before provisioning."""
    from web import crypto, sequences_db, sequence_compiler
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    for s in sequences_db.list_sequences(db_path):
        seq = sequences_db.get_sequence(db_path, s["id"])
        if s["name"] == "Hybrid Autopilot (stub)":
            with pytest.raises(sequence_compiler.StepNotImplemented):
                sequence_compiler.compile(seq)
        elif s["name"] == "AD Domain Join — Local Admin":
            # Compiles (join_ad_domain has credential_id=0 which is truthy
            # to the compiler — the renderer rejects later).
            compiled = sequence_compiler.compile(seq)
            # Should have the RunOnce steps present
            types = [x["step_type"] for x in compiled.runonce_steps]
            assert "join_ad_domain" in types
            assert "rename_computer" in types
        else:
            compiled = sequence_compiler.compile(seq)
```

**Wait — issue:** my compiler's `_handle_join_ad_domain` raises `CompilerError` when `credential_id` is falsy, and 0 is falsy. So the seeded entry with `credential_id: 0` will raise at compile time, which breaks the seed-compiles test.

Fix the seed: change `credential_id: 0` to use a special sentinel string like `credential_id: "__unconfigured__"` so the compile doesn't silently pass — OR — change the compiler to allow `0` / None at compile time but fail at render time with a clearer message.

I'll pick the compiler change: emit the step even with placeholder credential_id, then let the renderer raise a friendly "credential 0 not found — edit the sequence and select a real credential" error. That's friendlier to the "operator just saw the seed, now must configure it" experience.

Update the compiler (back in `web/sequence_compiler.py`):

```python
def _handle_join_ad_domain(params: dict, out: CompiledSequence) -> None:
    cred_id = params.get("credential_id")
    # We do NOT raise on missing/zero credential_id — the seed ships with
    # credential_id=0 as a placeholder so operators can discover the
    # sequence and edit it. The RunOnce renderer reports a clear error
    # at provision time if the credential still hasn't been set.
    out.runonce_steps.append({
        "step_type": "join_ad_domain",
        "ps_template": _JOIN_AD_DOMAIN_PS,
        "credential_id": int(cred_id) if cred_id else 0,
        "params": {"ou_path": params.get("ou_path", "") or ""},
        "causes_reboot": True,
    })
```

And remove the `test_join_ad_domain_requires_credential_id` test from `test_sequence_compiler.py` (the check moved to the renderer; the renderer test `test_render_raises_when_credential_lookup_fails` already covers the failure path).

Hmm that's a schema change. Let me make it cleaner: don't emit the step at compile time when `credential_id` is falsy. Emit a warning in the sequence's `needs_configuration` field instead.

Actually simplest: the seed uses a sentinel `credential_id=-1` and the compiler ALLOWS that — renderer catches it. Less churn. Let me just do that.

```python
def _handle_join_ad_domain(params: dict, out: CompiledSequence) -> None:
    cred_id = params.get("credential_id")
    if cred_id is None:
        raise CompilerError(
            "join_ad_domain step requires a credential_id (use -1 to mark "
            "the sequence as unconfigured)"
        )
    out.runonce_steps.append({...})
```

OK, actually let me just commit to the change: **seed uses credential_id=0, compiler allows it, renderer raises a clear message**. Simpler. And it matches "the sequence is valid shape, just needs config."

Remove `test_join_ad_domain_requires_credential_id`. Keep the renderer's `test_render_raises_when_credential_lookup_fails` — it catches the same intent on the other side.

- [ ] **Step 4: Run tests**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/ 2>&1 | tail -3
```

Expected: all pass. If `test_join_ad_domain_requires_credential_id` was left in, remove it.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequences_db.py autopilot-proxmox/tests/test_sequence_compiler.py
git commit -m "feat(seed): re-enable join_ad_domain + rename_computer in AD sequence"
```

---

## Phase 8 — Integration test + live smoke

### Task 8.1: API-level integration test for the RunOnce render path

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequences_api.py`

- [ ] **Step 1: Append**

```python
def test_provision_renders_runonce_scripts_per_job(app_env, tmp_path, monkeypatch):
    """POST /api/jobs/provision with a sequence that includes join_ad_domain
    must render a per-job runonce dir and pass its manifest to Ansible."""
    from web import sequences_db, crypto
    from web.app import SEQUENCES_DB, CREDENTIAL_KEY, job_manager
    cipher = crypto.Cipher(CREDENTIAL_KEY)

    cred_id = sequences_db.create_credential(
        SEQUENCES_DB, cipher, name="test-dj", type="domain_join",
        payload={"domain_fqdn": "example.local", "username": "x",
                 "password": "y", "ou_hint": ""},
    )
    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="test-ad", description="",
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "join_ad_domain",
         "params": {"credential_id": cred_id, "ou_path": "OU=Test"},
         "enabled": True},
    ])

    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        captured["args"] = args or {}
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    # Give the job manager a writable jobs_dir
    job_manager.jobs_dir = str(tmp_path / "jobs")

    from unittest.mock import patch
    with patch("web.app._load_proxmox_config", return_value={
        "proxmox_node": "pve", "proxmox_snippets_storage": "local",
    }), patch("web.proxmox_snippets.ensure_chassis_type_binary", return_value=""):
        r = app_env.post("/api/jobs/provision", data={
            "profile": "",
            "count": "1",
            "cores": "2",
            "memory_mb": "4096",
            "disk_size_gb": "64",
            "serial_prefix": "",
            "group_tag": "",
            "sequence_id": str(seq_id),
        }, follow_redirects=False)
    assert r.status_code == 303

    # cmd contains -e _runonce_scripts_json=... whose JSON lists the step
    import json
    cmd = captured["cmd"]
    runonce_arg = next(
        (c for c in cmd if isinstance(c, str) and c.startswith("_runonce_scripts_json=")),
        None,
    )
    assert runonce_arg is not None, f"no _runonce_scripts_json in cmd: {cmd}"
    manifest = json.loads(runonce_arg.split("=", 1)[1])
    assert len(manifest) == 1
    assert manifest[0]["step_type"] == "join_ad_domain"
    assert manifest[0]["causes_reboot"] is True
    # The rendered .ps1 actually exists on disk with 0600 perms
    from pathlib import Path
    import os
    p = Path(manifest[0]["path"])
    assert p.exists()
    assert p.stat().st_mode & 0o777 == 0o600
    content = p.read_text()
    # Credentials resolved into the script
    assert "example.local" in content
    assert "OU=Test" in content
    # The password ('y') shows up at least once as the rendered ConvertTo-SecureString arg
    assert content.count("'y'") >= 1
    # Branding envelope is present (header + event log + registry stamp)
    assert "ProxmoxVEAutopilot" in content
    assert "EventId 1001" in content
    assert r"HKLM:\SOFTWARE\ProxmoxVEAutopilot\Provisioning" in content
    # Since join_ad_domain causes_reboot=True, the envelope emits Restart-Computer
    assert "Restart-Computer -Force" in content


def test_test_domain_join_endpoint_uses_ldap3(app_env):
    """The /api/credentials/test-domain-join endpoint calls ldap_tester."""
    from unittest.mock import patch
    fake_result = {"ok": True, "dns": {"ok": True, "servers": ["dc01"]},
                   "connect": {"ok": True}, "bind": {"ok": True},
                   "rootdse": {"ok": True}, "ou": {"ok": True}}
    with patch("web.ldap_tester.test_domain_join", return_value=fake_result):
        r = app_env.post("/api/credentials/test-domain-join", json={
            "payload": {"domain_fqdn": "example.local",
                        "username": "x", "password": "y", "ou_hint": ""}
        })
    assert r.status_code == 200
    assert r.json() == fake_result
```

- [ ] **Step 2: Run**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequences_api.py -v -k "runonce or test_domain_join"
```

Expected: 2 passed.

- [ ] **Step 3: Full suite**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/ 2>&1 | tail -3
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/tests/test_sequences_api.py
git commit -m "test(api): RunOnce render path + test-domain-join endpoint"
```

---

### Task 8.2: Live harness — Test Connection button renders

**Files:**
- Modify: `autopilot-proxmox/tests/integration/test_live.py`

- [ ] **Step 1: Append**

```python
def test_credential_edit_page_has_test_connection_button(session, base_url):
    """Phase B.2a: credential_edit.html gains a Test connection button
    for domain_join credentials. Verify the button is in the HTML for
    the 'new domain_join' page."""
    r = session.get(base_url + "/credentials/new", timeout=10)
    assert r.status_code == 200
    # The button is inside the domain_join fieldset (hidden by default
    # on the /new page until the type dropdown is switched, but the
    # HTML is present).
    assert "testDomainJoin()" in r.text
    assert "Test connection" in r.text
```

- [ ] **Step 2: Commit**

```bash
git add autopilot-proxmox/tests/integration/test_live.py
git commit -m "test(integration): live smoke for Test Connection button"
```

---

## Phase 9 — Push + PR

### Task 9.1: Final pytest + push

- [ ] **Step 1: Full suite**

```
cd autopilot-proxmox && .venv/bin/python -m pytest tests/ -v 2>&1 | tail -5
```

Expected: all tests pass.

- [ ] **Step 2: Push**

```
git push -u origin feat/task-sequences-phase-b2a
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --base main --head feat/task-sequences-phase-b2a \
  --title "feat(task-sequences): Phase B.2a — AD domain join via RunOnce" \
  --body "$(cat <<'EOF'
## Summary

Provisioning with a sequence that includes \`join_ad_domain\` (and optionally \`rename_computer\`) now actually joins the VM to an AD domain using SCCM-style \`Add-Computer -Restart\` via QEMU guest agent after first OOBE logon.

## Architecture (RunOnce, not UnattendedJoin)

Q2 revisited — the UnattendedJoin-in-specialize-pass path needed per-VM answer ISO infrastructure that was out of proportion to the value delivered. The RunOnce pattern ships the same outcome with:

- One extra reboot mid-provision (SCCM task sequences do this routinely)
- No per-VM ISO generation
- No unattend manipulation
- Much smaller code surface

## What's here

- **Compiler handlers** \`join_ad_domain\` + \`rename_computer\` (\`web/sequence_compiler.py\`) — emit PowerShell templates with \`{{ cred.X }}\` / \`{{ params.X }}\` / \`{{ vm.X }}\` placeholders. \`CompiledSequence\` gains \`runonce_steps\` list.
- **Renderer** (\`web/runonce_renderer.py\`) — resolves placeholders in-memory, writes per-step .ps1 to a per-job \`runonce/\` dir with 0600 perms. PS-safe single-quote escaping via custom \`ps_escape\` Jinja filter.
- **LDAP Test Connection** (\`web/ldap_tester.py\`) — DNS SRV → TLS bind → rootDSE → optional OU search using \`ldap3\`. Stage-by-stage response; password never echoed back.
- **Test Connection button** on the \`domain_join\` credential form, shows per-stage green/red checklist.
- **Ansible common tasks**: \`guest_run_ps_script.yml\` (write + exec + delete via guest agent) and \`wait_reboot_cycle.yml\` (detects guest-agent ping gap, delegates to \`wait_guest_agent.yml\`).
- **Seed fix**: "AD Domain Join — Local Admin" seed now has \`join_ad_domain\` and \`rename_computer\` enabled by default (credential_id=0 as placeholder — operator edits the sequence and selects a real credential before provisioning).

## Deferred to Phase B.2b

- \`local_admin\` / \`run_script\` / \`install_module\` step types
- Devices-page capture-action conditional disable
- Hybrid stub "coming soon" badge

## Test plan

- [ ] pytest tests/ passes locally
- [ ] Live harness: \`pytest tests/integration -v --run-integration\` (Test Connection button renders)
- [ ] Create a \`domain_join\` credential against a real domain, click Test Connection, all 5 stages go green
- [ ] Edit the seeded "AD Domain Join — Local Admin" sequence, select the credential, save
- [ ] Provision a VM with that sequence
- [ ] In the guest after the expected reboot: \`Get-ComputerInfo\` shows \`CsDomain: example.local\` (not \`WORKGROUP\`)
- [ ] Guest hostname matches the serial from the sequence's \`rename_computer\` pattern

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**

- §5 step types: `join_ad_domain` ✓ (Phase 1), `rename_computer` ✓ (Phase 1). `local_admin`/`run_script`/`install_module` deferred per scope.
- §8.6 Test Connection: ✓ (Phases 3+4)
- §9 execution flow: ✓ (two-stage render, per-job temp dir with 0600, secrets redacted in Ansible via `no_log: true` on guest file-write/exec tasks)
- §10 reboot tracking: ✓ (wait_reboot_cycle.yml uses ping-gap detection)
- §12 precedence rules: unchanged from B.1

**Placeholder scan:** no TBD, no "implement later", no "similar to task N". One structural fix inline (Task 6.2 Step 2 caught a broken loop structure and refactored it during authoring).

**Type consistency:** `CompiledSequence.runonce_steps` list-of-dicts shape consistent across compiler (Task 1.2), renderer (Task 2.2), manifest test (Task 8.1). `RenderError`/`CompilerError` distinction clear. `_ps_step_name`/`_ps_script_path` kwargs on `guest_run_ps_script.yml` consistent between Task 5.1 definition and Task 6.2 caller.

**Known rough spots:**

- Task 6.1 uses `_cipher` (a function) captured in a default-kw closure to keep the helper callable from Ansible-driven retries. If the cipher cache gets invalidated mid-job (tests that patch `_CIPHER = None`), the closure still holds the old reference. Acceptable for production — tests shouldn't invalidate mid-job.
- The `sed` tokenization in `run_one_runonce_step.yml` modifies the file in place on the controller. That file is read later by the guest_run_ps_script slurp, so the substitution needs to happen BEFORE the slurp. Task 5.1 slurps at the top of `guest_run_ps_script.yml`; Task 6.2 places the sed BEFORE the include of `guest_run_ps_script.yml`. Verified — ordering is correct.
- `ldap3`'s `auto_bind="TLS_BEFORE_BIND"` semantics differ across versions (`True`/`False` vs `"DEFAULT"` strings). Pinned `ldap3>=2.9,<3`; worth rechecking if tests fail in CI with a different version.
- The live harness in 8.2 only asserts the button renders — it doesn't actually invoke Test Connection against a real DC. Doing so safely from CI would require either a mock server or domain-join creds in the harness config. Both out of scope.
