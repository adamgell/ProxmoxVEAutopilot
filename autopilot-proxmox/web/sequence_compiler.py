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

# `New-LocalUser` fails if the account already exists, so we gate on
# Get-LocalUser to make re-runs idempotent (operators who re-push a
# sequence against a VM should not blow up on the second provision).
# AccountExpires:$false avoids Windows defaulting to a 30-day expiry on
# cmdlet-created accounts; PasswordNeverExpires keeps lab VMs out of
# forced-rotation territory.
_LOCAL_ADMIN_PS = r"""$secure = ConvertTo-SecureString '{{ cred.password | ps_escape }}' -AsPlainText -Force
$u = Get-LocalUser -Name '{{ cred.username | ps_escape }}' -ErrorAction SilentlyContinue
if ($null -eq $u) {
    New-LocalUser -Name '{{ cred.username | ps_escape }}' `
                  -Password $secure `
                  -FullName '{{ cred.username | ps_escape }}' `
                  -AccountNeverExpires `
                  -PasswordNeverExpires
} else {
    Set-LocalUser -Name '{{ cred.username | ps_escape }}' -Password $secure
}
$group = (Get-LocalGroup -SID 'S-1-5-32-544').Name  # localized 'Administrators'
if (-not (Get-LocalGroupMember -Group $group -Member '{{ cred.username | ps_escape }}' -ErrorAction SilentlyContinue)) {
    Add-LocalGroupMember -Group $group -Member '{{ cred.username | ps_escape }}'
}
"""

# `Install-Module` on a fresh Windows image needs three pre-reqs:
# NuGet provider, TLS 1.2 (PSGallery rejects TLS 1.0 since 2020), and
# the target repo trusted (otherwise it prompts for confirmation even
# with -Force). We set each idempotently so re-runs are cheap.
_INSTALL_MODULE_PS = r"""[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$repo = '{{ params.repository | ps_escape }}'
if ([string]::IsNullOrWhiteSpace($repo)) { $repo = 'PSGallery' }
if ((Get-PSRepository -Name $repo -ErrorAction SilentlyContinue).InstallationPolicy -ne 'Trusted') {
    Set-PSRepository -Name $repo -InstallationPolicy Trusted
}
if (-not (Get-PackageProvider -Name NuGet -ErrorAction SilentlyContinue)) {
    Install-PackageProvider -Name NuGet -Force -Scope AllUsers | Out-Null
}
$scope = '{{ params.scope | ps_escape }}'
if ([string]::IsNullOrWhiteSpace($scope)) { $scope = 'AllUsers' }
$installArgs = @{
    Name       = '{{ params.module | ps_escape }}'
    Repository = $repo
    Scope      = $scope
    Force      = $true
    AllowClobber = $true
}
if ('{{ params.version | ps_escape }}' -ne '') {
    $installArgs['RequiredVersion'] = '{{ params.version | ps_escape }}'
}
Install-Module @installArgs
"""

# `run_script` is just the operator's script body wrapped in the
# branding envelope. `script` is pasted into the generated .ps1
# verbatim — it's already inside the outer try{} so errors bubble up
# to the envelope's catch block (Event 1099 + Registry Failure stamp).
# No ps_escape here: the script is PowerShell, not a PS single-quoted
# literal.
_RUN_SCRIPT_PS = r"""{{ params.script }}
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


def _handle_rename_computer(params: dict, out: CompiledSequence) -> None:
    pattern = params.get("pattern", "{serial}") or "{serial}"
    out.runonce_steps.append({
        "step_type": "rename_computer",
        "ps_template": _RENAME_COMPUTER_PS,
        "credential_id": None,
        "params": {"pattern": pattern},
        "causes_reboot": True,
    })


def _handle_local_admin(params: dict, out: CompiledSequence) -> None:
    # Same credential_id=0 tolerance as join_ad_domain — the seed ships
    # with the default-local-admin credential pre-wired, but if the
    # operator cleared it the renderer reports a clear error at
    # provision time.
    cred_id = params.get("credential_id")
    out.runonce_steps.append({
        "step_type": "local_admin",
        "ps_template": _LOCAL_ADMIN_PS,
        "credential_id": int(cred_id) if cred_id else 0,
        "params": {},
        "causes_reboot": False,
    })


def _handle_run_script(params: dict, out: CompiledSequence) -> None:
    script = params.get("script", "") or ""
    if not script.strip():
        raise CompilerError("run_script step requires a non-empty 'script' param")
    # Reboot is caller-declared — the sequence editor exposes a
    # "causes reboot" checkbox. When True, the renderer appends
    # Restart-Computer -Force; Ansible then waits the ping-gap.
    out.runonce_steps.append({
        "step_type": "run_script",
        "ps_template": _RUN_SCRIPT_PS,
        "credential_id": None,
        "params": {"script": script},
        "causes_reboot": bool(params.get("causes_reboot")),
    })


def _handle_install_module(params: dict, out: CompiledSequence) -> None:
    module = (params.get("module") or "").strip()
    if not module:
        raise CompilerError("install_module step requires a 'module' param")
    out.runonce_steps.append({
        "step_type": "install_module",
        "ps_template": _INSTALL_MODULE_PS,
        "credential_id": None,
        "params": {
            "module":     module,
            "version":    (params.get("version") or "").strip(),
            "repository": (params.get("repository") or "").strip(),
            "scope":      (params.get("scope") or "").strip(),
        },
        "causes_reboot": False,
    })


_STEP_HANDLERS: dict[str, StepHandler] = {
    "set_oem_hardware": _handle_set_oem_hardware,
    "autopilot_entra": _handle_autopilot_entra,
    "autopilot_hybrid": _handle_hybrid_stub,
    "join_ad_domain": _handle_join_ad_domain,
    "rename_computer": _handle_rename_computer,
    "local_admin": _handle_local_admin,
    "run_script": _handle_run_script,
    "install_module": _handle_install_module,
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
