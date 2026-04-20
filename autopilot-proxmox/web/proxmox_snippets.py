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

Presence is surprisingly tricky to verify. Proxmox's content listing
silently filters out ``snippets`` entries unless the caller has the
``Datastore.Allocate`` privilege on the storage (see
``PVE::Storage::check_volume_access`` in PVE/Storage.pm — snippets can
run as hookscripts, so they're held to a higher bar than ISOs). An
empty listing therefore has three possible causes:

1. ``snippets`` is not enabled as a content type on the storage.
2. The API token is missing ``Datastore.Allocate`` on the storage, so
   snippets are filtered out of the result.
3. The file genuinely isn't on disk.

``require_chassis_type_binary`` diagnoses each case and raises a
:class:`ChassisBinaryMissing` with a message that tells the operator
exactly what to fix.
"""
from __future__ import annotations

from web.app import _proxmox_api

_LOCAL_STORAGE_ROOT = "/var/lib/vz"


class ChassisBinaryMissing(RuntimeError):
    """Raised when an expected chassis-type SMBIOS binary isn't usable."""


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


def _diagnose_storage_content(storage: str) -> set[str]:
    """Return the set of content types allowed on the storage, or empty
    set on error (treated as 'unknown'). Cluster-scoped endpoint, so
    one call covers every node."""
    try:
        cfg = _proxmox_api(f"/storage/{storage}") or {}
    except Exception:
        return set()
    raw = cfg.get("content") if isinstance(cfg, dict) else None
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _diagnose_token_has_datastore_allocate(storage: str) -> bool:
    """True if the calling token has Datastore.Allocate on the storage.
    False on error (treated as 'cannot confirm')."""
    try:
        perms = _proxmox_api(f"/access/permissions?path=/storage/{storage}") or {}
    except Exception:
        return False
    if not isinstance(perms, dict):
        return False
    effective = perms.get(f"/storage/{storage}") or {}
    return bool(effective.get("Datastore.Allocate"))


def _missing_snippets_content(storage: str, allowed: set[str]) -> str:
    new_list = ",".join(sorted(allowed | {"snippets"})) or "snippets"
    allowed_desc = ",".join(sorted(allowed)) if allowed else "unknown"
    return (
        f"Proxmox storage {storage!r} does not allow the 'snippets' "
        f"content type (allowed: {allowed_desc}). Enable it on the "
        f"Proxmox host and retry:\n"
        f"    pvesm set {storage} --content {new_list}"
    )


def _missing_datastore_allocate(storage: str) -> str:
    return (
        f"The API token cannot list snippets on storage {storage!r}: it "
        f"is missing the 'Datastore.Allocate' privilege, which Proxmox "
        f"requires for snippet volumes (see PVE::Storage::"
        f"check_volume_access). Add it to your role on the Proxmox host:"
        f"\n    pveum role modify <role> -privs "
        f"+Datastore.Allocate\n"
        f"or grant the built-in PVEDatastoreAdmin role scoped to the "
        f"storage:\n    pveum acl modify /storage/{storage} "
        f"-user <user> -role PVEDatastoreAdmin"
    )


def _file_not_seeded(node: str, storage: str, chassis_type: int) -> str:
    filename = _binary_filename(chassis_type)
    return (
        f"{filename} is not present on Proxmox node {node!r} "
        f"(storage {storage!r}, content 'snippets'). Seed it on the "
        f"node:\n"
        f"    scp autopilot-proxmox/scripts/seed_chassis_binaries.py "
        f"root@<node>:/tmp/\n"
        f"    ssh root@<node> 'python3 /tmp/seed_chassis_binaries.py "
        f"{chassis_type}'"
    )


def require_chassis_type_binary(*, node: str, storage: str,
                                chassis_type: int) -> str:
    """Verify the chassis-type binary is usable for QEMU on the node.

    Returns the on-host path to pass to ``-smbios file=``. Raises
    :class:`ChassisBinaryMissing` with a diagnostic remediation hint
    when the binary can't be listed — either because the storage
    config doesn't allow snippets, the token lacks the required
    privilege, or the file genuinely isn't seeded.
    """
    if not isinstance(chassis_type, int) or chassis_type < 1 or chassis_type > 255:
        raise ValueError(f"chassis_type must be 1..255, got {chassis_type!r}")

    filename = _binary_filename(chassis_type)
    target_path = _host_path_for(storage, chassis_type)
    expected_volid = f"{storage}:snippets/{filename}"

    content = _proxmox_api(
        f"/nodes/{node}/storage/{storage}/content?content=snippets"
    ) or []
    if any(entry.get("volid") == expected_volid for entry in content):
        return target_path

    # Empty / missing — diagnose *why* before blaming the operator for
    # forgetting to seed the file.
    allowed = _diagnose_storage_content(storage)
    if allowed and "snippets" not in allowed:
        raise ChassisBinaryMissing(_missing_snippets_content(storage, allowed))

    if not _diagnose_token_has_datastore_allocate(storage):
        raise ChassisBinaryMissing(_missing_datastore_allocate(storage))

    raise ChassisBinaryMissing(_file_not_seeded(node, storage, chassis_type))
