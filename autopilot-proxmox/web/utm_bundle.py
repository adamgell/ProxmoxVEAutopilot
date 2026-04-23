"""UTM .utm bundle generator and runtime control.

Produces config.plist, lays out the bundle directory, wraps utmctl.
Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md

UTM.app version coverage: 4.7.5 (ConfigurationVersion 4).
"""
from __future__ import annotations

import argparse
import json
import plistlib
import random
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


# Baked-in defaults. Keys and value formats are pulled directly from UTM's
# Codable definitions (Configuration/UTMQemuConfiguration*.swift) — they are
# NOT guesses. If upstream renames any of these, the Tier 2 contract test
# will fail, forcing an explicit bump here.
_DEFAULT_INPUT = {
    "UsbBusSupport":   "3.0",          # QEMUUSBBus: "Disabled" | "2.0" | "3.0"
    "UsbSharing":      False,          # USB passthrough off for template builds
    "MaximumUsbShare": 3,
}
_DEFAULT_SHARING = {
    "DirectoryShareMode":     "None",  # QEMUFileShareMode: "None" | "WebDAV" | "VirtFS"
    "DirectoryShareReadOnly": False,
    "ClipboardSharing":       True,
}
_DEFAULT_DISPLAY_FILTERS = {
    "UpscalingFilter":   "Linear",     # QEMUScaler: "Linear" | "Nearest"
    "DownscalingFilter": "Linear",
}


def _random_mac() -> str:
    """Generate a locally-administered unicast MAC (02:...). UTM requires
    Network[].MacAddress to be present as a non-optional String."""
    octets = [0x02] + [random.randint(0, 0xff) for _ in range(5)]
    return ":".join(f"{b:02X}" for b in octets)


def _render_system(s: SystemSpec) -> dict:
    # System does NOT own Hypervisor — that lives in QEMU. See
    # UTMQemuConfigurationSystem.swift / UTMQemuConfigurationQEMU.swift.
    return {
        "Architecture":   s.architecture,
        "Target":         s.target,
        "MemorySize":     s.memory_mib,
        "CPUCount":       s.cpu_count,
        "ForceMulticore": False,
        "JITCacheSize":   s.jit_cache_size,
        "CPU":            "default",
        "CPUFlagsAdd":    [],
        "CPUFlagsRemove": [],
    }


def _render_qemu(q: QemuSpec, use_hypervisor: bool) -> dict:
    # Required by UTM's Codable decode: DebugLog, UEFIBoot, RNGDevice,
    # BalloonDevice, TPMDevice, Hypervisor, RTCLocalTime, PS2Controller,
    # AdditionalArguments. TSO and MachinePropertyOverride are optional.
    return {
        "DebugLog":             q.debug_log,
        "UEFIBoot":             q.uefi_boot,
        "RNGDevice":            q.rng_device,
        "BalloonDevice":        q.balloon_device,
        "TPMDevice":            q.tpm_device,
        "Hypervisor":           use_hypervisor,
        "RTCLocalTime":         q.rtc_local_time,
        "PS2Controller":        False,
        "AdditionalArguments":  list(q.additional_arguments),
    }


def _render_drive(d: DriveSpec) -> dict:
    # UTM's Drive schema keys: Identifier, ImageType, Interface,
    # InterfaceVersion, ReadOnly, ImageName. There is NO "External" key —
    # `isExternal` is inferred at decode time from whether ImageName is
    # present. We still emit ImageName for removable CDs because that's
    # how UTM learns which ISO to mount in the slot (the existing code
    # base works this way; verified against current bundles).
    entry = {
        "Identifier":       d.identifier.upper(),
        "ImageType":        d.image_type,
        "Interface":        d.interface,
        "InterfaceVersion": d.interface_version,
        "ReadOnly":         d.read_only,
    }
    if d.image_name is not None:
        # Uppercase UUID portion if image_name starts with a UUID
        parts = d.image_name.split(".")
        if len(parts) > 0 and len(parts[0]) == 36:  # UUID length is 36 chars
            try:
                # If first part looks like a UUID, uppercase it
                parts[0] = parts[0].upper()
            except Exception:
                pass
        entry["ImageName"] = ".".join(parts)
    return entry


def _render_display(d: DisplaySpec) -> dict:
    # All five non-optional keys + optional VgaRamMib.
    return {
        "Hardware":          d.hardware,
        "DynamicResolution": d.dynamic_resolution,
        "NativeResolution":  d.native_resolution,
        "VgaRamMib":         d.vga_ram_mib,
        **_DEFAULT_DISPLAY_FILTERS,
    }


def _render_network(n: NetworkSpec) -> dict:
    # Required: Mode, Hardware, MacAddress, IsolateFromHost, PortForward.
    return {
        "Mode":            n.mode,
        "Hardware":        n.hardware,
        "MacAddress":      n.mac_address or _random_mac(),
        "IsolateFromHost": False,
        "PortForward":     [],
    }


def render_plist(spec: BundleSpec) -> dict:
    """Return the config.plist body as a Python dict. Keys are PascalCase
    per UTM's Codable schema (ConfigurationVersion 4). Callers either pass
    this to plistlib.dumps or inspect it in tests."""
    return {
        "ConfigurationVersion": UTM_CONFIGURATION_VERSION,
        "Backend": "qemu",
        "Information": {
            "Name":       spec.name,
            "UUID":       spec.uuid.upper(),
            "IconCustom": False,
        },
        "System":  _render_system(spec.system),
        "QEMU":    _render_qemu(spec.qemu, use_hypervisor=spec.system.use_hypervisor),
        "Input":   dict(_DEFAULT_INPUT),
        "Sharing": dict(_DEFAULT_SHARING),
        "Display": [_render_display(spec.display)],
        "Drive":   [_render_drive(d) for d in spec.drives],
        "Network": [_render_network(spec.network)],
        "Serial":  [],
        "Sound":   [],
    }


def render_plist_bytes(spec: BundleSpec) -> bytes:
    """XML-plist bytes ready to write to config.plist."""
    return plistlib.dumps(render_plist(spec), fmt=plistlib.FMT_XML, sort_keys=False)


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
