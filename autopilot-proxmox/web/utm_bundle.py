"""UTM .utm bundle generator and runtime control.

Produces config.plist, lays out the bundle directory, wraps utmctl.
Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md

UTM.app version coverage: 4.7.5 (ConfigurationVersion 4).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import plistlib
import random
import shutil
import struct
import subprocess
import sys
import time
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
    # Optional. UTM unconditionally appends `vgamem_mb=<val>` to the -device
    # arg when this is set, which QEMU rejects for virtio-ramfb-gl (that
    # device has no vgamem_mb property). Leave None for virtio-ramfb-gl
    # and related virtio-gpu variants; set a value only for VGA-family
    # devices that support the property.
    vga_ram_mib: int | None = None


@dataclass
class NetworkSpec:
    hardware: str = "virtio-net-pci"
    mode: str = "Shared"                # UTM shared-NAT (QEMUNetworkMode rawValue)
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
    entry = {
        "Hardware":          d.hardware,
        "DynamicResolution": d.dynamic_resolution,
        "NativeResolution":  d.native_resolution,
        **_DEFAULT_DISPLAY_FILTERS,
    }
    if d.vga_ram_mib is not None:
        entry["VgaRamMib"] = d.vga_ram_mib
    return entry


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
        # UTMBackend enum rawValue is "QEMU" (UTMConfiguration.swift). UTM's
        # root decode throws UTMConfigurationError.invalidBackend on mismatch.
        "Backend": "QEMU",
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


def create_qcow2(dest: pathlib.Path, virtual_size_gib: int,
                 qemu_img: str = "qemu-img") -> None:
    """Create a sparse qcow2 at `dest` with the given virtual size.

    Uses the qemu-img on PATH by default. UTM.app ships one at
    `/Applications/UTM.app/Contents/MacOS/qemu-img` — callers can override
    via the `qemu_img` arg when a specific binary is required.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [qemu_img, "create", "-f", "qcow2", str(dest), f"{virtual_size_gib}G"],
        check=True, capture_output=True, text=True,
    )


def prepare_efi_vars(
    efi_vars_path: pathlib.Path,
    boot_filepath: str = "\\efi\\boot\\bootaa64.efi",
    title: str = "autounattend",
) -> bool:
    """Add a Boot0000 + BootOrder=0000 entry to an EDK2 NVRAM varstore so
    AAVMF boots straight into the autounattend ISO instead of dropping to
    the EFI shell on first start.

    The boot entry uses a FilePath-only device path
    (`FilePath(\\efi\\boot\\bootaa64.efi)` by default), which AAVMF's BDS
    resolves against every connected media handle: it finds the file on
    whichever removable USB CD has it. This is the same shape the EDK2
    "default boot" code synthesises on first boot for removable media,
    just baked in ahead of time so we don't need to rely on UTM's
    AppleScript `input keystroke` fallback.

    Args:
        efi_vars_path: path to the per-bundle `efi_vars.fd`. Modified in
            place. Caller must have already copied `edk2-arm-secure-vars.fd`
            (or another seed varstore) here via `write_bundle`.
        boot_filepath: EFI-style absolute path inside the boot media.
            Default targets the autounattend ISO's bootaa64.efi loader.
        title: human-readable Boot#### title (shown in firmware UI).

    Returns True on success. Returns False (no exception) when
    `virt-firmware` is not installed -- callers fall back to the legacy
    keystroke escape via the playbook's `utm_boot_fallback_keystrokes`
    variable. Raises on any other unexpected failure (corrupt varstore,
    write error) so we don't silently boot to EFI shell with users
    thinking the fix landed.
    """
    try:
        from virt.firmware.efi import devpath, efivar, ucs16  # noqa: F401
        from virt.firmware.varstore import autodetect
    except ImportError:
        return False

    efi_vars_path = pathlib.Path(efi_vars_path)
    try:
        varstore = autodetect.open_varstore(str(efi_vars_path))
    except (struct.error, ValueError):
        # autodetect parses the file with a chain of probe()s; when the
        # input is not a real EDK2 / AWS / JSON varstore (e.g. a unit
        # test stub), the AWS probe trips on its 16-byte struct header.
        # Treat as "no varstore to bake" and let the caller fall back.
        return False
    if varstore is None:
        return False
    varlist = varstore.get_varlist()

    # Build the FilePath-only device path. AAVMF BDS treats a single
    # FilePath node as "try this file on every connected filesystem",
    # which is exactly the behaviour we want for a removable USB CD
    # whose volume GUID is not known until QEMU enumerates it.
    bpath = devpath.DevicePath.filepath(boot_filepath)

    # Use index 0x0000 deterministically. add_boot_entry() picks the
    # first free slot, but the stock UTM seed has Boot0000-Boot0004
    # already populated by EDK2 defaults (UiApp, UEFI QEMU USB
    # HARDDRIVE, Misc Device, EFI Shell). We *replace* Boot0000 with
    # our entry so we don't depend on slot ordering.
    varlist.set_boot_entry(0x0000, title, bpath)

    # Force BootOrder so 0x0000 is tried first. The existing entries
    # remain in the varstore (firmware can still fall back to them),
    # but BdsBoot iterates BootOrder in order so our ISO loader runs
    # before the original Boot0001 (USB HARDDRIVE) etc.
    existing_order = varlist.get("BootOrder")
    tail: list[int] = []
    if existing_order is not None:
        # Existing BootOrder is a packed array of little-endian uint16.
        raw = bytes(existing_order.data)
        for i in range(0, len(raw), 2):
            idx = int.from_bytes(raw[i:i + 2], "little")
            if idx != 0x0000:
                tail.append(idx)
    new_order = [0x0000, *tail]
    if existing_order is None:
        existing_order = varlist.create("BootOrder")
    existing_order.set_boot_order(new_order)

    varstore.write_varstore(str(efi_vars_path), varlist)
    return True


def write_bundle(
    spec: BundleSpec,
    bundle_path: pathlib.Path,
    disk_size_gib: int,
    efi_vars_source: pathlib.Path,
    iso_sources: dict[str, pathlib.Path],
    qemu_img: str = "qemu-img",
    bake_boot_entry: bool = True,
) -> dict:
    """Create a fully-populated .utm bundle directory.

    Args:
        spec: the BundleSpec describing the VM.
        bundle_path: absolute path to the .utm directory to create.
        disk_size_gib: virtual size of the system qcow2.
        efi_vars_source: file to copy into Data/efi_vars.fd. When
            `bake_boot_entry=True` (the default) this is then rewritten
            in place to add a Boot0000 + BootOrder pointing at the
            autounattend ISO's bootaa64.efi, so AAVMF skips the EFI
            shell on first start.
        iso_sources: maps the drive's image_name (e.g. "Win11.iso") to the
            file on disk to copy into Data/. Drives whose image_name is
            None or missing from this map are skipped -- useful for the
            system disk (no ISO) and during tests.
        qemu_img: qemu-img binary path.
        bake_boot_entry: when True, call prepare_efi_vars on the freshly
            copied efi_vars.fd. Set to False to keep the legacy
            keystroke-escape behaviour (the seed file's stock BootOrder
            drops to EFI shell). Silently no-op when virt-firmware is
            not installed -- callers must handle the fallback path.

    Returns a dict {"uuid", "bundle_path", "drive_uuids"}.
    """
    bundle_path = pathlib.Path(bundle_path).resolve()
    data_dir = bundle_path / "Data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. config.plist
    (bundle_path / "config.plist").write_bytes(render_plist_bytes(spec))

    # 2. system disk (first drive with ImageType=Disk)
    for drive in spec.drives:
        if drive.image_type == "Disk":
            disk_name = drive.identifier.upper() + ".qcow2"
            create_qcow2(data_dir / disk_name, disk_size_gib, qemu_img=qemu_img)
            break

    # 3. EFI vars (copy seed, then optionally bake a Boot0000 entry so
    #    AAVMF boots straight into the autounattend ISO without dropping
    #    to the EFI shell on first start).
    efi_vars_dest = data_dir / "efi_vars.fd"
    shutil.copyfile(efi_vars_source, efi_vars_dest)
    if bake_boot_entry:
        prepare_efi_vars(efi_vars_dest)

    # 4. ISOs
    for drive in spec.drives:
        if drive.image_type != "CD" or drive.image_name is None:
            continue
        src = iso_sources.get(drive.image_name)
        if src is None:
            continue  # caller chose not to supply this ISO
        shutil.copyfile(src, data_dir / drive.image_name)

    return {
        "uuid": spec.uuid.upper(),
        "bundle_path": str(bundle_path),
        "drive_uuids": [d.identifier.upper() for d in spec.drives],
    }


DEFAULT_UTMCTL = "/Applications/UTM.app/Contents/MacOS/utmctl"
OPEN_COMMAND = "/usr/bin/open"


class UtmctlClient:
    """Thin subprocess wrapper around UTM's utmctl CLI."""

    def __init__(self,
                 utmctl: str = DEFAULT_UTMCTL,
                 open_command: str = OPEN_COMMAND) -> None:
        self.utmctl = utmctl
        self.open_command = open_command

    def _run(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.utmctl, *args],
            input=input_text, capture_output=True, text=True, check=True,
        )

    def register(self, bundle_path: pathlib.Path,
                 poll_attempts: int = 30, poll_delay: float = 0.5) -> str:
        """Register a .utm bundle with UTM and return its UUID.

        UTM 4.7.5's utmctl has no `register` subcommand. Registration goes
        through Launch Services via `open -a UTM <bundle>`; UTM picks the
        bundle up asynchronously and it appears in `utmctl list` shortly
        after. We poll list for up to ~15 s (30 × 0.5 s) looking for the
        bundle stem as the VM name, then return the UUID UTM reports.

        The UUID we return matches the one we wrote into the bundle's
        config.plist via render_plist — UTM adopts the plist's UUID
        rather than assigning a fresh one, which is what we want for
        determinism.
        """
        subprocess.run(
            [self.open_command, "-a", "UTM", str(bundle_path)],
            check=True, capture_output=True, text=True,
        )
        bundle_name = pathlib.Path(bundle_path).stem
        for _ in range(poll_attempts):
            result = subprocess.run(
                [self.utmctl, "list"], capture_output=True, text=True, check=False,
            )
            for line in result.stdout.splitlines():
                parts = line.split(maxsplit=2)
                if len(parts) == 3 and parts[2].strip() == bundle_name:
                    return parts[0]
            time.sleep(poll_delay)
        raise RuntimeError(
            f"UTM did not register bundle within {poll_attempts * poll_delay:.0f}s: "
            f"{bundle_path}"
        )

    def start(self, uuid: str) -> None:
        self._run("start", uuid)

    def stop(self, uuid: str, force: bool = False) -> None:
        args = ("stop", uuid, "--force") if force else ("stop", uuid)
        self._run(*args)

    def status(self, uuid: str) -> str:
        """Returns UTM's status string ('started', 'stopped', 'paused', ...)."""
        return self._run("status", uuid).stdout.strip()

    def exec(self, uuid: str, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a command inside the guest via utmctl exec. Caller inspects
        returncode and stdout/stderr. No retry; caller handles flaps."""
        return self._run("exec", uuid, "--", *cmd)

    def delete(self, uuid: str) -> None:
        self._run("delete", uuid)


def _spec_from_payload(p: dict) -> BundleSpec:
    """Construct a BundleSpec from a JSON-shaped payload. Unknown sub-dict
    keys raise TypeError via dataclass(**). Missing sub-dicts get defaults."""
    return BundleSpec(
        name=p["name"],
        uuid=p["uuid"],
        system=SystemSpec(**p.get("system") or {}),
        qemu=QemuSpec(**p.get("qemu") or {}),
        display=DisplaySpec(**p.get("display") or {}),
        network=NetworkSpec(**p.get("network") or {}),
        drives=[DriveSpec(**d) for d in p["drives"]],
    )


def _cmd_build(args: argparse.Namespace) -> int:
    """Read spec JSON from --spec (file path or '-' for stdin), write bundle
    to --out, print {"uuid": ..., "bundle_path": ..., "drive_uuids": [...]}
    as JSON on stdout. Optionally register the bundle with UTM.
    """
    if args.spec == "-":
        raw = sys.stdin.read()
    else:
        with open(args.spec) as f:
            raw = f.read()
    payload = json.loads(raw)
    spec = _spec_from_payload(payload)

    iso_sources = {name: pathlib.Path(path)
                   for name, path in (payload.get("iso_sources") or {}).items()}

    result = write_bundle(
        spec,
        bundle_path=pathlib.Path(args.out),
        disk_size_gib=int(payload.get("disk_size_gib", 80)),
        efi_vars_source=pathlib.Path(payload["efi_vars_source"]),
        iso_sources=iso_sources,
        bake_boot_entry=bool(payload.get("bake_boot_entry", True)),
    )

    if payload.get("register"):
        client = UtmctlClient()
        assigned = client.register(pathlib.Path(args.out))
        result["registered_uuid"] = assigned

    json.dump(result, sys.stdout)
    return 0


def _cmd_regenerate_golden(args: argparse.Namespace) -> int:
    """Write tests/fixtures/win11_template_expected.plist from the test's
    sample spec. Run this from autopilot-proxmox/ after any intentional
    renderer change."""
    # Import locally to avoid a test dependency during normal imports.
    sys.path.insert(0, "tests")
    from test_utm_bundle import _sample_win11_spec  # type: ignore
    dest = pathlib.Path("tests/fixtures/win11_template_expected.plist")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(render_plist_bytes(_sample_win11_spec()))
    print(f"wrote {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="utm_bundle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build", help="write a .utm bundle from a spec JSON")
    build.add_argument("--spec", required=True, help="path to spec JSON, or '-' for stdin")
    build.add_argument("--out", required=True, help="absolute path to the .utm bundle to create")
    build.set_defaults(func=_cmd_build)
    regen = sub.add_parser("_regenerate_golden_fixture",
                           help="(dev) rewrite tests/fixtures/win11_template_expected.plist")
    regen.set_defaults(func=_cmd_regenerate_golden)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
