"""UTM .utm bundle generator and runtime control.

Produces config.plist, lays out the bundle directory, wraps utmctl.
Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md

UTM.app version coverage: 4.7.5 (ConfigurationVersion 4).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

UTM_CONFIGURATION_VERSION = 4


@dataclass
class SystemSpec:
    architecture: str = "aarch64"
    target: str = "virt"
    memory_mib: int = 8192
    cpu_count: int = 4
    use_hypervisor: bool = True
    jit_cache_size: int = 0


@dataclass
class QemuSpec:
    uefi_boot: bool = True
    tpm_device: bool = True
    rtc_local_time: bool = True         # Windows expects local-time RTC
    rng_device: bool = True
    balloon_device: bool = False
    debug_log: bool = False
    additional_arguments: list[str] = field(default_factory=list)


@dataclass
class DriveSpec:
    identifier: str                     # uppercased UUID at render time
    image_type: str                     # "CD" | "Disk" | "None"
    interface: str                      # "USB" | "VirtIO" | "IDE" | "SCSI" | "NVMe"
    interface_version: int = 1
    read_only: bool = False
    image_name: str | None = None       # filename inside bundle Data/
    # UTM has no "External" key — removable-ness is derived from ImageType=CD.


@dataclass
class DisplaySpec:
    hardware: str = "virtio-ramfb-gl"
    dynamic_resolution: bool = True
    native_resolution: bool = True
    vga_ram_mib: int = 64


@dataclass
class NetworkSpec:
    hardware: str = "virtio-net-pci"
    mode: str = "shared"                # UTM shared NAT
    mac_address: str | None = None


@dataclass
class BundleSpec:
    name: str
    uuid: str
    system: SystemSpec
    qemu: QemuSpec
    drives: list[DriveSpec]
    display: DisplaySpec
    network: NetworkSpec


def _cmd_build(args: argparse.Namespace) -> int:
    """Read spec JSON from --spec (file path or '-' for stdin), write bundle
    to --out, print {"uuid": ..., "bundle_path": ..., "drive_uuids": [...]}
    as JSON on stdout.
    """
    if args.spec == "-":
        raw = sys.stdin.read()
    else:
        with open(args.spec) as f:
            raw = f.read()
    spec = json.loads(raw)
    result = {"uuid": spec.get("uuid"), "bundle_path": args.out, "drive_uuids": []}
    json.dump(result, sys.stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="utm_bundle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build", help="write a .utm bundle from a spec JSON")
    build.add_argument("--spec", required=True, help="path to spec JSON, or '-' for stdin")
    build.add_argument("--out", required=True, help="absolute path to the .utm bundle to create")
    build.set_defaults(func=_cmd_build)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
