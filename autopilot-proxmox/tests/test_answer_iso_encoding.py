"""Regression tests for answer-ISO encoding + ASCII-cleanliness.

Windows PowerShell 5.1 reads BOM-less .ps1 files as the system ANSI
codepage, not UTF-8. Any multi-byte char (em-dash, arrow, smart quote)
then mis-decodes and the parser throws MissingEndCurlyBrace on unrelated
lines - silently breaking FirstLogonCommand execution.

T17 acc-1 on 2026-04-24 hit exactly this: new QGA/UTM-guest-tools install
blocks added em-dashes to firstboot.ps1.j2, the rendered file had no BOM,
FLC's powershell.exe -File failed parse, no log, no scheduled task, VM
never halted. Two defenses:

  1. Forbid em/en-dashes, arrows, and smart quotes in Windows-bound
     template files (firstboot.ps1.j2, unattend/autounattend.xml[.j2]).
  2. Write the rendered firstboot.ps1 with UTF-8 BOM (encoding='utf-8-sig')
     so PS 5.1 detects UTF-8 regardless of source content.
"""
import pathlib
import tempfile
import xml.etree.ElementTree as ET

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FORBIDDEN_CHARS = {
    "—": "em-dash",
    "–": "en-dash",
    "→": "rightwards arrow",
    "←": "leftwards arrow",
    "‘": "left single quote",
    "’": "right single quote",
    "“": "left double quote",
    "”": "right double quote",
}

WINDOWS_BOUND_TEMPLATES = [
    "files/autounattend.xml",
    "files/autounattend.xml.j2",
    "roles/utm_answer_iso/templates/firstboot.ps1.j2",
    "roles/utm_answer_iso/templates/unattend.xml.j2",
]


@pytest.mark.parametrize("rel_path", WINDOWS_BOUND_TEMPLATES)
def test_windows_bound_template_is_ascii_clean(rel_path: str) -> None:
    path = REPO_ROOT / rel_path
    text = path.read_text(encoding="utf-8")
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for ch in line:
            if ch in FORBIDDEN_CHARS:
                hits.append(f"{rel_path}:{i}: {FORBIDDEN_CHARS[ch]} in {line.strip()!r}")
                break
    assert not hits, "Forbidden non-ASCII chars found:\n" + "\n".join(hits[:10])


def test_unattend_stages_vioserial_driver_for_qga() -> None:
    """The UTM ARM64 unattend must add Microsoft-Windows-PnpCustomizationsNonWinPE
    in the offlineServicing pass with DriverPaths entries for vioserial.

    Without this, qemu-ga-aarch64.msi installs cleanly but its QEMU-GA
    service start hangs on CreateFile \\\\.\\Global\\org.qemu.guest_agent.0
    until the 4-minute SCM start timeout fires (MSI Error 1920 -> exit 1603).

    The WinPE PnpCustomizationsWinPE pass does not persist drivers into
    the OS driver store; only offlineServicing or auditSystem do, and
    offlineServicing is canonical for non-boot-critical drivers per
    learn.microsoft.com.
    """
    from web.answer_iso import render_arm64_unattend

    rendered = render_arm64_unattend({
        "admin_user": "Administrator",
        "admin_pass": "P@ssw0rd!",
        "locale": "en-US",
        "timezone": "Pacific Standard Time",
        "windows_edition": "Windows 11 Pro",
        "vm_name": "test-vm",
    })
    root = ET.fromstring(rendered)
    ns = {"u": "urn:schemas-microsoft-com:unattend"}

    offline_settings = root.findall("u:settings[@pass='offlineServicing']", ns)
    assert offline_settings, "offlineServicing pass missing from unattend.xml"

    paths = set()
    for s in offline_settings:
        for comp in s.findall("u:component", ns):
            if "PnpCustomizationsNonWinPE" not in (comp.get("name") or ""):
                continue
            for p in comp.findall("u:DriverPaths/u:PathAndCredentials/u:Path", ns):
                paths.add((p.text or "").strip())

    assert any("vioserial" in p and "ARM64" in p for p in paths), (
        f"offlineServicing DriverPaths missing vioserial ARM64 entry; got {paths}"
    )


def test_rendered_firstboot_ps1_has_utf8_bom() -> None:
    from web.answer_iso import stage_answer_iso_files

    with tempfile.TemporaryDirectory() as td:
        staging = pathlib.Path(td)
        stage_answer_iso_files(staging, profile={
            "admin_user": "Administrator",
            "admin_pass": "P@ssw0rd!",
            "locale": "en-US",
            "timezone": "Pacific Standard Time",
            "windows_edition": "Windows 11 Pro",
            "vm_name": "test-vm",
            "qemu_ga_msi_path": "",
            "utm_guest_tools_exe_path": "",
        })
        firstboot = staging / "$OEM$" / "$1" / "autopilot" / "firstboot.ps1"
        assert firstboot.is_file(), f"firstboot.ps1 not staged at {firstboot}"
        raw = firstboot.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", (
            f"firstboot.ps1 missing UTF-8 BOM; starts with {raw[:10]!r}"
        )
