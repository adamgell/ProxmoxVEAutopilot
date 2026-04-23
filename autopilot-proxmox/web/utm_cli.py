"""UTM subprocess helper — shared by all /api/utm/vms/* endpoints.

Every public function documents its error contract:
  - run_utmctl: never raises — errors are returned as (rc, stdout, stderr)
  - list_vms: raises RuntimeError on failure (callers surface it as HTTP 500)
  - get_vm_ip: returns [] on any failure (IP is best-effort)
  - bundle_path_for: raises RuntimeError on failure
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_BASE_DIR = Path(__file__).resolve().parent.parent
_VARS_PATH = _BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"

_DEFAULT_UTMCTL = "/Applications/UTM.app/Contents/MacOS/utmctl"
_DEFAULT_LIBRARY = "~/Library/Containers/com.utmapp.UTM/Data/Documents"


def _load_vars() -> dict:
    if _VARS_PATH.exists():
        with open(_VARS_PATH) as f:
            data = yaml.safe_load(f)
        return data or {}
    return {}


def utmctl_path() -> str:
    return _load_vars().get("utm_utmctl_path") or _DEFAULT_UTMCTL


def utm_library_path() -> Path:
    """Return the resolved library directory (expanduser, no traversal)."""
    raw = _load_vars().get("utm_library_path") or _DEFAULT_LIBRARY
    # utm_documents_dir may contain Ansible template syntax — ignore it
    # if it contains '{{'; fall back to the static default.
    if "{{" in str(raw):
        raw = _DEFAULT_LIBRARY
    return Path(os.path.expanduser(raw)).resolve()


def run_utmctl(args: list[str], *, timeout: int = 10) -> tuple[int, str, str]:
    """Run utmctl with the given extra args.  Never raises.

    Returns (returncode, stdout, stderr).
    rc=-1 → FileNotFoundError (binary missing)
    rc=-2 → TimeoutExpired
    """
    binary = utmctl_path()
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", f"utmctl not found at {binary}"
    except subprocess.TimeoutExpired:
        return -2, "", f"utmctl {' '.join(args)} timed out after {timeout}s"
    except Exception as exc:
        return -3, "", str(exc)


def list_vms() -> list[dict]:
    """Return list of {uuid, status, name} dicts.  Raises RuntimeError on failure."""
    rc, stdout, stderr = run_utmctl(["list"], timeout=10)
    if rc != 0:
        msg = stderr.strip() or f"utmctl list exited {rc}"
        raise RuntimeError(msg)
    vms = []
    for line in stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) >= 3:
            vms.append({"uuid": parts[0], "status": parts[1], "name": parts[2]})
        elif len(parts) == 2:
            vms.append({"uuid": parts[0], "status": parts[1], "name": ""})
    return vms


def get_vm_ip(name_or_uuid: str) -> list[str]:
    """Return list of IP strings for the VM.  Returns [] on any failure."""
    rc, stdout, _stderr = run_utmctl(["ip-address", name_or_uuid], timeout=10)
    if rc != 0:
        return []
    ips = [line.strip() for line in stdout.splitlines() if line.strip()]
    return ips


def bundle_path_for(name_or_uuid: str) -> Path:
    """Look up the on-disk .utm bundle path for *name_or_uuid*.

    Security guarantee: only returns paths that are direct children of
    utm_library_path and end in .utm — never follows symlinks out.

    Raises RuntimeError if the VM cannot be found, the bundle is missing,
    or the path fails the safety check.
    """
    try:
        vms = list_vms()
    except RuntimeError as exc:
        raise RuntimeError(f"Cannot list VMs to resolve bundle: {exc}") from exc

    vm_name = None
    for vm in vms:
        if vm["uuid"] == name_or_uuid or vm["name"] == name_or_uuid:
            vm_name = vm["name"]
            break

    if vm_name is None:
        raise RuntimeError(f"VM not found: {name_or_uuid!r}")

    library = utm_library_path()
    candidate = (library / f"{vm_name}.utm").resolve()

    if candidate.parent != library:
        raise RuntimeError(
            f"Resolved bundle path {candidate} is not a direct child of "
            f"{library} — refusing to operate on it"
        )
    if not candidate.suffix == ".utm":
        raise RuntimeError(f"Bundle path does not end in .utm: {candidate}")
    if not candidate.exists():
        raise RuntimeError(f"Bundle not found on disk: {candidate}")

    return candidate
