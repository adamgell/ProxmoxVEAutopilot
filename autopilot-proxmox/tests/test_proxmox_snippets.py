"""Tests for web.proxmox_snippets — presence verification for chassis binaries."""
from unittest.mock import patch

import pytest


def _api_router(responses):
    """Return a _proxmox_api side_effect that routes by path prefix.

    ``responses`` maps path-prefix → value. Unknown paths raise.
    """
    def side_effect(path, method="GET", data=None, files=None):
        assert method == "GET", f"only GET expected, got {method} {path}"
        for prefix, value in responses.items():
            if path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected API call: {path}")
    return side_effect


def test_require_returns_path_when_volid_listed():
    from web import proxmox_snippets

    with patch("web.proxmox_snippets._proxmox_api", side_effect=_api_router({
        "/nodes/pve/storage/local/content": [
            {"volid": "local:snippets/autopilot-chassis-type-10.bin",
             "content": "snippets"},
        ],
    })):
        host_path = proxmox_snippets.require_chassis_type_binary(
            node="pve", storage="local", chassis_type=10,
        )
    assert host_path == "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"


def test_require_diagnoses_snippets_content_not_enabled():
    from web import proxmox_snippets

    with patch("web.proxmox_snippets._proxmox_api", side_effect=_api_router({
        "/nodes/pve/storage/local/content": [],
        "/storage/local": {"content": "backup,iso,vztmpl"},
    })):
        with pytest.raises(proxmox_snippets.ChassisBinaryMissing) as excinfo:
            proxmox_snippets.require_chassis_type_binary(
                node="pve", storage="local", chassis_type=10,
            )
    msg = str(excinfo.value)
    assert "does not allow the 'snippets' content type" in msg
    assert "pvesm set local --content" in msg
    assert "snippets" in msg
    # The fix command should include the existing types plus snippets.
    assert "backup" in msg and "iso" in msg and "vztmpl" in msg


def test_require_diagnoses_missing_datastore_allocate():
    from web import proxmox_snippets

    with patch("web.proxmox_snippets._proxmox_api", side_effect=_api_router({
        "/nodes/pve2/storage/local/content": [],
        "/storage/local": {"content": "backup,iso,import,vztmpl,snippets"},
        "/access/permissions": {"/storage/local": {"Datastore.Audit": 1,
                                                   "Datastore.AllocateSpace": 1}},
    })):
        with pytest.raises(proxmox_snippets.ChassisBinaryMissing) as excinfo:
            proxmox_snippets.require_chassis_type_binary(
                node="pve2", storage="local", chassis_type=31,
            )
    msg = str(excinfo.value)
    assert "Datastore.Allocate" in msg
    assert "pveum role modify" in msg
    assert "PVEDatastoreAdmin" in msg


def test_require_diagnoses_file_not_seeded():
    from web import proxmox_snippets

    with patch("web.proxmox_snippets._proxmox_api", side_effect=_api_router({
        "/nodes/pve/storage/local/content": [
            # A snippet exists (so listing works / perms are fine) but
            # it's not the one we need.
            {"volid": "local:snippets/somebody-else.bin", "content": "snippets"},
        ],
        "/storage/local": {"content": "backup,iso,import,vztmpl,snippets"},
        "/access/permissions": {"/storage/local": {"Datastore.Allocate": 1}},
    })):
        with pytest.raises(proxmox_snippets.ChassisBinaryMissing) as excinfo:
            proxmox_snippets.require_chassis_type_binary(
                node="pve", storage="local", chassis_type=35,
            )
    msg = str(excinfo.value)
    assert "autopilot-chassis-type-35.bin" in msg
    assert "seed_chassis_binaries.py" in msg
    assert "is not present" in msg


def test_require_never_calls_upload():
    """Regression: the original implementation tried /upload, which
    Proxmox rejects for content=snippets. We must never POST."""
    from web import proxmox_snippets

    def fake_api(path, method="GET", data=None, files=None):
        assert method == "GET", f"unexpected {method} {path}"
        assert "upload" not in path
        return [{"volid": "local:snippets/autopilot-chassis-type-3.bin"}]

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
