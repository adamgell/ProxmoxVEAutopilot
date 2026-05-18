"""Shared OSD client package file helpers."""
from __future__ import annotations

import base64
from hashlib import sha256
from pathlib import Path


CONFIG_PATH = r"V:\ProgramData\ProxmoxVEAutopilot\OSD\osd-config.json"

PACKAGE_FILES = (
    (
        Path("SetupComplete.cmd"),
        r"V:\Windows\Setup\Scripts\SetupComplete.cmd",
    ),
    (
        Path("osd-client") / "OsdClient.ps1",
        r"V:\ProgramData\ProxmoxVEAutopilot\OSD\OsdClient.ps1",
    ),
    (
        Path("FixRecoveryPartition.ps1"),
        r"V:\ProgramData\ProxmoxVEAutopilot\OSD\FixRecoveryPartition.ps1",
    ),
    (
        Path("Get-WindowsAutopilotInfo.ps1"),
        r"V:\ProgramData\ProxmoxVEAutopilot\OSD\Get-WindowsAutopilotInfo.ps1",
    ),
)


def files_dir() -> Path:
    from web import app as web_app

    return web_app.FILES_DIR


def content_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def osd_client_files(root: Path | None = None) -> list[dict[str, str]]:
    package_root = root or files_dir()
    sources = [(package_root / relative_path, target_path)
               for relative_path, target_path in PACKAGE_FILES]
    missing = [str(source_path) for source_path, _ in sources
               if not source_path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    return [
        {
            "path": target_path,
            "content_b64": content_b64(source_path),
            "sha256": sha256_file(source_path),
            "size_bytes": source_path.stat().st_size,
        }
        for source_path, target_path in sources
    ]
