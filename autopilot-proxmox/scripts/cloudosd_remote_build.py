#!/usr/bin/env python3
"""SSH-backed CloudOSD ISO build wrapper.

The Linux/macOS builder queue owns the job, but ADK/DISM run on the Windows
dev machine. This wrapper stages the current repo to the remote host, invokes
tools/cloudosd-build/build-cloudosd.ps1, copies artifacts back, and records a
CloudOSD artifact row in PostgreSQL.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

DEFAULT_SSH_KEY_PATH = Path("/app/secrets/cloudosd_devmachine_ed25519")


def windows_path_to_scp_path(path: str) -> str:
    return path.replace("\\", "/")


def remote_basename(path: str) -> str:
    return windows_path_to_scp_path(path).rstrip("/").rsplit("/", 1)[-1]


def parse_manifest_path(lines: list[str]) -> str:
    pattern = re.compile(r"[A-Za-z]:[\\/].*cloudosd-autopilot-[^\\/\s]+\.json")
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(0)
    raise RuntimeError("remote build did not print a CloudOSD manifest path")


def build_remote_script(
    *,
    job_id: str,
    remote_root: str,
    archive_name: str,
    arch: str,
    osdcloud_version: str,
) -> str:
    work_dir = rf"{remote_root}\work\cloudosd-src-{job_id}"
    archive_path = rf"{remote_root}\work\{archive_name}"
    outputs = rf"{remote_root}\outputs"
    script = rf"{work_dir}\tools\cloudosd-build\build-cloudosd.ps1"
    return "; ".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"Remove-Item -LiteralPath '{work_dir}' -Recurse -Force -ErrorAction SilentlyContinue",
            f"New-Item -ItemType Directory -Path '{work_dir}' -Force | Out-Null",
            f"New-Item -ItemType Directory -Path '{outputs}' -Force | Out-Null",
            f"tar -xf '{archive_path}' -C '{work_dir}'",
            "if ($LASTEXITCODE -ne 0) { throw \"tar extraction failed: $LASTEXITCODE\" }",
            "$pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source",
            "if (-not $pwsh) { $pwsh = 'powershell.exe' }",
            (
                f"& $pwsh -NoProfile -ExecutionPolicy Bypass -File '{script}' "
                f"-Arch {arch} -OutputDir '{outputs}' "
                f"-OSDCloudVersion '{osdcloud_version}'"
            ),
            "if ($LASTEXITCODE -ne 0) { throw \"CloudOSD build failed: $LASTEXITCODE\" }",
        ]
    )


def build_remote_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def _should_skip(path: Path, repo_root: Path) -> bool:
    rel = path.relative_to(repo_root)
    parts = set(rel.parts)
    if parts & {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".venv-test",
        "__pycache__",
        "node_modules",
    }:
        return True
    if rel.parts and rel.parts[0] in {"jobs", "output"}:
        return True
    return False


def create_source_bundle(repo_root: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as tar:
        for path in repo_root.rglob("*"):
            if _should_skip(path, repo_root):
                continue
            arcname = path.relative_to(repo_root)
            tar.add(path, arcname=arcname, recursive=False)


def run_streamed(cmd: list[str]) -> list[str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line.rstrip("\n"))
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"command failed with exit code {code}: {cmd!r}")
    return lines


def build_ssh_options(*, output_dir: Path, ssh_key: str = "", known_hosts: str = "") -> list[str]:
    key_path = Path(ssh_key) if ssh_key else DEFAULT_SSH_KEY_PATH
    known_hosts_path = Path(known_hosts) if known_hosts else output_dir / "cloudosd_known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    options = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts_path}",
        "-o", "ConnectTimeout=30",
    ]
    if key_path.exists():
        options.extend(["-i", str(key_path)])
    return options


def scp_remote_path(remote: str, windows_path: str) -> str:
    return f"{remote}:{windows_path_to_scp_path(windows_path)}"


def sibling_remote_path(manifest_path: str, suffix: str) -> str:
    return re.sub(r"\.json$", suffix, manifest_path, flags=re.IGNORECASE)


def record_artifact(
    manifest_path: Path,
    local_output_dir: Path,
    *,
    build_job_id: str | None = None,
) -> dict:
    from web import cloudosd_pg, db_pg

    manifest = json.loads(manifest_path.read_text())
    iso_path = local_output_dir / remote_basename(manifest["output_iso"])
    wim_path = local_output_dir / remote_basename(manifest["output_wim"])
    iso_sha = manifest.get("iso_sha256") or _sha256_file(iso_path)
    wim_sha = manifest.get("wim_sha256") or _sha256_file(wim_path)
    with db_pg.connection() as conn:
        cloudosd_pg.init(conn)
        return cloudosd_pg.create_artifact(
            conn,
            architecture=manifest["architecture"],
            osdcloud_module_version=manifest["osdcloud_module_version"],
            build_sha=manifest["build_sha"],
            iso_path=str(iso_path),
            wim_path=str(wim_path),
            manifest_path=str(manifest_path),
            iso_sha256=iso_sha,
            wim_sha256=wim_sha,
            built_by_host=manifest.get("built_by_host") or "unknown",
            build_job_id=build_job_id,
        )


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--remote", default="Adam.Gell@10.211.55.6")
    parser.add_argument("--remote-root", default=r"F:\BuildRoot")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--output-dir", default=str(APP_ROOT / "output" / "cloudosd"))
    parser.add_argument("--arch", default="amd64")
    parser.add_argument("--osdcloud-version", default="26.4.17.1")
    parser.add_argument("--ssh-key", default="")
    parser.add_argument("--known-hosts", default="")
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ssh_options = build_ssh_options(
        output_dir=output_dir,
        ssh_key=args.ssh_key,
        known_hosts=args.known_hosts,
    )
    archive_name = f"cloudosd-src-{args.job_id}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="cloudosd-build-") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / archive_name
        create_source_bundle(repo_root, archive_path)

        remote_work_root = rf"{args.remote_root}\work"
        remote_archive_path = rf"{remote_work_root}\{archive_name}"
        mkdir_cmd = (
            "powershell.exe -NoProfile -ExecutionPolicy Bypass "
            f"-Command \"New-Item -ItemType Directory -Path '{remote_work_root}' -Force | Out-Null\""
        )
        run_streamed(["ssh", *ssh_options, args.remote, mkdir_cmd])
        run_streamed([
            "scp",
            *ssh_options,
            str(archive_path),
            scp_remote_path(args.remote, remote_archive_path),
        ])

        remote_script = build_remote_script(
            job_id=args.job_id,
            remote_root=args.remote_root,
            archive_name=archive_name,
            arch=args.arch,
            osdcloud_version=args.osdcloud_version,
        )
        remote_command = build_remote_command(remote_script)
        lines = run_streamed(["ssh", *ssh_options, args.remote, remote_command])
        manifest_remote = parse_manifest_path(lines)
        wim_remote = sibling_remote_path(manifest_remote, ".wim")
        iso_remote = sibling_remote_path(manifest_remote, ".iso")

        for remote_path in (manifest_remote, wim_remote, iso_remote):
            run_streamed([
                "scp",
                *ssh_options,
                scp_remote_path(args.remote, remote_path),
                str(output_dir / remote_basename(remote_path)),
            ])

        if args.no_record:
            artifact = {
                "recorded": False,
                "manifest_path": str(output_dir / remote_basename(manifest_remote)),
            }
        else:
            artifact = record_artifact(
                output_dir / remote_basename(manifest_remote),
                output_dir,
                build_job_id=args.job_id,
            )
        print(json.dumps({"artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
