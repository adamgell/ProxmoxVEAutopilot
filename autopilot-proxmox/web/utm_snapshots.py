"""UTM snapshot operations via ``qemu-img snapshot``.

Only STOPPED VMs may have snapshots created, restored, or deleted.
Listing uses ``-U`` (force-share) so it works on any VM state.

If ``qemu-img`` is not found on PATH or at the configured
``utm_qemu_img_path``, every function raises a ``RuntimeError`` that
points the user at ``brew install qemu``.

Error contract (mirrors utm_cli conventions):
  - All public functions raise RuntimeError on failure.
  - Callers surface RuntimeError as HTTP 500 (or 409 for state errors).
"""
from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
from pathlib import Path

from web import utm_cli

_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DEFAULT_QEMU_IMG = "/opt/homebrew/bin/qemu-img"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _qemu_img_path() -> str:
    """Return the resolved qemu-img binary path.

    Search order:
    1. ``utm_qemu_img_path`` setting (vars.yml)
    2. ``/opt/homebrew/bin/qemu-img`` (Homebrew default on Apple Silicon)
    3. ``qemu-img`` on ``$PATH``

    Raises RuntimeError if none found.
    """
    vars_ = utm_cli._load_vars()
    configured = (vars_.get("utm_qemu_img_path") or "").strip()
    if configured:
        p = Path(configured)
        if p.exists() and p.is_file():
            return str(p)

    default = Path(_DEFAULT_QEMU_IMG)
    if default.exists():
        return str(default)

    found = shutil.which("qemu-img")
    if found:
        return found

    raise RuntimeError(
        "qemu-img not found. Install via `brew install qemu` or set "
        "utm_qemu_img_path in Settings → UTM (macOS/ARM64) Configuration."
    )


def _disk_images_for_bundle(bundle: Path) -> list[Path]:
    """Return writable disk qcow2 paths inside a .utm bundle.

    Parses ``config.plist`` for ``Drive`` entries where
    ``ImageType == "Disk"`` and ``ReadOnly == false``.
    Falls back to globbing ``Data/*.qcow2`` if plist parsing fails.
    """
    config_path = bundle / "config.plist"
    images: list[Path] = []

    try:
        with open(config_path, "rb") as f:
            config = plistlib.load(f)

        for drive in config.get("Drive", []):
            if (
                drive.get("ImageType") == "Disk"
                and not drive.get("ReadOnly", False)
                and "ImageName" in drive
            ):
                img_path = bundle / "Data" / drive["ImageName"]
                if img_path.exists():
                    images.append(img_path)
    except Exception:
        # Fallback: enumerate all qcow2 files in Data/
        data_dir = bundle / "Data"
        if data_dir.is_dir():
            images = sorted(data_dir.glob("*.qcow2"))

    return images


def _validate_snapshot_name(name: str) -> None:
    """Raise ValueError if name does not match the allowed pattern."""
    if not _SNAPSHOT_NAME_RE.match(name):
        raise ValueError(
            f"Snapshot name must be 1–64 alphanumeric/._- characters, got {name!r}"
        )


def _run_qemu_img(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    """Run ``qemu-img`` with explicit arg list.  Never uses shell=True.

    Raises RuntimeError on FileNotFoundError or TimeoutExpired.
    Returns (returncode, stdout, stderr) otherwise.
    """
    binary = _qemu_img_path()
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        raise RuntimeError(
            f"qemu-img not found at {binary}. Install via `brew install qemu`."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"qemu-img {' '.join(args[:3])} timed out after {timeout}s"
        )


def _require_stopped(vm_uuid: str) -> None:
    """Raise RuntimeError if the VM is not in 'stopped' state.

    Note: 'suspended'/'paused' are NOT sufficient — QEMU still holds a
    write lock on the disk file in those states.
    """
    try:
        vms = utm_cli.list_vms()
    except RuntimeError as exc:
        raise RuntimeError(f"Cannot check VM state: {exc}") from exc

    vm = next((m for m in vms if m["uuid"] == vm_uuid or m["name"] == vm_uuid), None)
    if vm is None:
        raise RuntimeError(f"VM not found: {vm_uuid!r}")

    if vm["status"] != "stopped":
        raise RuntimeError(
            f"VM must be stopped before this snapshot operation "
            f"(current status: {vm['status']!r}). "
            f"Stop it via the UI or `utmctl stop {vm_uuid}` first."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_snapshots(vm_uuid: str) -> list[dict]:
    """List internal qcow2 snapshots for all writable disks in the VM.

    Uses ``-U`` (force-share) so it works on any VM state, including started.

    Returns a list of dicts::

        {
          "disk":        "9FCD54A1-….qcow2",
          "snapshot_id": "1",
          "tag":         "before-update",
          "vm_size":     "221 MiB",
          "date":        "2025-07-01 12:00:00",
        }

    Raises RuntimeError on bundle resolution failure or qemu-img error.
    """
    bundle = utm_cli.bundle_path_for(vm_uuid)
    disks = _disk_images_for_bundle(bundle)
    if not disks:
        return []

    snapshots: list[dict] = []
    for disk in disks:
        rc, stdout, stderr = _run_qemu_img(
            ["snapshot", "-l", "-U", str(disk)], timeout=30
        )
        if rc != 0:
            raise RuntimeError(
                f"qemu-img snapshot -l failed for {disk.name}: {stderr.strip()}"
            )

        for line in stdout.splitlines():
            # Output format (after header lines):
            # ID   TAG           VM SIZE    DATE                VM CLOCK  ICOUNT
            # 1    before-upd    221 MiB    2025-07-01 12:00:00  00:00:00   0
            stripped = line.strip()
            if not stripped or stripped.startswith("Snapshot") or stripped.startswith("ID"):
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                snapshots.append({
                    "disk": disk.name,
                    "snapshot_id": parts[0],
                    "tag": parts[1],
                    "vm_size": " ".join(parts[2:4]) if len(parts) >= 4 else "",
                    "date": " ".join(parts[4:6]) if len(parts) >= 6 else "",
                })

    return snapshots


def create_snapshot(vm_uuid: str, name: str, description: str = "") -> dict:
    """Create a named snapshot on all writable disks.

    VM must be **stopped** (not started, suspended, or paused).
    Raises RuntimeError otherwise or on qemu-img failure.

    ``description`` is accepted for API symmetry but is not stored by
    qemu-img.  Future versions may persist it in a sidecar file.
    """
    _validate_snapshot_name(name)
    _require_stopped(vm_uuid)

    bundle = utm_cli.bundle_path_for(vm_uuid)
    disks = _disk_images_for_bundle(bundle)
    if not disks:
        raise RuntimeError("No writable disk images found in bundle")

    created: list[str] = []
    for disk in disks:
        rc, _out, stderr = _run_qemu_img(
            ["snapshot", "-c", name, str(disk)], timeout=120
        )
        if rc != 0:
            raise RuntimeError(
                f"qemu-img snapshot -c {name!r} failed for {disk.name}: {stderr.strip()}"
            )
        created.append(disk.name)

    return {"ok": True, "name": name, "disks": created}


def restore_snapshot(vm_uuid: str, name: str) -> dict:
    """Apply (restore) a named snapshot on all writable disks.

    VM must be **stopped**. Raises RuntimeError otherwise.
    """
    _validate_snapshot_name(name)
    _require_stopped(vm_uuid)

    bundle = utm_cli.bundle_path_for(vm_uuid)
    disks = _disk_images_for_bundle(bundle)
    if not disks:
        raise RuntimeError("No writable disk images found in bundle")

    restored: list[str] = []
    for disk in disks:
        rc, _out, stderr = _run_qemu_img(
            ["snapshot", "-a", name, str(disk)], timeout=120
        )
        if rc != 0:
            raise RuntimeError(
                f"qemu-img snapshot -a {name!r} failed for {disk.name}: {stderr.strip()}"
            )
        restored.append(disk.name)

    return {"ok": True, "name": name, "disks": restored}


def delete_snapshot(vm_uuid: str, name: str) -> dict:
    """Delete a named snapshot from all writable disks.

    VM must be **stopped**. Raises RuntimeError otherwise.
    """
    _validate_snapshot_name(name)
    _require_stopped(vm_uuid)

    bundle = utm_cli.bundle_path_for(vm_uuid)
    disks = _disk_images_for_bundle(bundle)
    if not disks:
        raise RuntimeError("No writable disk images found in bundle")

    deleted: list[str] = []
    for disk in disks:
        rc, _out, stderr = _run_qemu_img(
            ["snapshot", "-d", name, str(disk)], timeout=120
        )
        if rc != 0:
            raise RuntimeError(
                f"qemu-img snapshot -d {name!r} failed for {disk.name}: {stderr.strip()}"
            )
        deleted.append(disk.name)

    return {"ok": True, "name": name, "disks": deleted}
