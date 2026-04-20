"""Verify per-chassis SMBIOS binaries are present on a Proxmox node.

QEMU's ``-smbios file=`` option requires the binary to live on the
Proxmox host's local filesystem (not inside the autopilot container).
Proxmox exposes a filesystem-backed 'snippets' content type on the
``local`` storage, typically rooted at ``/var/lib/vz/snippets/``.

Proxmox's storage upload API only accepts ``iso``, ``vztmpl``, and
``import`` content types — ``snippets`` is rejected server-side. We
therefore require an operator to pre-seed these binaries on each
Proxmox node (see ``scripts/seed_chassis_binaries.py``) and only
verify their presence here.
"""
from __future__ import annotations

from web.app import _proxmox_api

_LOCAL_STORAGE_ROOT = "/var/lib/vz"


class ChassisBinaryMissing(RuntimeError):
    """Raised when an expected chassis-type SMBIOS binary isn't on the node."""


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


def _seed_hint(node: str, storage: str, chassis_type: int) -> str:
    filename = _binary_filename(chassis_type)
    return (
        f"{filename} is not present on Proxmox node {node!r} "
        f"(storage {storage!r}, content type 'snippets'). Seed it by "
        f"running scripts/seed_chassis_binaries.py on the Proxmox host, "
        f"e.g.:\n"
        f"    ssh root@<node> 'python3 - {chassis_type}' "
        f"< autopilot-proxmox/scripts/seed_chassis_binaries.py\n"
        f"and ensure 'snippets' is enabled on the storage:\n"
        f"    pvesm set {storage} --content backup,iso,import,vztmpl,snippets"
    )


def require_chassis_type_binary(*, node: str, storage: str,
                                chassis_type: int) -> str:
    """Verify the chassis-type binary is present on the given storage.

    Returns the on-host path to pass to QEMU ``-smbios file=``.
    Raises :class:`ChassisBinaryMissing` with a remediation hint if the
    storage doesn't list the expected volid. Raises :class:`ValueError`
    for invalid inputs.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(f"chassis_type must be 1..255, got {chassis_type!r}")

    filename = _binary_filename(chassis_type)
    target_path = _host_path_for(storage, chassis_type)
    expected_volid = f"{storage}:snippets/{filename}"

    content = _proxmox_api(f"/nodes/{node}/storage/{storage}/content") or []
    if any(entry.get("volid") == expected_volid for entry in content):
        return target_path

    raise ChassisBinaryMissing(_seed_hint(node, storage, chassis_type))
