"""Sidecar JSON validation for build artifacts.

A sidecar is the metadata file written next to every WIM by Build-*.ps1.
This module is the single source of truth for the sidecar schema on the
Python side; PowerShell side is `Write-ArtifactSidecar`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ArtifactKind(str, Enum):
    INSTALL_WIM = "install-wim"
    PE_WIM = "pe-wim"
    STAGE_ZIP = "stage-zip"      # for Plan 2's per-VM rendered blobs
    UNATTEND_XML = "unattend-xml" # for Plan 2's per-VM rendered blobs
    DRIVER_ZIP = "driver-zip"     # for Plan 3's per-VM driver-override step


class SidecarValidationError(ValueError):
    pass


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Sidecar:
    kind: ArtifactKind
    sha256: str
    size: int
    metadata: dict


def load_sidecar(path: Path) -> Sidecar:
    """Parse and validate a sidecar JSON file. Raises SidecarValidationError on any issue."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SidecarValidationError("sidecar root must be an object")

    if "kind" not in raw:
        raise SidecarValidationError("sidecar missing required field: kind")
    try:
        kind = ArtifactKind(raw["kind"])
    except ValueError:
        valid = ", ".join(k.value for k in ArtifactKind)
        raise SidecarValidationError(f"unknown kind '{raw['kind']}'; valid: {valid}")

    if "sha256" not in raw:
        raise SidecarValidationError("sidecar missing required field: sha256")
    sha = raw["sha256"]
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        if isinstance(sha, str) and sha.lower() == sha and len(sha) != 64:
            raise SidecarValidationError(f"sha256 must be 64 lowercase hex chars; got {len(sha)}")
        if isinstance(sha, str) and sha.lower() != sha:
            raise SidecarValidationError("sha256 must be lowercase hex")
        raise SidecarValidationError("sha256 must be 64 lowercase hex chars")

    if "size" not in raw:
        raise SidecarValidationError("sidecar missing required field: size")
    size = raw["size"]
    if not isinstance(size, int) or size < 0:
        raise SidecarValidationError(f"size must be a non-negative int; got {size!r}")

    metadata = {k: v for k, v in raw.items() if k not in ("kind", "sha256", "size")}
    return Sidecar(kind=kind, sha256=sha, size=size, metadata=metadata)
