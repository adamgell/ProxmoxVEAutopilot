"""CLI: register a built artifact (WIM/ISO/zip) into the artifact store.

Usage from autopilot-proxmox/:
    python -m web.artifact_register \\
        --path  ../var/artifacts/staging/winpe-autopilot-arm64-<sha>.wim \\
        --sidecar ../var/artifacts/staging/winpe-autopilot-arm64-<sha>.json \\
        --extension wim
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from web.artifact_sidecar import SidecarValidationError, load_sidecar
from web.artifact_store import ArtifactStore


def _default_artifact_root() -> Path:
    return Path.cwd().parent / "var" / "artifacts"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register a build artifact into the artifact store.")
    parser.add_argument("--path", required=True, type=Path, help="Path to the artifact file (WIM/ISO/zip).")
    parser.add_argument("--sidecar", required=True, type=Path, help="Path to the sidecar JSON.")
    parser.add_argument("--extension", required=True, help="File extension to use in the store (wim, iso, zip).")
    parser.add_argument("--artifact-root", type=Path, default=None,
                        help="Artifact-store root (defaults to ../var/artifacts).")
    args = parser.parse_args(argv)

    artifact_root = args.artifact_root or _default_artifact_root()
    artifact_root.mkdir(parents=True, exist_ok=True)

    try:
        sidecar = load_sidecar(args.sidecar)
    except SidecarValidationError as exc:
        print(f"sidecar validation failed: {exc}", file=sys.stderr)
        return 2

    store = ArtifactStore(artifact_root)
    try:
        record = store.register(args.path, sidecar, extension=args.extension)
    except ValueError as exc:
        print(f"register failed: {exc}", file=sys.stderr)
        return 3

    print(f"registered {record.kind.value} {record.sha256}")
    print(f"  size:           {record.size}")
    print(f"  relative_path:  {record.relative_path}")
    print(f"  registered_at:  {record.registered_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
