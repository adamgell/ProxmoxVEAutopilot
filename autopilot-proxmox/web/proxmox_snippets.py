"""Upload per-chassis SMBIOS binaries to a Proxmox node's snippet storage.

QEMU's ``-smbios file=`` option requires the binary to live on the
Proxmox host's local filesystem (not inside the autopilot container).
Proxmox exposes a filesystem-backed 'snippets' content type on the
``local`` storage, typically rooted at ``/var/lib/vz/snippets/``.

Upload is idempotent: before POSTing, we list the storage's ``content``
endpoint for a volid matching the expected filename and skip the upload
if it's already present.
"""
from __future__ import annotations

from web.smbios_builder import build_type3_chassis
from web.app import _proxmox_api

_LOCAL_STORAGE_ROOT = "/var/lib/vz"


def _binary_filename(chassis_type: int) -> str:
    return f"autopilot-chassis-type-{int(chassis_type)}.bin"


def _host_path_for(storage: str, chassis_type: int) -> str:
    if storage != "local":
        raise ValueError(
            f"only the 'local' Proxmox storage is supported for chassis "
            f"binaries (got {storage!r}); map the file manually or extend "
            f"_host_path_for()."
        )
    return f"{_LOCAL_STORAGE_ROOT}/snippets/{_binary_filename(chassis_type)}"


def ensure_chassis_type_binary(*, node: str, storage: str,
                               chassis_type: int) -> str:
    """Guarantee the chassis-type binary is present on the given storage.

    If the content listing shows the volid already, no upload happens.
    Otherwise the binary is generated in-memory and POSTed via the
    Proxmox storage upload endpoint.

    Returns the on-host path to pass to QEMU ``-smbios file=``.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(f"chassis_type must be 1..255, got {chassis_type!r}")

    filename = _binary_filename(chassis_type)
    target_path = _host_path_for(storage, chassis_type)

    try:
        content = _proxmox_api(f"/nodes/{node}/storage/{storage}/content")
    except Exception:
        content = []
    expected_volid = f"{storage}:snippets/{filename}"
    if any(entry.get("volid") == expected_volid for entry in content or []):
        return target_path

    blob = build_type3_chassis(chassis_type=chassis_type)
    _proxmox_api(
        f"/nodes/{node}/storage/{storage}/upload",
        method="POST",
        data={"content": "snippets", "filename": filename},
        files={"filename": (filename, blob, "application/octet-stream")},
    )
    return target_path
