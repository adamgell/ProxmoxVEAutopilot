"""Tests for web.proxmox_snippets — idempotent chassis-binary upload."""
from unittest.mock import MagicMock, patch


def test_ensure_uploads_when_absent():
    """If the Proxmox storage doesn't already list the expected filename,
    ensure_chassis_type_binary must POST to the upload endpoint and
    return the on-host path."""
    from web import proxmox_snippets

    listing_calls = []
    upload_calls = []

    def fake_api(path, method="GET", data=None, files=None):
        if method == "GET" and path.endswith("/content"):
            listing_calls.append(path)
            return []
        if method == "POST" and path.endswith("/upload"):
            upload_calls.append({"path": path, "data": data, "files": files})
            return "UPID:fake:0000"
        raise AssertionError(f"unexpected call: {method} {path}")

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        host_path = proxmox_snippets.ensure_chassis_type_binary(
            node="pve", storage="local", chassis_type=10,
        )

    assert host_path == "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"
    assert len(listing_calls) == 1
    assert len(upload_calls) == 1
    assert upload_calls[0]["data"]["content"] == "snippets"
    from web import smbios_builder
    assert upload_calls[0]["files"]["filename"][1] == \
        smbios_builder.build_type3_chassis(chassis_type=10)


def test_ensure_skips_when_already_present():
    from web import proxmox_snippets

    def fake_api(path, method="GET", data=None, files=None):
        if method == "GET" and path.endswith("/content"):
            return [
                {"volid": "local:snippets/autopilot-chassis-type-10.bin",
                 "content": "snippets"},
            ]
        raise AssertionError(f"upload should not be called; got {method} {path}")

    with patch("web.proxmox_snippets._proxmox_api", side_effect=fake_api):
        host_path = proxmox_snippets.ensure_chassis_type_binary(
            node="pve", storage="local", chassis_type=10,
        )
    assert host_path == "/var/lib/vz/snippets/autopilot-chassis-type-10.bin"


def test_ensure_validates_chassis_type():
    from web import proxmox_snippets
    import pytest
    with pytest.raises(ValueError):
        proxmox_snippets.ensure_chassis_type_binary(
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
