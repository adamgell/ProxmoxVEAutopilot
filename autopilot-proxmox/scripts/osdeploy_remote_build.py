#!/usr/bin/env python3
"""SSH-backed OSDeploy Windows Server media build wrapper."""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

DEFAULT_SSH_KEY_PATH = Path("/app/secrets/osdeploy_devmachine_ed25519")


def windows_path_to_scp_path(path: str) -> str:
    return path.replace("\\", "/")


def remote_basename(path: str) -> str:
    return windows_path_to_scp_path(path).rstrip("/").rsplit("/", 1)[-1]


def parse_manifest_path(lines: list[str]) -> str:
    pattern = re.compile(r"[A-Za-z]:[\\/].*osdeploy-server-[^\\/\s]+\.json")
    for line in lines:
        match = pattern.search(line)
        if match:
            return match.group(0)
    raise RuntimeError("remote build did not print an OSDeploy manifest path")


def ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_remote_script(
    *,
    job_id: str,
    remote_root: str,
    archive_name: str,
    arch: str,
    osdeploy_version: str,
    osdbuilder_version: str,
    adk_version: str,
    source_media_path: str = "",
    image_name: str = "Windows Server 2025 Datacenter",
    image_index: int = 4,
    os_version: str = "Windows Server 2025",
    os_edition: str = "Datacenter",
    os_language: str = "en-us",
    controller_url: str = "",
    fallback_controller_url: str = "",
) -> str:
    work_dir = rf"{remote_root}\work\osdeploy-src-{job_id}"
    archive_path = rf"{remote_root}\work\{archive_name}"
    outputs = rf"{remote_root}\outputs"
    script = rf"{work_dir}\tools\osdeploy-build\build-osdeploy.ps1"
    build_args = [
        f"-Arch {arch}",
        f"-OutputDir {ps_quote(outputs)}",
        f"-OSDeployVersion {ps_quote(osdeploy_version)}",
        f"-OSDBuilderVersion {ps_quote(osdbuilder_version)}",
        f"-ADKVersion {ps_quote(adk_version)}",
        f"-ImageName {ps_quote(image_name)}",
        f"-ImageIndex {int(image_index)}",
        f"-OSVersion {ps_quote(os_version)}",
        f"-OSEdition {ps_quote(os_edition)}",
        f"-OSLanguage {ps_quote(os_language)}",
    ]
    if controller_url:
        build_args.append(f"-ControllerUrl {ps_quote(controller_url)}")
    if fallback_controller_url:
        build_args.append(f"-FallbackControllerUrl {ps_quote(fallback_controller_url)}")
    if source_media_path:
        build_args.append(f"-SourceMediaPath {ps_quote(source_media_path)}")
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
            f"& $pwsh -NoProfile -ExecutionPolicy Bypass -File {ps_quote(script)} {' '.join(build_args)}",
            "if ($LASTEXITCODE -ne 0) { throw \"OSDeploy build failed: $LASTEXITCODE\" }",
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
            tar.add(path, arcname=path.relative_to(repo_root), recursive=False)


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
    known_hosts_path = Path(known_hosts) if known_hosts else output_dir / "osdeploy_known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "ConnectTimeout=30",
    ]
    if key_path.exists():
        options.extend(["-i", str(key_path)])
    return options


def scp_remote_path(remote: str, windows_path: str) -> str:
    return f"{remote}:{windows_path_to_scp_path(windows_path)}"


def sibling_remote_path(manifest_path: str, suffix: str) -> str:
    return re.sub(r"\.json$", suffix, manifest_path, flags=re.IGNORECASE)


def _sha256_file(path: Path) -> str:
    h = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_build_output(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"OSDeploy build output missing: {label} {path}")


def record_artifact(
    manifest_path: Path,
    local_output_dir: Path,
    *,
    build_job_id: str | None = None,
) -> dict:
    manifest = json.loads(manifest_path.read_text())
    iso_path = local_output_dir / remote_basename(manifest["output_iso"])
    wim_path = local_output_dir / remote_basename(manifest["output_wim"])
    _require_build_output(iso_path, "iso")
    _require_build_output(wim_path, "wim")
    iso_sha = manifest.get("iso_sha256") or _sha256_file(iso_path)
    wim_sha = manifest.get("wim_sha256") or _sha256_file(wim_path)

    from web import db_pg, osdeploy_pg

    with db_pg.connection() as conn:
        osdeploy_pg.init(conn)
        return osdeploy_pg.create_artifact(
            conn,
            architecture=manifest["architecture"],
            osdeploy_module_version=manifest["osdeploy_module_version"],
            osdbuilder_module_version=manifest["osdbuilder_module_version"],
            adk_version=manifest["adk_version"],
            build_sha=manifest["build_sha"],
            iso_path=str(iso_path),
            wim_path=str(wim_path),
            manifest_path=str(manifest_path),
            iso_sha256=iso_sha,
            wim_sha256=wim_sha,
            source_media=manifest["source_media"],
            image_name=manifest["image_name"],
            image_index=int(manifest["image_index"]),
            os_version=manifest.get("os_version") or osdeploy_pg.DEFAULT_OS_VERSION,
            os_edition=manifest.get("os_edition") or osdeploy_pg.DEFAULT_OS_EDITION,
            os_language=manifest.get("os_language") or osdeploy_pg.DEFAULT_OS_LANGUAGE,
            built_by_host=manifest.get("built_by_host") or "unknown",
            build_job_id=build_job_id,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--remote", default="Adam.Gell@10.211.55.6")
    parser.add_argument("--remote-root", default=r"F:\BuildRoot")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--output-dir", default=str(APP_ROOT / "output" / "osdeploy"))
    parser.add_argument("--arch", default="amd64")
    parser.add_argument("--osdeploy-version", default="26.4.17.1")
    parser.add_argument("--osdbuilder-version", default="25.12.1")
    parser.add_argument("--adk-version", default="10.1.26100.1")
    parser.add_argument("--source-media-path", default="")
    parser.add_argument("--image-name", default="Windows Server 2025 Datacenter")
    parser.add_argument("--image-index", type=int, default=4)
    parser.add_argument("--os-version", default="Windows Server 2025")
    parser.add_argument("--os-edition", default="Datacenter")
    parser.add_argument("--os-language", default="en-us")
    parser.add_argument("--controller-url", default="")
    parser.add_argument("--fallback-controller-url", default="")
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
    archive_name = f"osdeploy-src-{args.job_id}.tar.gz"

    with tempfile.TemporaryDirectory(prefix="osdeploy-build-") as tmp:
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
            osdeploy_version=args.osdeploy_version,
            osdbuilder_version=args.osdbuilder_version,
            adk_version=args.adk_version,
            source_media_path=args.source_media_path,
            image_name=args.image_name,
            image_index=args.image_index,
            os_version=args.os_version,
            os_edition=args.os_edition,
            os_language=args.os_language,
            controller_url=args.controller_url,
            fallback_controller_url=args.fallback_controller_url,
        )
        lines = run_streamed(["ssh", *ssh_options, args.remote, build_remote_command(remote_script)])
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
