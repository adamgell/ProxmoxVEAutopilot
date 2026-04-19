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


StepHandler = Callable[[dict, CompiledSequence], None]


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
