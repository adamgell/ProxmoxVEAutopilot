"""Tests for the post_winpe unattend template (no windowsPE pass)."""
from pathlib import Path

import pytest


_TEMPLATE = Path(__file__).resolve().parent.parent / "files" / "autounattend.post_winpe.xml.j2"


def test_template_file_exists():
    assert _TEMPLATE.is_file(), f"missing: {_TEMPLATE}"


def test_template_has_no_windowsPE_pass():
    text = _TEMPLATE.read_text()
    assert 'pass="windowsPE"' not in text, (
        "post_winpe template must not contain the windowsPE settings block"
    )


def test_template_has_specialize_pass():
    text = _TEMPLATE.read_text()
    assert 'pass="specialize"' in text


def test_template_has_oobeSystem_pass():
    text = _TEMPLATE.read_text()
    assert 'pass="oobeSystem"' in text


def test_template_has_no_disk_config_or_image_install():
    """Setup's windowsPE pass owns DiskConfiguration / ImageInstall.
    The post_winpe path bypasses Setup, so neither block must remain.
    Drivers come from phase-0 dism /add-driver against the VirtIO ISO,
    not from a PnpCustomizations pass in the unattend."""
    text = _TEMPLATE.read_text()
    assert "<DiskConfiguration>" not in text
    assert "<ImageInstall>" not in text
    assert "Microsoft-Windows-PnpCustomizationsWinPE" not in text


def test_template_jinja_blocks_match_full_template():
    """post_winpe must accept the same {{ ... }} block names so renderer
    code path is unified."""
    full = (_TEMPLATE.parent / "autounattend.xml.j2").read_text()
    pw = _TEMPLATE.read_text()
    for var in ("oobe_user_accounts", "oobe_auto_logon",
                "specialize_computer_name",
                "specialize_identification_component",
                "extra_first_logon_commands"):
        assert f"{{{{ {var} }}}}" in pw or f"{{{{{var}}}}}" in pw, (
            f"post_winpe template missing placeholder for {var}"
        )
        # same placeholder must exist in the full template (sanity check)
        assert f"{{{{ {var} }}}}" in full or f"{{{{{var}}}}}" in full
