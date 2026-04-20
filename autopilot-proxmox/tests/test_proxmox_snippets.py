"""Tests for web.proxmox_snippets — presence verification for chassis binaries."""
from unittest.mock import patch

import pytest


def test_require_returns_path_when_volid_listed():
    from web import proxmox_snippets

    def fake_api(path, method="GET", data=None, files=None):
        assert method == "GET" and path.endswith("/content")
        return [
            {"volid": "local:snippets/autopilot-chassis-type-10.bin",
             "content": "snippets"},
        ]

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        host_path = proxmox_snippets.require_chassis_type_binary(
            node="pve", storage="local", chassis_type=10,
        )
    assert host_path == "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"


def test_require_raises_when_missing_with_seed_hint():
    from web import proxmox_snippets

    def fake_api(path, method="GET", data=None, files=None):
        return []

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        with pytest.raises(proxmox_snippets.ChassisBinaryMissing) as excinfo:
            proxmox_snippets.require_chassis_type_binary(
                node="pve2", storage="local", chassis_type=31,
            )
    msg = str(excinfo.value)
    assert "autopilot-chassis-type-31.bin" in msg
    assert "seed_chassis_binaries.py" in msg
    assert "pvesm set local" in msg


def test_require_never_calls_upload():
    """Regression: previous implementation tried the /upload API, which
    Proxmox rejects with 400 for content=snippets. We must only GET."""
    from web import proxmox_snippets

    def fake_api(path, method="GET", data=None, files=None):
        assert method == "GET", f"unexpected {method} {path}"
        assert "upload" not in path
        return [
            {"volid": "local:snippets/autopilot-chassis-type-3.bin"},
        ]

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        proxmox_snippets.require_chassis_type_binary(
            node="pve", storage="local", chassis_type=3,
        )


def test_require_validates_chassis_type():
    from web import proxmox_snippets
    with pytest.raises(ValueError):
        proxmox_snippets.require_chassis_type_binary(
            node="pve", storage="local", chassis_type=0,
        )


def test_filename_uses_chassis_type_integer():
    from web import proxmox_snippets
    assert proxmox_snippets._binary_filename(10) == \
        "autopilot-chassis-type-10.bin"
    assert proxmox_snippets._binary_filename(31) == \
        "autopilot-chassis-type-31.bin"


def test_host_path_uses_local_default_root():
    from web import proxmox_snippets
    assert proxmox_snippets._host_path_for("local", 10) == \
        "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"


def test_seed_script_build_matches_smbios_builder():
    """The stdlib-only seed script must produce byte-identical output
    to the in-app smbios_builder, so files seeded offline are exactly
    what the app would have generated."""
    import importlib.util
    from pathlib import Path
    from web import smbios_builder

    script = Path(__file__).resolve().parent.parent / "scripts" / "seed_chassis_binaries.py"
    spec = importlib.util.spec_from_file_location("seed_chassis_binaries", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for ct in (3, 10, 31, 35):
        assert mod.build_type3_chassis(ct) == smbios_builder.build_type3_chassis(ct)
