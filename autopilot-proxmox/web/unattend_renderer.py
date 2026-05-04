"""Render a per-VM ``autounattend.xml`` from a :class:`CompiledSequence`.

Takes a ``CompiledSequence`` (from :mod:`web.sequence_compiler`) plus the
path to the Jinja template (``files/autounattend.xml.j2``) and produces
the final XML. Any block the compiler left empty falls back to the
hardcoded default in :data:`_DEFAULTS`, so a sequence whose only effects
are Ansible-vars (e.g., seed-1 "Entra Join") compiles to bytes
indistinguishable from today's static ``autounattend.xml``.

The renderer is deliberately not a full Jinja-inheritance setup — each
replaceable section is a single ``{{ var }}`` expression. Simpler to
reason about, and the byte-identical regression test catches drift.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from jinja2 import Environment, StrictUndefined

from web.sequence_compiler import CompiledSequence


_FILES_DIR = Path(__file__).resolve().parent.parent / "files"
_TEMPLATE_PATH = _FILES_DIR / "autounattend.xml.j2"
_POST_WINPE_TEMPLATE_PATH = _FILES_DIR / "autounattend.post_winpe.xml.j2"


# Defaults for the per-clone unattend.
#
# These were originally a verbatim copy of files/autounattend.xml's
# OOBE block — including <AutoLogon>Enabled=true</AutoLogon> — so that
# an "empty" sequence rendered byte-identically to the static unattend
# the template was built with. That decision was correct for the
# template-install autounattend (post-install needs AutoLogon to run
# FirstLogonCommands as Administrator: install QEMU GA, enable RDP,
# etc.) but wrong for clones.
#
# Pre-Phase-B, sysprepped clones had NO per-VM unattend — they booted
# into a fresh OOBE where Autopilot's self-deploying mode could fire.
# Post-Phase-B, the "byte-identical" default copied AutoLogon onto
# every clone, auto-completing OOBE as Administrator and bypassing
# Autopilot entirely (the AutopilotConfigurationFile.json is read
# DURING OOBE; once it's auto-completed, the file sits unused on
# disk).
#
# Empty defaults restore the pre-Phase-B clone behavior:
#   - Clones land at OOBE → Autopilot self-deploys if a config + a
#     registered hash exist
#   - QEMU guest agent runs as a Windows service from boot, so
#     ansible's autopilot_inject + hash_capture still reach it without
#     needing a logged-in user
#   - Sequences that DO want auto-logon (FLC chains, AD domain join's
#     local_admin step) opt in explicitly via _handle_local_admin,
#     which sets oobe_user_accounts + oobe_auto_logon when the step
#     is enabled.
_DEFAULT_USER_ACCOUNTS = ""
_DEFAULT_AUTO_LOGON = ""


_IDENT_COMPONENT_WRAPPER = """

    <component name="Microsoft-Windows-UnattendedJoin"
               processorArchitecture="amd64" language="neutral"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               publicKeyToken="31bf3856ad364e35" versionScope="nonSxS">
{inner}
    </component>"""


def _wrap_identification(inner: str) -> str:
    """Wrap the compiler's <Identification> fragment in the
    Microsoft-Windows-UnattendedJoin component. Returns the empty string
    if the compiler didn't emit an <Identification>."""
    if not inner:
        return ""
    return _IDENT_COMPONENT_WRAPPER.format(inner=inner)


def _render_first_logon_extras(commands: list) -> str:
    """Render additional FirstLogonCommand entries. Orders start at 4
    (after the three hardcoded defaults)."""
    if not commands:
        return ""
    lines = []
    for i, entry in enumerate(commands, start=4):
        cmd = entry["command"]
        desc = entry.get("description", "")
        # Escape only the bare minimum for XML text content: &, <, >.
        # Quotes inside <CommandLine> are fine because the element uses
        # text content, not an attribute.
        def _esc(s: str) -> str:
            return (s.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
        lines.append(
            "\n        <SynchronousCommand wcm:action=\"add\">"
            f"\n          <Order>{i}</Order>"
            f"\n          <CommandLine>{_esc(cmd)}</CommandLine>"
            f"\n          <Description>{_esc(desc)}</Description>"
            "\n        </SynchronousCommand>"
        )
    return "".join(lines)


def render_unattend(compiled: CompiledSequence,
                    *,
                    template_path: Optional[Path] = None,
                    phase_layout: str = "full") -> str:
    """Render unattend XML bytes for the given compiled sequence.

    phase_layout:
      "full"        -- include the windowsPE pass (default; clone path).
      "post_winpe"  -- omit windowsPE; for the WinPE provisioning path.
    """
    if phase_layout not in ("full", "post_winpe"):
        raise ValueError(f"invalid phase_layout: {phase_layout!r}")
    if template_path is None:
        path = (_POST_WINPE_TEMPLATE_PATH if phase_layout == "post_winpe"
                else _TEMPLATE_PATH)
    else:
        path = template_path
    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    template = env.from_string(path.read_text())
    blocks = compiled.unattend_blocks
    return template.render(
        oobe_user_accounts=blocks.get("oobe_user_accounts",
                                      _DEFAULT_USER_ACCOUNTS),
        oobe_auto_logon=blocks.get("oobe_auto_logon",
                                   _DEFAULT_AUTO_LOGON),
        specialize_computer_name=blocks.get("specialize_computer_name", "*"),
        specialize_identification_component=_wrap_identification(
            blocks.get("specialize_identification", "")),
        extra_first_logon_commands=_render_first_logon_extras(
            compiled.first_logon_commands),
    )
