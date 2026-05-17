#!/usr/bin/env python3
"""Compatibility entrypoint for OSDeploy artifact builds."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import osdeploy_remote_build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OSDeploy artifact build job")
    parser.add_argument("action", choices=["build"])
    parser.add_argument("--arch", default="amd64")
    parser.add_argument("--job-id", default="manual-osdeploy-build")
    parser.add_argument("--remote", default="Adam.Gell@10.211.55.6")
    parser.add_argument("--remote-root", default=r"F:\BuildRoot")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parents[1] / "output" / "osdeploy"))
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
    return osdeploy_remote_build.main([
        "--job-id",
        args.job_id,
        "--remote",
        args.remote,
        "--remote-root",
        args.remote_root,
        "--repo-root",
        args.repo_root,
        "--output-dir",
        args.output_dir,
        "--arch",
        args.arch,
        "--osdeploy-version",
        args.osdeploy_version,
        "--osdbuilder-version",
        args.osdbuilder_version,
        "--adk-version",
        args.adk_version,
        "--source-media-path",
        args.source_media_path,
        "--image-name",
        args.image_name,
        "--image-index",
        str(args.image_index),
        "--os-version",
        args.os_version,
        "--os-edition",
        args.os_edition,
        "--os-language",
        args.os_language,
        "--controller-url",
        args.controller_url,
        "--fallback-controller-url",
        args.fallback_controller_url,
        *(["--ssh-key", args.ssh_key] if args.ssh_key else []),
        *(["--known-hosts", args.known_hosts] if args.known_hosts else []),
        *(["--no-record"] if args.no_record else []),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
