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

import base64
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


@dataclass
class CompiledWinPEPhase:
    actions: list = field(default_factory=list)
    requires_windows_iso: bool = True
    requires_virtio_iso: bool = True
    expected_reboot_count: int = 1
    autopilot_enabled: bool = False
    # Note: actual AutopilotConfigurationFile.json bytes are NOT carried
    # on this struct. The /winpe/autopilot-config/<run_id> endpoint reads
    # them from autopilot_config_path at request time (matching what
    # roles/autopilot_inject does today) so updates to the file take
    # effect without recompiling existing runs.


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
    # Emit <ComputerName> into the specialize pass. Running at specialize
    # means the machine is renamed BEFORE join_ad_domain creates the
    # computer object — otherwise the object lands in AD as WIN-random
    # and the post-join Rename-Computer FLC silently fails (needs
    # -DomainCredential on a domain-joined machine).
    #
    # The pattern may contain {serial} and {vmid} tokens. We can't
    # substitute here because the per-sequence unattend is content-hash
    # cached across provisions; substituting per-VM would blow the
    # cache. Instead, emit sentinel placeholders that inject_unattend.yml
    # replaces with the actual values just before writing Panther.
    source = (params.get("name_source") or "serial").strip().lower()
    if source == "pattern":
        pattern = params.get("pattern") or ""
        if not pattern:
            raise CompilerError(
                "rename_computer with name_source='pattern' requires a "
                "non-empty 'pattern' param."
            )
    else:
        pattern = "{serial}"
    name_value = (
        pattern.replace("{serial}", "%AUTOPILOT_SERIAL%")
               .replace("{vmid}", "%AUTOPILOT_VMID%")
    )
    out.unattend_blocks["specialize_computer_name"] = _xml_escape(name_value)


def _encode_ps_command(script: str) -> str:
    """Return a PowerShell -EncodedCommand payload for ``script``.

    Base64 of UTF-16-LE is the encoding ``powershell.exe -EncodedCommand``
    expects. Going through this avoids every flavor of quoting hell we'd
    otherwise hit inside XML <CommandLine> → cmd.exe → powershell's
    triple-stacked parsers: operators can paste multi-line PowerShell
    with single quotes, double quotes, and shell metacharacters, and it
    all arrives verbatim at the interpreter.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _handle_run_script(params: dict, out: CompiledSequence,
                       resolver: Optional[Callable]) -> None:
    script = params.get("script") or ""
    if not script.strip():
        raise CompilerError(
            "run_script step requires a non-empty 'script' param."
        )
    name = (params.get("name") or "").strip() or "run_script"
    encoded = _encode_ps_command(script)
    out.first_logon_commands.append({
        "command": (
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass "
            f"-EncodedCommand {encoded}"
        ),
        "description": name,
    })
    # causes_reboot is caller-declared. The step editor exposes a
    # checkbox; when True, the post-provision waiter expects an extra
    # reboot cycle. The script itself is responsible for scheduling the
    # reboot (`shutdown /r /t 5` is the convention).
    if params.get("causes_reboot"):
        out.causes_reboot_count += 1


def _handle_install_module(params: dict, out: CompiledSequence,
                           resolver: Optional[Callable]) -> None:
    module = (params.get("module") or "").strip()
    if not module:
        raise CompilerError(
            "install_module step requires a 'module' param."
        )
    version    = (params.get("version")    or "").strip()
    repository = (params.get("repository") or "").strip() or "PSGallery"
    scope      = (params.get("scope")      or "").strip() or "AllUsers"

    # Three pre-reqs on a fresh Windows image:
    #   1. TLS 1.2 — PSGallery rejected TLS 1.0 years ago.
    #   2. NuGet provider — Install-Module uses it under the hood.
    #   3. Repository trust — without it, Install-Module prompts for
    #      confirmation and silently hangs inside FLC (no stdin).
    # Single-quote literals for PS strings so they survive XML's
    # whitespace handling unchanged.  All user-supplied values are
    # passed through _ps_escape so an embedded single quote doesn't
    # produce invalid PowerShell syntax.
    r = _ps_escape(repository)
    m = _ps_escape(module)
    s = _ps_escape(scope)
    lines = [
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor "
        "[Net.SecurityProtocolType]::Tls12",
        f"if ((Get-PSRepository -Name '{r}' -ErrorAction SilentlyContinue)"
        f".InstallationPolicy -ne 'Trusted') {{ "
        f"Set-PSRepository -Name '{r}' -InstallationPolicy Trusted }}",
        "if (-not (Get-PackageProvider -Name NuGet -ErrorAction SilentlyContinue)) { "
        "Install-PackageProvider -Name NuGet -Force -Scope AllUsers | Out-Null }",
    ]
    install_args = [
        f"-Name '{m}'",
        f"-Repository '{r}'",
        f"-Scope '{s}'",
        "-Force",
        "-AllowClobber",
    ]
    if version:
        install_args.append(f"-RequiredVersion '{_ps_escape(version)}'")
    lines.append("Install-Module " + " ".join(install_args))
    script = "; ".join(lines)
    encoded = _encode_ps_command(script)
    out.first_logon_commands.append({
        "command": (
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass "
            f"-EncodedCommand {encoded}"
        ),
        "description": f"Install-Module {module}"
                       + (f" {version}" if version else ""),
    })


_STEP_HANDLERS: dict[str, StepHandler] = {
    "set_oem_hardware": _handle_set_oem_hardware,
    "autopilot_entra": _handle_autopilot_entra,
    "autopilot_hybrid": _handle_hybrid_stub,
    "local_admin": _handle_local_admin,
    "join_ad_domain": _handle_join_ad_domain,
    "rename_computer": _handle_rename_computer,
    "run_script": _handle_run_script,
    "install_module": _handle_install_module,
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

    When auto-logon is enabled (local_admin with autologon=True — the
    default), a final FirstLogonCommand is appended that reboots once
    all other commands have run. This consumes the auto-logon (which
    used LogonCount=1) and lands the guest at the Windows login screen
    instead of an auto-logged-in Administrator desktop. The
    reboot-cycle count is bumped so the post-provision waiter follows
    the guest through.
    """
    out = CompiledSequence()
    for step in sequence.get("steps", []):
        if not step.get("enabled", True):
            continue
        handler = _STEP_HANDLERS.get(step["step_type"])
        if handler is None:
            raise UnknownStepType(step["step_type"])
        handler(step.get("params", {}), out, resolve_credential)
    _append_final_reboot_if_autologon(out)
    return out


def _append_final_reboot_if_autologon(out: CompiledSequence) -> None:
    """Finalize FirstLogonCommands so the VM ends at the Windows logon
    screen (not auto-logged-in as the local admin). Only meaningful
    when auto-logon was configured — without it, FirstLogonCommands
    don't run at all.

    The finalizer has to do three things atomically:
      1. Disable AutoAdminLogon in the Winlogon registry key. LogonCount
         alone isn't enough — Windows sometimes re-arms auto-logon to
         finish "pending" FirstLogonCommands across a reboot triggered
         by an earlier command (e.g., rename_computer's /r /t 5).
      2. Wipe the cached DefaultPassword / AutoLogonCount values so a
         later human enabling auto-logon doesn't inherit our creds.
      3. Cancel any already-scheduled shutdown and schedule our own.
         Windows only allows one pending shutdown at a time, so without
         ``shutdown /a`` our reboot is silently dropped when an earlier
         FLC already scheduled one.
    """
    if "oobe_auto_logon" not in out.unattend_blocks:
        return
    # Run PowerShell DIRECTLY — no cmd.exe /c wrapper. cmd.exe's /c
    # parser terminates the outer quoted block at the first `\"` it
    # sees (it treats `\` as literal and `"` as end-of-quote, NOT as
    # an escape-pair), which ate everything after `$k=` in testing on
    # VM 121 — Set-ItemProperty was invoked with -Path <empty>, no
    # registry change happened, VM came back auto-logged-in again.
    # All the unattend's other FLCs use this pattern: single pair of
    # outer double quotes around -Command, single-quoted literals
    # inside. XML un-escapes `2&gt;$null` → `2>$null` at load time so
    # PowerShell's stderr redirect works.
    ps = (
        "$k='HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon'; "
        "Set-ItemProperty -Path $k -Name AutoAdminLogon -Type String -Value '0' -Force; "
        "Remove-ItemProperty -Path $k -Name AutoLogonCount -ErrorAction SilentlyContinue; "
        "Remove-ItemProperty -Path $k -Name DefaultPassword -ErrorAction SilentlyContinue; "
        "Remove-ItemProperty -Path $k -Name DefaultUserName -ErrorAction SilentlyContinue; "
        # Cancel whatever shutdown rename_computer (or similar) queued,
        # then schedule ours. /t 15 > /t 5 so the earlier command's
        # countdown is aborted and our reboot is the one that fires.
        "shutdown.exe /a 2>$null; "
        "shutdown.exe /r /t 15 /c 'Provisioning complete, rebooting to logon screen'"
    )
    out.first_logon_commands.append({
        "command": (
            'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "'
            + ps + '"'
        ),
        "description": "Disable auto-logon and reboot to logon screen",
    })
    out.causes_reboot_count += 1


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


# ---------------------------------------------------------------------------
# WinPE phase compiler
# ---------------------------------------------------------------------------

def _sequence_has_autopilot(sequence: dict) -> bool:
    for step in sequence.get("steps", []) or []:
        if not step.get("enabled", True):
            continue
        if step.get("step_type") == "autopilot_entra":
            return True
    return False


def compile_winpe(sequence: dict,
                  resolver: Optional[Callable] = None,
                  ) -> "CompiledWinPEPhase":
    """Compile the phase-0 (WinPE) action list for a sequence.

    Returns the canonical action list every WinPE run executes. The
    ordering is fixed; operator-authored steps do not appear in this
    output (they flow through compile() and become FLC entries).
    """
    out = CompiledWinPEPhase()

    autopilot = _sequence_has_autopilot(sequence)
    capture_hash_in_winpe = (
        bool(sequence.get("produces_autopilot_hash"))
        and sequence.get("hash_capture_phase") == "winpe"
    )

    if capture_hash_in_winpe:
        out.actions.append({"kind": "capture_hash", "params": {}})

    out.actions.append({
        "kind": "partition_disk",
        "params": {"layout": "recovery_before_c"},
    })
    out.actions.append({
        "kind": "apply_wim",
        "params": {"image_index_metadata_name": "Windows 11 Enterprise"},
    })
    out.actions.append({
        "kind": "inject_drivers",
        "params": {
            "required_infs": [
                "vioscsi.inf", "netkvm.inf", "vioser.inf",
                "balloon.inf", "vioinput.inf",
            ],
        },
    })
    out.actions.append({
        "kind": "validate_boot_drivers",
        "params": {
            "required_infs": [
                "vioscsi.inf", "netkvm.inf", "vioser.inf",
            ],
        },
    })

    if autopilot:
        # Phase 0 applies Windows to V:\ (the soon-to-be-C:\ partition).
        # The agent runs from X:\ (WinPE RAM drive) and has no C:\, so
        # we MUST stage to V:\Windows\... here. The OS sees this as
        # C:\Windows\... after first boot when V: is remapped to C:.
        out.actions.append({
            "kind": "stage_autopilot_config",
            "params": {
                "guest_path": (
                    "V:\\Windows\\Provisioning\\Autopilot\\"
                    "AutopilotConfigurationFile.json"
                ),
            },
        })
        out.autopilot_enabled = True

    out.actions.append({"kind": "bake_boot_entry", "params": {}})
    out.actions.append({"kind": "stage_unattend", "params": {}})

    return out
