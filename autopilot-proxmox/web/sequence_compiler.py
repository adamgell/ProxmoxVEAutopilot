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


StepHandler = Callable[[dict, CompiledSequence], None]


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


def _handle_set_oem_hardware(params: dict, out: CompiledSequence) -> None:
    profile = (params.get("oem_profile") or "").strip()
    if profile:
        out.ansible_vars["vm_oem_profile"] = profile
    # Optional chassis-type override. 0 / None / missing all mean "inherit
    # from the profile"; only positive integers emit the Ansible var.
    ct = params.get("chassis_type")
    try:
        ct_int = int(ct) if ct is not None else 0
    except (TypeError, ValueError):
        ct_int = 0
    if ct_int > 0:
        out.ansible_vars["chassis_type_override"] = str(ct_int)


def _handle_autopilot_entra(params: dict, out: CompiledSequence) -> None:
    out.autopilot_enabled = True
    out.ansible_vars["autopilot_enabled"] = "true"


def _handle_hybrid_stub(params: dict, out: CompiledSequence) -> None:
    raise StepNotImplemented("autopilot_hybrid")


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


def resolve_provision_vars(
    compiled: CompiledSequence,
    *,
    form_overrides: dict,
    vars_yml: dict,
) -> dict:
    """Merge three layers per spec §12: vars.yml < sequence < form."""
    merged: dict = {}
    # vars.yml (lowest) — only provisioning-relevant keys
    for key in ("vm_oem_profile", "chassis_type_override"):
        if vars_yml.get(key):
            merged[key] = vars_yml[key]
    # sequence-compiled vars
    merged.update(compiled.ansible_vars)
    # form overrides (highest) — blank/None inherits
    for key, value in form_overrides.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        merged[key] = value
    return merged
