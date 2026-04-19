# Task Sequences — Phase B.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Phase A sequence data into the real provisioning flow — the default sequence reproduces today's behavior byte-identically, and a non-default sequence can change which OEM profile is used and whether Autopilot injection runs.

**Architecture:** A new `web/sequence_compiler.py` resolves a sequence (+ provision-form overrides + `vars.yml` defaults) into a small dict of Ansible variables. The provision route passes those vars to `ansible-playbook` via `-e`. The existing `_provision_clone_vm.yml` gates `autopilot_inject` on a compiled `autopilot_enabled` flag instead of the legacy `autopilot_skip` variable. `vm_provisioning` rows are written after each successful clone so the Devices page can later hide Autopilot-hash actions for non-Autopilot sequences.

**Tech Stack:** Python (compiler + FastAPI), Ansible (existing playbook), pytest. No new dependencies.

**Spec reference:** `docs/superpowers/specs/2026-04-19-task-sequences-design.md` sections 9 (execution flow), 12 (precedence), 13 (seeded content).

---

## Scope decisions

**In scope for B.1 (this plan):**

- Compiler module with TWO step-type handlers only: `set_oem_hardware` → `vm_oem_profile`, `autopilot_entra` → `autopilot_enabled=true`.
- Precedence resolution: provision-UI field > sequence step param > `vars.yml` default. Blank UI field inherits.
- Wire `/api/jobs/provision` POST handler to compile and pass resolved vars to `ansible-playbook`.
- Modify `_provision_clone_vm.yml` to gate `autopilot_inject` on the new `autopilot_enabled` variable (falls back to the existing `autopilot_skip` logic when the new var is unset — no back-compat break).
- Record `vm_provisioning(vmid, sequence_id)` after each successful clone so the Devices page can join on it later.
- No-regression test: the seeded "Entra Join (default)" sequence + today's `vars.yml` produce the same Ansible vars (`vm_oem_profile`, `autopilot_enabled=true`) as the status quo.

**Out of scope for B.1 (deferred to B.2):**

- Compiling the unattend XML itself. The existing `files/unattend_oobe.xml` is consumed at *template-build* time (baked into the template's answer ISO) — not at clone time. Per-sequence OOBE variation requires new infrastructure (per-clone answer ISO, or guest-agent injection of `unattend.xml` into `C:\Windows\Panther\` before specialize). That infrastructure ships in Phase B.2 alongside the remaining step types.
- `local_admin` step type compilation (needs the unattend mechanism).
- `join_ad_domain`, `rename_computer`, `run_script`, `install_module` step types.
- Reboot-aware waiter.
- Devices-page capture-action conditional disable (will use the `vm_provisioning` rows this phase writes).
- "Test connection" button.
- `autopilot_hybrid` "coming soon" badge on the step-type dropdown.

**Why this scope ships value:** Phase A let users *define* sequences but the data sat inert. Phase B.1 is the minimum wire-up where choosing a different sequence actually changes VM provisioning behavior — specifically, picking a non-default sequence can skip Autopilot injection and pick a different OEM profile. That's enough to validate the architecture end-to-end before the bigger B.2 work.

---

## File Structure (Phase B.1)

**New files:**

- `autopilot-proxmox/web/sequence_compiler.py` — one responsibility: resolve a (sequence_id, form_overrides, vars_yml_defaults) tuple into an Ansible-ready dict.
- `autopilot-proxmox/tests/test_sequence_compiler.py` — unit tests for each step-type handler + precedence resolution.

**Modified files:**

- `autopilot-proxmox/web/app.py` — `start_provision()` handler calls the compiler when a `sequence_id` form field is present, passes `-e` args; also registers a post-job hook that records `vm_provisioning` rows.
- `autopilot-proxmox/playbooks/_provision_clone_vm.yml` — the existing `autopilot_inject` include gets a new `when:` clause that prefers `autopilot_enabled` over `autopilot_skip`.
- `autopilot-proxmox/web/jobs.py` — small hook for "on job completion, invoke a callback". The Phase A patch already added `set_arg`; this adds an on-complete callback list.
- `autopilot-proxmox/tests/test_sequences_api.py` — add one integration test for the provision flow with a compiled sequence.

---

## Phase 1 — Sequence compiler (pure function, no I/O)

### Task 1.1: Compiler skeleton + `set_oem_hardware` handler — failing tests

**Files:**
- Create: `autopilot-proxmox/tests/test_sequence_compiler.py`

- [ ] **Step 1: Create the test file**

```python
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
    """autopilot_hybrid is the Phase A stub — compiler must refuse clearly
    rather than silently producing nothing (spec §11)."""
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "autopilot_hybrid", "params": {}}])
    with pytest.raises(sequence_compiler.StepNotImplemented) as exc:
        sequence_compiler.compile(seq)
    assert "autopilot_hybrid" in str(exc.value)
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v`

Expected: all tests fail with `ModuleNotFoundError: No module named 'web.sequence_compiler'`.

---

### Task 1.2: Implement compiler module with `set_oem_hardware`

**Files:**
- Create: `autopilot-proxmox/web/sequence_compiler.py`

- [ ] **Step 1: Create the module**

```python
"""Compile a task sequence into a bundle of Ansible variables.

Pure function — takes the sequence dict (as returned by sequences_db.get_sequence)
and returns a CompiledSequence. No DB access, no file I/O, no network.

Only the step types needed for Phase B.1 are implemented: set_oem_hardware
and autopilot_entra. Unknown step types raise UnknownStepType; stubs
(autopilot_hybrid) raise StepNotImplemented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class CompilerError(Exception):
    """Base class for compiler errors."""


class UnknownStepType(CompilerError):
    def __init__(self, step_type: str):
        super().__init__(f"unknown step type: {step_type!r}")
        self.step_type = step_type


class StepNotImplemented(CompilerError):
    def __init__(self, step_type: str):
        super().__init__(
            f"step type {step_type!r} is not implemented in this version"
        )
        self.step_type = step_type


@dataclass
class CompiledSequence:
    """The resolved form of a sequence.

    ``ansible_vars`` is a flat dict the provisioning playbook will receive
    via ``-e key=value`` on the ansible-playbook command line.
    ``autopilot_enabled`` is broken out because it has its own truthy
    semantics and callers want to gate on it.
    """
    ansible_vars: dict = field(default_factory=dict)
    autopilot_enabled: bool = False


# Step-type handler signature: (params, out: CompiledSequence) -> None.
StepHandler = Callable[[dict, CompiledSequence], None]


def _handle_set_oem_hardware(params: dict, out: CompiledSequence) -> None:
    profile = (params.get("oem_profile") or "").strip()
    # Empty string = inherit from vars.yml. Only emit when a value is set.
    if profile:
        out.ansible_vars["vm_oem_profile"] = profile


def _handle_autopilot_entra(params: dict, out: CompiledSequence) -> None:
    out.autopilot_enabled = True
    out.ansible_vars["autopilot_enabled"] = "true"


def _handle_hybrid_stub(params: dict, out: CompiledSequence) -> None:
    raise StepNotImplemented("autopilot_hybrid")


_STEP_HANDLERS: dict[str, StepHandler] = {
    "set_oem_hardware": _handle_set_oem_hardware,
    "autopilot_entra": _handle_autopilot_entra,
    "autopilot_hybrid": _handle_hybrid_stub,
}


def compile(sequence: dict) -> CompiledSequence:
    """Resolve a sequence to a CompiledSequence.

    Iterates enabled steps in order, dispatching to per-type handlers.
    Unknown types raise UnknownStepType.
    """
    out = CompiledSequence()
    for step in sequence.get("steps", []):
        if not step.get("enabled", True):
            continue
        handler = _STEP_HANDLERS.get(step["step_type"])
        if handler is None:
            raise UnknownStepType(step["step_type"])
        handler(step.get("params", {}), out)
    return out
```

- [ ] **Step 2: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v`

Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequence_compiler.py
git commit -m "feat(compiler): add sequence compiler with set_oem_hardware + autopilot_entra"
```

---

### Task 1.3: `autopilot_entra` handler test coverage

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequence_compiler.py`

- [ ] **Step 1: Append tests for the autopilot_entra handler**

```python
def test_autopilot_entra_sets_flag_and_var():
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "autopilot_entra", "params": {}}])
    result = sequence_compiler.compile(seq)
    assert result.autopilot_enabled is True
    assert result.ansible_vars["autopilot_enabled"] == "true"


def test_seeded_entra_default_compiles_to_expected_vars():
    """Byte-identical check: the Phase A seed 'Entra Join (default)' must
    produce exactly these Ansible vars — any change breaks the no-regression
    guarantee we gave when making task sequences the default."""
    from web import sequence_compiler
    seq = _make_sequence([
        {"step_type": "set_oem_hardware", "params": {"oem_profile": ""}},
        {"step_type": "local_admin",
         "params": {"credential_id": 1}, "enabled": False},  # disabled: not impl in B.1
        {"step_type": "autopilot_entra", "params": {}},
    ], is_default=True)
    result = sequence_compiler.compile(seq)
    # Empty oem_profile inherits — no vm_oem_profile emitted
    assert "vm_oem_profile" not in result.ansible_vars
    # Autopilot is enabled
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
```

- [ ] **Step 2: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v`

Expected: 9 passed.

Note: the test named `test_seeded_entra_default_compiles_to_expected_vars` intentionally marks the `local_admin` step as `enabled=False`. The real Phase A seed has it `enabled=True`, but since `local_admin` isn't implemented yet, the seeded sequence will fail to compile until B.2 unless we disable it or add a skip-unknown flag. We address this in Task 4.1 where we update the seed to mark unimplemented steps as `enabled=False` until B.2.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/tests/test_sequence_compiler.py
git commit -m "test(compiler): cover autopilot_entra and merged multi-step output"
```

---

## Phase 2 — Provision-flow precedence

### Task 2.1: Precedence helper — failing tests

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequence_compiler.py`

- [ ] **Step 1: Append precedence tests**

```python
def test_precedence_ui_over_sequence_over_varsyml():
    """UI form values override sequence; blank UI inherits; sequence
    overrides vars.yml; blank sequence step inherits vars.yml (spec §12)."""
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
        # set_oem_hardware with blank profile — no value from sequence
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
    """autopilot_enabled is a sequence-level compiled fact; vars.yml
    historically uses autopilot_skip which is a different flag. The
    compiled autopilot_enabled always wins over both."""
    from web import sequence_compiler
    seq = _make_sequence([{"step_type": "autopilot_entra", "params": {}}])
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled, form_overrides={}, vars_yml={"autopilot_skip": "true"},
    )
    assert resolved["autopilot_enabled"] == "true"


def test_precedence_empty_sequence_preserves_legacy_varsyml():
    """A sequence with no autopilot step should NOT emit autopilot_enabled —
    legacy behavior (autopilot_skip from vars.yml) stays intact."""
    from web import sequence_compiler
    seq = _make_sequence([])
    compiled = sequence_compiler.compile(seq)
    resolved = sequence_compiler.resolve_provision_vars(
        compiled, form_overrides={}, vars_yml={},
    )
    assert "autopilot_enabled" not in resolved
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v -k precedence`

Expected: fail with `AttributeError: module 'web.sequence_compiler' has no attribute 'resolve_provision_vars'`.

---

### Task 2.2: Implement `resolve_provision_vars`

**Files:**
- Modify: `autopilot-proxmox/web/sequence_compiler.py`

- [ ] **Step 1: Append the precedence resolver**

```python
def resolve_provision_vars(
    compiled: CompiledSequence,
    *,
    form_overrides: dict,
    vars_yml: dict,
) -> dict:
    """Merge three layers of configuration per spec §12.

    Precedence (lowest → highest):
        vars.yml defaults  <  sequence step params  <  provision-form fields

    A blank or missing value in an upper layer inherits from the layer below.
    Compiler-produced facts like ``autopilot_enabled`` always originate from
    the sequence; they don't have vars.yml peers.
    """
    merged: dict = {}
    # Layer 1: vars.yml (lowest priority) — only keys relevant to provisioning
    # that the existing roles already read.
    for key in ("vm_oem_profile",):
        if vars_yml.get(key):
            merged[key] = vars_yml[key]
    # Layer 2: sequence-compiled vars override
    merged.update(compiled.ansible_vars)
    # Layer 3: non-blank form overrides win
    for key, value in form_overrides.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        merged[key] = value
    return merged
```

- [ ] **Step 2: Run precedence tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v -k precedence`

Expected: 4 passed.

- [ ] **Step 3: Full compiler test run**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequence_compiler.py -v`

Expected: 13 passed (6 + 3 + 4).

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequence_compiler.py
git commit -m "feat(compiler): add vars.yml→sequence→form precedence resolver"
```

---

## Phase 3 — Ansible wiring

### Task 3.1: Gate autopilot_inject on the new `autopilot_enabled` var

**Files:**
- Modify: `autopilot-proxmox/playbooks/_provision_clone_vm.yml`

**Current behavior:** The `autopilot_inject` role runs when `autopilot_skip` is falsy (default true means it runs). We need to respect the new `autopilot_enabled` flag **when it is defined**; otherwise fall back to today's `autopilot_skip` logic so existing `vars.yml`-only installs keep working.

- [ ] **Step 1: Edit the playbook**

Find the `Inject Autopilot config into VM` task (around line 18) and replace its `when:` clause:

```yaml
- name: "Inject Autopilot config into VM {{ vm_vmid }}"
  ansible.builtin.include_role:
    name: autopilot_inject
  when: >-
    (autopilot_enabled is defined and (autopilot_enabled | bool))
    or
    (autopilot_enabled is not defined and not (autopilot_skip | default(false) | bool))
```

The new `when:` reads: "If the sequence compiler set `autopilot_enabled`, use it. Otherwise preserve the legacy `autopilot_skip` gate."

- [ ] **Step 2: Lint the YAML**

Run: `cd autopilot-proxmox && .venv/bin/python -c "import yaml; yaml.safe_load(open('playbooks/_provision_clone_vm.yml'))" && echo ok`

Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/playbooks/_provision_clone_vm.yml
git commit -m "feat(ansible): gate autopilot_inject on compiled autopilot_enabled var"
```

---

## Phase 4 — Seed fix + web wire-up

### Task 4.1: Mark unimplemented-in-B.1 seed steps as `enabled=False`

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`
- Modify: `autopilot-proxmox/tests/test_sequences_db.py`

**Why:** Phase A seeded `"Entra Join (default)"` with three steps: `set_oem_hardware`, `local_admin`, `autopilot_entra`. The compiler in B.1 doesn't know about `local_admin` — compiling the seeded default would raise `UnknownStepType`. Mark `local_admin` as `enabled=False` in the seed so the compiler skips it; B.2 will re-enable it when the handler lands.

- [ ] **Step 1: Update `_SEED_SEQUENCES` in `sequences_db.py`**

Find the `Entra Join (default)` entry and change the `local_admin` step to `enabled=False` with an inline comment:

```python
{
    "name": "Entra Join (default)",
    "description": "Entra-joined via Windows Autopilot. Matches the pre-sequence hardcoded flow.",
    "is_default": True,
    "produces_autopilot_hash": True,
    "steps": [
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": ""},
         "enabled": True},
        {"step_type": "local_admin",
         "params": {"credential_name": "default-local-admin"},
         # Disabled until Phase B.2 wires the unattend ISO mechanism
         # that actually consumes the local-admin output. The local-admin
         # credentials today come from the template's baked unattend.
         "enabled": False},
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ],
},
```

Also update the `"AD Domain Join — Local Admin"` seed: set `local_admin`, `join_ad_domain`, and `rename_computer` steps all to `enabled=False` (B.2 will re-enable them as those handlers land).

- [ ] **Step 2: Update the seed tests to reflect the new enabled-state**

In `tests/test_sequences_db.py`, find `test_seed_defaults_inserts_three_on_empty` and below, and add a new test:

```python
def test_seed_default_sequence_b1_compiles_cleanly(db_path, key_path):
    """After B.1, the seeded default sequence must compile without error
    using only the B.1 step-type handlers. Any step not yet implemented
    must be marked enabled=False in the seed."""
    from web import crypto, sequences_db, sequence_compiler
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    default_id = sequences_db.get_default_sequence_id(db_path)
    seq = sequences_db.get_sequence(db_path, default_id)
    compiled = sequence_compiler.compile(seq)  # must not raise
    assert compiled.autopilot_enabled is True
```

- [ ] **Step 3: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequences_db.py tests/test_sequence_compiler.py -v`

Expected: all pass (existing seed tests + new no-regression test).

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequences_db.py
git commit -m "fix(seed): disable not-yet-implemented steps in seeded sequences"
```

---

### Task 4.2: Provision handler — compile and pass vars

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

First read the current `start_provision` handler (search for `@app.post("/api/jobs/provision")`) to understand its signature before editing. It currently accepts form fields, builds the `ansible-playbook` command with `-e` args, and calls `job_manager.start(...)`. The Phase A patch added a `sequence_id` form field that's currently only stashed on the job metadata.

- [ ] **Step 1: Add compiler import at the top of `app.py`**

Under the other `from web import ...` lines:

```python
from web import sequence_compiler
```

- [ ] **Step 2: Extend the `start_provision` handler to compile the sequence**

Locate the handler and find where it builds the `-e` args list. Replace the current sequence-handling block (post-Phase A: `if sequence_id: job_manager.set_arg(...)`) with:

```python
    # Resolve sequence → Ansible vars (spec §12 precedence).
    resolved_vars: dict = {}
    if sequence_id:
        seq = sequences_db.get_sequence(SEQUENCES_DB, sequence_id)
        if seq is None:
            raise HTTPException(404, f"sequence {sequence_id} not found")
        try:
            compiled = sequence_compiler.compile(seq)
        except sequence_compiler.CompilerError as e:
            raise HTTPException(400, f"sequence compile failed: {e}")
        # UI form fields the operator may have filled in as overrides.
        form_overrides = {
            "vm_oem_profile": profile,  # existing form var named 'profile'
        }
        resolved_vars = sequence_compiler.resolve_provision_vars(
            compiled,
            form_overrides=form_overrides,
            vars_yml=_load_vars(),
        )
```

- [ ] **Step 3: Merge `resolved_vars` into the `-e` args list the playbook receives**

Find the list comprehension or loop that builds the `-e key=value` args. Immediately before the final `ansible-playbook ... -e ...` assembly, add:

```python
    for key, value in resolved_vars.items():
        # Existing -e args from the form may have already set the same key;
        # if so, leave them (form already captured in resolved_vars too).
        if any(arg.startswith(f"{key}=") for arg in extra_vars):
            continue
        extra_vars.append(f"{key}={value}")
```

(Adjust `extra_vars` to match the variable name the existing code uses for the `-e` list. Read the handler first to confirm.)

- [ ] **Step 4: Record sequence_id on the job (already done in Phase A; keep it)**

The existing `job_manager.set_arg(job["id"], "sequence_id", sequence_id)` call stays — we'll use it in Task 4.3 to write the `vm_provisioning` row after completion.

- [ ] **Step 5: Full pytest run**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ -v`

Expected: all pass. The existing provision-flow tests don't cover the compiled-vars path yet; we add one in Task 4.4.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "feat(web): compile selected sequence into ansible-playbook -e args"
```

---

### Task 4.3: Record `vm_provisioning` after a clone completes

**Files:**
- Modify: `autopilot-proxmox/web/jobs.py`
- Modify: `autopilot-proxmox/web/app.py`

**Approach:** When the Ansible playbook finishes successfully, we need to know which `vmid`(s) were allocated and map each to the selected `sequence_id`. The playbook doesn't currently emit this in a machine-readable form, but every successful clone logs `Cloned VM '{vm_name}' (VMID: {vm_vmid}) from template ...` via `debug`. We'll scrape the job log for `VMID: \d+` lines at completion time.

- [ ] **Step 1: Add an on-complete hook to `JobManager`**

In `autopilot-proxmox/web/jobs.py`, find the method that finalizes a job (where it writes the final status / flushes the log). Read enough of the file to know the existing shape. Then add near the top of the class:

```python
    def add_on_complete(self, job_id: str, callback) -> None:
        """Register a callback(job_dict) to run when the job finishes.
        Callbacks run in the job-runner thread. Exceptions are logged and
        swallowed — a bad callback must not poison job status."""
        with self._lock:
            self._on_complete.setdefault(job_id, []).append(callback)
```

And initialize `self._on_complete: dict[str, list] = {}` in `__init__`. In the method that handles job completion, after the final status is set, iterate:

```python
        for cb in self._on_complete.pop(job_id, []):
            try:
                cb(job)
            except Exception as e:
                # Log but don't propagate — a bad callback must not change
                # the job's externally-visible status.
                self._log(job_id, f"[on_complete] callback error: {e}")
```

(Adjust method names to match what's in `jobs.py`.)

- [ ] **Step 2: Register a callback in `start_provision` that records vm_provisioning**

In `app.py`, immediately after the `job = job_manager.start(...)` line in `start_provision`, add:

```python
    if sequence_id:
        sid = int(sequence_id)
        def _record_vms(job_dict, sid=sid):
            import re
            log_path = Path(job_manager.jobs_dir) / job_dict["id"] / "output.log"
            if not log_path.exists():
                return
            text = log_path.read_text(errors="replace")
            for m in re.finditer(r"VMID: (\d+)", text):
                try:
                    sequences_db.record_vm_provisioning(
                        SEQUENCES_DB, vmid=int(m.group(1)), sequence_id=sid,
                    )
                except Exception:
                    pass  # DAL raises on constraint violations; skip
        job_manager.add_on_complete(job["id"], _record_vms)
```

This parses the Ansible log for `VMID: \d+` patterns (emitted by the existing final `debug` task in `proxmox_vm_clone/tasks/main.yml`) and records each.

- [ ] **Step 3: Run tests**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ -v`

Expected: all pass (no test asserts the callback runs yet).

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/jobs.py autopilot-proxmox/web/app.py
git commit -m "feat(web): record vm_provisioning rows after a sequence-driven clone"
```

---

### Task 4.4: Integration test — provision with a sequence passes compiled vars

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequences_api.py`

- [ ] **Step 1: Append an integration test**

```python
def test_provision_with_sequence_passes_compiled_vars(app_env, monkeypatch):
    """POST /api/jobs/provision with sequence_id=<default> calls
    ansible-playbook with autopilot_enabled=true in the -e args."""
    from web import sequences_db, crypto
    # Seed a sequence directly (bypass the startup seed so the test fixture's
    # init-without-seed stays valid).
    from web.app import SEQUENCES_DB, CREDENTIAL_KEY
    cipher = crypto.Cipher(CREDENTIAL_KEY)
    cred_id = sequences_db.create_credential(
        SEQUENCES_DB, cipher, name="la", type="local_admin",
        payload={"username": "Administrator", "password": "x"},
    )
    seq_id = sequences_db.create_sequence(
        SEQUENCES_DB, name="test-entra", description="",
        is_default=True, produces_autopilot_hash=True,
    )
    sequences_db.set_sequence_steps(SEQUENCES_DB, seq_id, [
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "lenovo-t14"}, "enabled": True},
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])

    # Capture the command JobManager.start is called with.
    from web.app import job_manager
    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd)
        captured["args"] = args or {}
        return {"id": "fake-job"}
    job_manager.start.side_effect = fake_start
    job_manager.set_arg = lambda *a, **k: None
    job_manager.add_on_complete = lambda *a, **k: None

    r = app_env.post("/api/jobs/provision", data={
        "profile": "",            # blank UI → inherit from sequence
        "count": "1",
        "cores": "2",
        "memory_mb": "4096",
        "disk_size_gb": "64",
        "serial_prefix": "",
        "group_tag": "",
        "sequence_id": str(seq_id),
    }, follow_redirects=False)
    assert r.status_code == 303

    cmd = captured["cmd"]
    # -e autopilot_enabled=true must appear
    assert "autopilot_enabled=true" in cmd
    # -e vm_oem_profile=lenovo-t14 must appear (from sequence, since UI blank)
    assert "vm_oem_profile=lenovo-t14" in cmd
```

- [ ] **Step 2: Run**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/test_sequences_api.py -v -k sequence_passes`

Expected: PASS.

- [ ] **Step 3: Full suite**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ -v`

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/tests/test_sequences_api.py
git commit -m "test(api): sequence-driven provision passes compiled vars to ansible"
```

---

## Phase 5 — Smoke + PR

### Task 5.1: Manual smoke

- [ ] **Step 1: Full pytest pass**

Run: `cd autopilot-proxmox && .venv/bin/python -m pytest tests/ -v`

Expected: all tests pass (should be ~85+ after this phase: 78 from B.1 predecessors + ~9 compiler tests + 1 integration + 1 seed-compiles test).

- [ ] **Step 2: Smoke import**

Run: `cd autopilot-proxmox && .venv/bin/python -c "from web import app, sequence_compiler; print('ok')"`

Expected: `ok`.

- [ ] **Step 3: Optional — render provision page and verify dropdown is populated**

Start uvicorn locally and curl `/provision`:

```bash
cd autopilot-proxmox && .venv/bin/python -m uvicorn web.app:app --host 127.0.0.1 --port 5050 &
sleep 2
curl -s http://127.0.0.1:5050/provision | grep -o 'Entra Join (default)'
kill %1
```

Expected: prints `Entra Join (default)`.

---

### Task 5.2: Push branch + open PR

- [ ] **Step 1: Verify clean state**

Run: `git status`
Expected: clean.

- [ ] **Step 2: Push**

Run: `git push -u origin <branch-name>`

Use the implementation branch from the controller (likely `feat/task-sequences-spec` if you're appending to Phase A's PR, or a new `feat/task-sequences-phase-b1` branch).

- [ ] **Step 3: Open PR**

```bash
gh pr create --base main --head <branch-name> \
  --title "feat(task-sequences): Phase B.1 — compiler + autopilot gating" \
  --body "$(cat <<'EOF'
## Summary

Wires Phase A's sequence data into the provisioning flow. Chosen sequence now affects which OEM profile is used and whether Autopilot injection runs. Default sequence reproduces today's behaviour byte-identically in the vars it produces.

## What's here

- \`web/sequence_compiler.py\` — pure-function compiler for \`set_oem_hardware\` and \`autopilot_entra\` steps, plus a precedence resolver (UI > sequence > vars.yml)
- \`_provision_clone_vm.yml\` — gates \`autopilot_inject\` on the new \`autopilot_enabled\` var (falls back to legacy \`autopilot_skip\` when unset — no back-compat break)
- \`/api/jobs/provision\` — calls the compiler, merges resolved vars into the \`ansible-playbook -e\` args
- \`vm_provisioning\` DB rows written after each successful clone by scraping \`VMID: \\d+\` from the job log

## What's deferred to Phase B.2

- Unattend compilation (needs per-clone answer ISO or guest-agent injection infra — significant new plumbing)
- \`local_admin\`, \`join_ad_domain\`, \`rename_computer\`, \`run_script\`, \`install_module\` step types
- Reboot-aware waiter
- Devices-page capture-action conditional disable (will use the \`vm_provisioning\` rows this PR writes)
- "Test connection" button
- \`autopilot_hybrid\` "coming soon" badge

## Test plan

- [ ] pytest tests/ passes
- [ ] Provision page shows Task Sequence dropdown with "Entra Join (default)" selected
- [ ] Provision a VM with default sequence → Autopilot injection runs, same as before
- [ ] Provision with a new sequence where \`autopilot_entra\` step is DELETED → Autopilot injection is skipped
- [ ] Devices page shows newly-provisioned VMs (no UI change in this PR)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage (vs. sections 9, 12, 13):**

- §9 execution flow: compiler → ansible-playbook `-e` args ✓ (Task 4.2). Artifact file generation for unattend is deferred to B.2 and called out in the Out-of-Scope section.
- §12 precedence: ✓ (Task 2.1 + 2.2).
- §13 seeded content: the seeded default compiles cleanly in B.1 ✓ (Task 4.1 disables not-yet-implemented steps); full sequence re-enables in B.2.
- Autopilot-hash compatibility (§8.7): `vm_provisioning` rows are written (Task 4.3) but the Devices page doesn't use them yet. Phase B.2 adds the UI.

**Placeholder scan:** no "TBD", "implement later", or "similar to task N" instances.

**Type consistency:** `CompiledSequence` is the dataclass shape across all tasks. `UnknownStepType`/`StepNotImplemented`/`CompilerError` hierarchy is consistent. `resolve_provision_vars` signature is stable across Task 2.1 (tests) and Task 2.2 (impl).

**Known rough spots:**
- Task 4.2 asks the implementer to locate the existing `extra_vars` list in `start_provision`. Name may differ — the task instructs to read the handler first. If the existing handler structure doesn't have a convenient list to extend, the implementer should report BLOCKED with details.
- Task 4.3 depends on the existing `proxmox_vm_clone` role emitting `VMID: \d+` in its final debug line. Verified against `roles/proxmox_vm_clone/tasks/main.yml:117` in Phase A research; if that line changes upstream, the regex needs to track.
- The integration test in Task 4.4 mocks `job_manager.start` heavily. If the real handler already has its own fixture pattern in `test_web.py` or elsewhere, prefer that pattern to avoid duplication.
