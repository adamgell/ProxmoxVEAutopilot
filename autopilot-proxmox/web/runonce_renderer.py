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
