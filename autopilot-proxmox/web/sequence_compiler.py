"""Compile a task sequence into a bundle of Ansible variables + unattend artifacts.

Pure function — takes the sequence dict (as returned by sequences_db.get_sequence)
and an optional credential resolver, returns a CompiledSequence. No DB access,
no file I/O, no network. The resolver is the only injection point for secrets;
plaintext credentials appear only inside compiled artifact strings, never as
attributes on the CompiledSequence.

The output has four buckets that downstream wiring consumes:

* ``ansible_vars``: existing ``-e key=value`` overrides for ansible-playbook.
* ``unattend_blocks``: XML fragments keyed by the Jinja block name they replace
  in ``files/autounattend.xml.j2``. Compiler fills only blocks it has content
  for; the template's default for each block matches today's hardcoded output
  so unchanged sequences compile byte-identically to the static file.
* ``first_logon_commands``: ordered list of ``{command, description}`` entries,
  rendered into the ``<FirstLogonCommands>`` block by the unattend template.
* ``causes_reboot_count``: how many reboot cycles the post-provision waiter
  should expect. Incremented by any step that triggers a guest-side reboot
  outside of Windows's normal specialize→OOBE transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional
from xml.sax.saxutils import escape as _xml_escape


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


class CredentialMissing(CompilerError):
    """Step references a credential but the resolver returned nothing."""


@dataclass
class CompiledSequence:
    ansible_vars: dict = field(default_factory=dict)
    unattend_blocks: dict = field(default_factory=dict)
    first_logon_commands: list = field(default_factory=list)
    autopilot_enabled: bool = False
    causes_reboot_count: int = 0


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

StepHandler = Callable[[dict, CompiledSequence, Optional[Callable]], None]


def _handle_set_oem_hardware(params: dict, out: CompiledSequence,
                             resolver: Optional[Callable]) -> None:
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


def _handle_autopilot_entra(params: dict, out: CompiledSequence,
                            resolver: Optional[Callable]) -> None:
    out.autopilot_enabled = True
    out.ansible_vars["autopilot_enabled"] = "true"


def _handle_hybrid_stub(params: dict, out: CompiledSequence,
                        resolver: Optional[Callable]) -> None:
    raise StepNotImplemented("autopilot_hybrid")


def _require_credential(params: dict, resolver: Optional[Callable],
                        step_type: str) -> dict:
    cid = params.get("credential_id")
    if cid is None:
        raise CredentialMissing(
            f"{step_type} step has no credential_id set; edit the sequence "
            f"and pick a credential."
        )
    if resolver is None:
        raise CredentialMissing(
            f"{step_type} step needs a credential but the compile call did "
            f"not provide a resolver (compiler was invoked without access "
            f"to decrypted credentials)."
        )
    payload = resolver(int(cid))
    if not payload:
        raise CredentialMissing(
            f"{step_type} step references credential id={cid} but no "
            f"such credential exists (it may have been deleted)."
        )
    return payload


def _handle_local_admin(params: dict, out: CompiledSequence,
                        resolver: Optional[Callable]) -> None:
    payload = _require_credential(params, resolver, "local_admin")
    username = (payload.get("username") or "").strip() or "Administrator"
    password = payload.get("password") or ""
    if not password:
        raise CredentialMissing(
            "local_admin credential has no password (create or edit the "
            "credential and set one)."
        )
    out.unattend_blocks["oobe_user_accounts"] = _render_user_accounts(
        username, password,
    )
    # AutoLogon is needed so FirstLogonCommands can run as SYSTEM-elevated.
    # Sequences can explicitly opt out by setting params.autologon=False.
    if params.get("autologon", True):
        out.unattend_blocks["oobe_auto_logon"] = _render_auto_logon(
            username, password,
        )


def _handle_join_ad_domain(params: dict, out: CompiledSequence,
                           resolver: Optional[Callable]) -> None:
    payload = _require_credential(params, resolver, "join_ad_domain")
    domain_fqdn = (payload.get("domain_fqdn") or "").strip()
    raw_username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not (domain_fqdn and raw_username and password):
        raise CredentialMissing(
            "join_ad_domain credential is missing one of domain_fqdn, "
            "username, password."
        )
    # Windows UnattendedJoin expects a BARE <Username>. When <Domain>
    # is also provided, Windows concatenates them as `<Domain>\<Username>`;
    # a credential stored as `DOMAIN\user` (NetBIOS) or `user@domain`
    # (UPN) would double up and produce
    # `home.gell.one\home\adam_admin` → ERROR_BAD_USERNAME (0x89a).
    # Parse both forms and emit the bare account name.
    user_domain, user_bare = _split_domain_user(raw_username)
    # Step-level ou_path overrides the credential's ou_hint; blanks fall
    # through.
    ou = (params.get("ou_path") or payload.get("ou_hint") or "").strip()
    out.unattend_blocks["specialize_identification"] = _render_join_domain(
        domain_fqdn=domain_fqdn,
        # If the credential username embedded a domain, prefer it for
        # <Domain> (preserves cross-domain join scenarios). Otherwise
        # default to the target join domain — UnattendedJoin docs say
        # the creds typically belong to the joining domain.
        credential_domain=user_domain or domain_fqdn,
        username=user_bare,
        password=password,
        ou_path=ou or None,
    )


def _split_domain_user(raw: str) -> tuple[str, str]:
    """Split a credential username into (domain, bare-username).

    Accepts ``DOMAIN\\user``, ``user@domain``, or a bare ``user``.
    Returns ``('', 'user')`` when no domain can be extracted.
    """
    if "\\" in raw:
        d, u = raw.split("\\", 1)
        return d.strip(), u.strip()
    if "@" in raw:
        u, d = raw.rsplit("@", 1)
        return d.strip(), u.strip()
    return "", raw.strip()


def _handle_rename_computer(params: dict, out: CompiledSequence,
                            resolver: Optional[Callable]) -> None:
    source = (params.get("name_source") or "serial").strip().lower()
    if source == "pattern":
        pattern = params.get("pattern") or ""
        if not pattern:
            raise CompilerError(
                "rename_computer with name_source='pattern' requires a "
                "non-empty 'pattern' param."
            )
        # The pattern is evaluated on the guest via PowerShell format
        # strings: {vmid} → $env:AUTOPILOT_VMID (set below), {serial} →
        # SMBIOS SerialNumber. We wire both so sequences can choose.
        new_name_expr = (
            "($pattern = '" + _ps_escape(pattern) + "'); "
            "$serial = (Get-CimInstance Win32_BIOS).SerialNumber; "
            "$vmid = $env:AUTOPILOT_VMID; "
            "$new = $pattern.Replace('{serial}', $serial)"
            ".Replace('{vmid}', $vmid)"
        )
    else:
        # Default: rename to SMBIOS SerialNumber.
        new_name_expr = (
            "$new = (Get-CimInstance Win32_BIOS).SerialNumber"
        )
    cmd = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \""
        + new_name_expr + "; "
        "Rename-Computer -NewName $new -Force; "
        "shutdown.exe /r /t 5 /c 'Autopilot rename restart'\""
    )
    out.first_logon_commands.append({
        "command": cmd,
        "description": "Rename computer and reboot",
    })
    out.causes_reboot_count += 1


_STEP_HANDLERS: dict[str, StepHandler] = {
    "set_oem_hardware": _handle_set_oem_hardware,
    "autopilot_entra": _handle_autopilot_entra,
    "autopilot_hybrid": _handle_hybrid_stub,
    "local_admin": _handle_local_admin,
    "join_ad_domain": _handle_join_ad_domain,
    "rename_computer": _handle_rename_computer,
}


# ---------------------------------------------------------------------------
# Fragment rendering — kept here (not in Jinja) so tests can assert on them
# independently from the full unattend template.
# ---------------------------------------------------------------------------


_INDENT = "      "  # 6 spaces, matching autounattend.xml inside <component>


def _render_user_accounts(username: str, password: str) -> str:
    return (
        f"{_INDENT}<UserAccounts>\n"
        f"{_INDENT}  <LocalAccounts>\n"
        f"{_INDENT}    <LocalAccount wcm:action=\"add\">\n"
        f"{_INDENT}      <Name>{_xml_escape(username)}</Name>\n"
        f"{_INDENT}      <Group>Administrators</Group>\n"
        f"{_INDENT}      <Password>\n"
        f"{_INDENT}        <Value>{_xml_escape(password)}</Value>\n"
        f"{_INDENT}        <PlainText>true</PlainText>\n"
        f"{_INDENT}      </Password>\n"
        f"{_INDENT}    </LocalAccount>\n"
        f"{_INDENT}  </LocalAccounts>\n"
        f"{_INDENT}</UserAccounts>"
    )


def _render_auto_logon(username: str, password: str) -> str:
    return (
        f"{_INDENT}<AutoLogon>\n"
        f"{_INDENT}  <Enabled>true</Enabled>\n"
        f"{_INDENT}  <Username>{_xml_escape(username)}</Username>\n"
        f"{_INDENT}  <Password>\n"
        f"{_INDENT}    <Value>{_xml_escape(password)}</Value>\n"
        f"{_INDENT}    <PlainText>true</PlainText>\n"
        f"{_INDENT}  </Password>\n"
        f"{_INDENT}  <LogonCount>1</LogonCount>\n"
        f"{_INDENT}</AutoLogon>"
    )


def _render_join_domain(*, domain_fqdn: str, credential_domain: str,
                        username: str, password: str,
                        ou_path: Optional[str]) -> str:
    ou_fragment = (
        f"{_INDENT}  <MachineObjectOU>{_xml_escape(ou_path)}</MachineObjectOU>\n"
        if ou_path else ""
    )
    return (
        f"{_INDENT}<Identification>\n"
        f"{_INDENT}  <Credentials>\n"
        f"{_INDENT}    <Domain>{_xml_escape(credential_domain)}</Domain>\n"
        f"{_INDENT}    <Username>{_xml_escape(username)}</Username>\n"
        f"{_INDENT}    <Password>{_xml_escape(password)}</Password>\n"
        f"{_INDENT}  </Credentials>\n"
        f"{_INDENT}  <JoinDomain>{_xml_escape(domain_fqdn)}</JoinDomain>\n"
        f"{ou_fragment}"
        f"{_INDENT}</Identification>"
    )


def _ps_escape(s: str) -> str:
    """Escape a string for a single-quoted PowerShell literal."""
    return s.replace("'", "''")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compile(sequence: dict,
            *,
            resolve_credential: Optional[Callable[[int], dict]] = None
            ) -> CompiledSequence:
    """Resolve a sequence to a CompiledSequence.

    Iterates enabled steps in order, dispatching to per-type handlers. The
    ``resolve_credential`` callable is threaded through so handlers that
    need decrypted secrets can fetch them lazily. Handlers that don't use
    credentials ignore the argument.
    """
    out = CompiledSequence()
    for step in sequence.get("steps", []):
        if not step.get("enabled", True):
            continue
        handler = _STEP_HANDLERS.get(step["step_type"])
        if handler is None:
            raise UnknownStepType(step["step_type"])
        handler(step.get("params", {}), out, resolve_credential)
    return out


def resolve_provision_vars(
    compiled: CompiledSequence,
    *,
    form_overrides: dict,
    vars_yml: dict,
) -> dict:
    """Merge three layers per spec §12: vars.yml < sequence < form."""
    merged: dict = {}
    for key in ("vm_oem_profile", "chassis_type_override"):
        if vars_yml.get(key):
            merged[key] = vars_yml[key]
    merged.update(compiled.ansible_vars)
    for key, value in form_overrides.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        merged[key] = value
    return merged
