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


# Defaults match files/autounattend.xml verbatim so an "empty" sequence
# (no unattend_blocks, no first_logon_commands) renders byte-identical
# output. Update both together.

_DEFAULT_USER_ACCOUNTS = """\
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>Administrator</Name>
            <Group>Administrators</Group>
            <Password>
              <Value>Nsta1200!!</Value>
              <PlainText>true</PlainText>
            </Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>"""


_DEFAULT_AUTO_LOGON = """\
      <AutoLogon>
        <Enabled>true</Enabled>
        <Username>Administrator</Username>
        <Password>
          <Value>Nsta1200!!</Value>
          <PlainText>true</PlainText>
        </Password>
        <LogonCount>1</LogonCount>
      </AutoLogon>"""


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
                    template_path: Optional[Path] = None) -> str:
    """Render ``autounattend.xml`` bytes for the given compiled sequence."""
    path = template_path or _TEMPLATE_PATH
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
        specialize_identification_component=_wrap_identification(
            blocks.get("specialize_identification", "")),
        extra_first_logon_commands=_render_first_logon_extras(
            compiled.first_logon_commands),
    )
