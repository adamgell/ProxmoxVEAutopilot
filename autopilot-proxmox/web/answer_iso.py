"""UTM answer ISO generator for Windows 11 ARM64.

Generates an ``autounattend.xml`` tailored for Windows 11 ARM64 and
packages it (plus the optional firstboot.ps1 payload) into a hybrid ISO
labeled ``AUTOUNATTEND`` using ``hdiutil makehybrid`` (built into macOS).
Windows Setup auto-detects media with that volume label.

OEM profile field mapping (from oem_profile_resolver):
  _oem_profile.manufacturer  → profile["org_name"]
  vm_name                    → profile["hostname"]   (overridable)
  vm_oem_profile key         → profile["oem_profile"]

Sentinel protocol:
  firstboot.ps1 writes ``C:\\autopilot\\autopilot-firstboot.done`` when
  it completes.  The ``utm_build_win11_template.yml`` playbook polls for
  that file via ``utmctl exec`` before suspending the template VM.

CLI usage (called by the utm_answer_iso role):
  python3 web/answer_iso.py \\
      --vm-name win11-template \\
      --output-dir output/answer-isos \\
      --profile-json '{"hostname":"WIN11-TPL","admin_user":"Administrator",...}'

  --preview-only: print rendered unattend.xml to stdout, do not build ISO.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, StrictUndefined

_ROLE_TEMPLATES = (
    Path(__file__).resolve().parent.parent
    / "roles" / "utm_answer_iso" / "templates"
)


def _xml_esc(s: str) -> str:
    """Minimal XML text-content escaping (& < >)."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_jinja_env() -> Environment:
    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["xml_escape"] = _xml_esc
    return env


def _build_template_context(profile: dict) -> dict:
    """Normalise the profile dict into a flat set of template variables."""
    return {
        "hostname": profile.get("hostname") or "*",
        "locale": profile.get("locale") or "en-US",
        "timezone": profile.get("timezone") or "Pacific Standard Time",
        "admin_user": profile.get("admin_user") or "Administrator",
        "admin_pass": profile.get("admin_pass") or "",
        "org_name": profile.get("org_name") or "",
        "product_key": profile.get("product_key") or "",
        "windows_edition": profile.get("windows_edition") or "Windows 11 Pro",
        "domain_join": profile.get("domain_join") or {},
        "firstboot_cmds": profile.get("firstboot_cmds") or [],
    }


def render_arm64_unattend(profile: dict) -> str:
    """Render ``autounattend.xml`` for Windows 11 ARM64 from a profile dict.

    Uses ``roles/utm_answer_iso/templates/unattend.xml.j2``.
    All profile fields are optional — omitted fields fall back to safe
    defaults (locale en-US, timezone PST, admin user Administrator, etc.).
    """
    template_path = _ROLE_TEMPLATES / "unattend.xml.j2"
    env = _build_jinja_env()
    template = env.from_string(template_path.read_text())
    return template.render(**_build_template_context(profile))


def render_firstboot_ps1(profile: dict) -> str:
    """Render ``firstboot.ps1`` from the Jinja2 template.

    Uses ``roles/utm_answer_iso/templates/firstboot.ps1.j2``.
    """
    template_path = _ROLE_TEMPLATES / "firstboot.ps1.j2"
    env = _build_jinja_env()
    template = env.from_string(template_path.read_text())
    return template.render(**_build_template_context(profile))


_QEMU_GA_MSI_DEFAULT = (
    Path(__file__).resolve().parent.parent
    / "assets" / "qemu-ga-aarch64-win" / "qemu-ga-aarch64.msi"
)

_UTM_GUEST_TOOLS_DEFAULT = (
    Path(__file__).resolve().parent.parent
    / "assets" / "utm-guest-tools-win" / "utm-guest-tools-0.1.271.exe"
)


def _stage_optional_asset(oem_dir: Path, profile_key: str, default_path: Path,
                          profile: dict, staged_name: str) -> None:
    """Copy an optional asset into ``oem_dir`` under *staged_name*.

    The asset source is taken from ``profile[profile_key]`` (empty string
    or ``null`` disables), falling back to ``default_path``. Missing
    files are silently skipped — test envs and callers that disable the
    asset both benefit from not warning.
    """
    src_str = profile.get(profile_key, str(default_path))
    if not src_str:
        return
    src = Path(src_str).expanduser()
    if src.is_file():
        shutil.copyfile(src, oem_dir / staged_name)


def stage_answer_iso_files(staging_dir: Path, profile: dict) -> None:
    """Populate *staging_dir* with ISO contents.

    Layout written::

        staging_dir/
        ├── autounattend.xml
        └── $OEM$/
            └── $1/
                └── autopilot/
                    ├── firstboot.ps1
                    ├── qemu-ga-aarch64.msi               (optional)
                    └── utm-guest-tools-0.1.271.exe       (optional)

    Note: Windows Setup's built-in ``$OEM$\\$1\\`` auto-copy only fires
    when autounattend.xml is on the installer media. Since our
    autounattend is on a separate CD, the FirstLogonCommand Order 1
    in ``unattend.xml.j2`` scans drives for autounattend.xml and
    ``Copy-Item``s the $OEM$ tree into ``C:\\`` before
    ``firstboot.ps1`` runs.

    Profile overrides (set to empty string or drop the key to skip):
      - ``qemu_ga_msi_path``            → QEMU GA MSI
      - ``utm_guest_tools_exe_path``    → UTM guest tools NSIS installer
    """
    (staging_dir / "autounattend.xml").write_text(
        render_arm64_unattend(profile), encoding="utf-8"
    )
    oem_dir = staging_dir / "$OEM$" / "$1" / "autopilot"
    oem_dir.mkdir(parents=True, exist_ok=True)
    (oem_dir / "firstboot.ps1").write_text(
        render_firstboot_ps1(profile), encoding="utf-8"
    )

    _stage_optional_asset(
        oem_dir, "qemu_ga_msi_path", _QEMU_GA_MSI_DEFAULT,
        profile, "qemu-ga-aarch64.msi",
    )
    _stage_optional_asset(
        oem_dir, "utm_guest_tools_exe_path", _UTM_GUEST_TOOLS_DEFAULT,
        profile, "utm-guest-tools.exe",
    )


def build_answer_iso(vm_name: str, profile: dict, output_dir: Path) -> Path:
    """Build a hybrid ISO labeled ``AUTOUNATTEND`` using ``hdiutil makehybrid``.

    The generated ISO contains:
    * ``autounattend.xml`` — picked up automatically by Windows Setup.
    * ``$OEM$\\$1\\autopilot\\firstboot.ps1`` — copied to ``C:\\autopilot\\``
      during Windows text-mode setup.

    Returns the path to the generated ``.iso`` file.
    Raises ``RuntimeError`` on hdiutil failure.
    Raises ``FileNotFoundError`` if hdiutil is not on PATH (non-macOS host).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_iso = output_dir / f"{vm_name}-autounattend.iso"

    # Stage files alongside the output ISO (not in /tmp).
    staging_dir = output_dir / f".staging-{vm_name}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    try:
        stage_answer_iso_files(staging_dir, profile)

        if out_iso.exists():
            out_iso.unlink()

        result = subprocess.run(
            [
                "hdiutil", "makehybrid",
                "-iso",
                "-joliet",
                "-default-volume-name", "AUTOUNATTEND",
                "-o", str(out_iso),
                str(staging_dir),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"hdiutil makehybrid failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    return out_iso


def _cli_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Build a Windows 11 ARM64 answer ISO (AUTOUNATTEND label)",
    )
    parser.add_argument(
        "--vm-name", required=True,
        help="VM name — used as the ISO filename stem",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory to write the generated ISO into",
    )
    parser.add_argument(
        "--profile-json", required=True,
        help=(
            "JSON object with profile keys: hostname, locale, timezone, "
            "admin_user, admin_pass, org_name, product_key, windows_edition, "
            "domain_join, firstboot_cmds"
        ),
    )
    parser.add_argument(
        "--preview-only", action="store_true",
        help="Print rendered unattend.xml to stdout; do not build the ISO",
    )
    args = parser.parse_args(argv)

    try:
        profile = json.loads(args.profile_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid profile JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.preview_only:
        print(render_arm64_unattend(profile))
        return

    output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
    iso_path = build_answer_iso(args.vm_name, profile, output_dir)
    # Print ISO path so the calling Ansible task can register it.
    print(str(iso_path))


if __name__ == "__main__":
    _cli_main()
