# UTM Native Lifecycle Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the AppleScript + plist-patch hybrid for UTM template builds with a Python library that writes `config.plist` in one shot, and prove it end-to-end with a fully unattended Windows 11 ARM64 template build on macOS.

**Architecture:** A new Python module `web/utm_bundle.py` owns plist generation (dataclasses → `plistlib.dumps`), bundle layout, `qemu-img` disk creation, `utmctl` control, and `efi_vars.fd` preparation. A thin Ansible task file `render_bundle.yml` shells out to its CLI. Stock `/Applications/UTM.app` remains the VM runtime. Schema drift against upstream UTM source is caught by a diff-based CI job (Tier 1) plus a JSON schema contract extracted from UTM's Swift `CodingKeys` (Tier 2).

**Tech Stack:** Python 3.10+, `plistlib` (stdlib), `dataclasses` (stdlib), `pytest`, `virt-firmware` (pip package, provides `virt-fw-vars`), Ansible (existing), UTM.app 4.7.5 (ConfigurationVersion 4).

**Spec:** `docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md`.

**Prerequisite state:** Currently on branch `feature/utm-macos-arm64-support`. `/Users/Adam.Gell/src/UTM` checked out at commit `884e75c` with `main` branch available. `/Applications/UTM.app` at 4.7.5.

---

## File Structure

**New files:**
- `autopilot-proxmox/web/utm_bundle.py` — Python library + CLI entrypoint
- `autopilot-proxmox/tests/test_utm_bundle.py` — unit tests
- `autopilot-proxmox/tests/fixtures/win11_template_expected.plist` — golden plist for snapshot test
- `autopilot-proxmox/tests/fixtures/utm_schema_contract_v4.json` — extracted key/enum contract
- `autopilot-proxmox/scripts/extract_utm_schema.py` — one-shot contract generator
- `autopilot-proxmox/scripts/check_utm_schema_drift.sh` — CI warning job
- `autopilot-proxmox/web/utm_schema_known_good.txt` — pinned UTM upstream sha
- `autopilot-proxmox/roles/utm_template_builder/tasks/render_bundle.yml` — Ansible wrapper

**Modified files:**
- `autopilot-proxmox/roles/utm_template_builder/tasks/main.yml` — cut `create_bundle.yml` + `customize_plist.yml` includes over to `render_bundle.yml`
- `autopilot-proxmox/roles/utm_answer_iso/templates/unattend.xml.j2` — add `<DriverPaths>` block
- `autopilot-proxmox/playbooks/utm_build_win11_template.yml` — remove UTM quit-relaunch + shrink or delete keystroke block
- `autopilot-proxmox/requirements.txt` — add `virt-firmware`
- `docs/UTM_MACOS_ARM64.md` — update handoff notes after migration completes

**Deleted files (Task 15):**
- `autopilot-proxmox/roles/utm_template_builder/tasks/create_bundle.yml`
- `autopilot-proxmox/roles/utm_template_builder/tasks/customize_plist.yml`

**Unchanged (consumed as-is):**
- `autopilot-proxmox/roles/utm_template_builder/tasks/start_and_wait.yml`
- `autopilot-proxmox/roles/utm_template_builder/tasks/sysprep_finalize.yml`
- `autopilot-proxmox/roles/utm_template_builder/defaults/main.yml`
- `autopilot-proxmox/roles/utm_answer_iso/tasks/main.yml`
- `autopilot-proxmox/roles/utm_answer_iso/templates/firstboot.ps1.j2`
- `autopilot-proxmox/roles/oem_profile_resolver/**`

---

## Execution notes

Run all `pytest` commands from `autopilot-proxmox/` (`cd autopilot-proxmox && pytest ...`). Run all `ansible-playbook` commands from `autopilot-proxmox/`. All paths in commands assume the repo root is `/Users/Adam.Gell/repo/ProxmoxVEAutopilot`.

Every task ends with a commit. Commit messages follow the existing `type(scope): subject` convention (e.g. `feat(utm):`, `test(utm):`, `docs(utm):`, `chore(utm):`).

---

## Task 1: Module skeleton and CLI stub

Create the empty shell of `utm_bundle.py` with a `build` subcommand that reads JSON from stdin and echoes it back on stdout. This proves the Ansible → Python → back handoff before any real logic exists.

**Files:**
- Create: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing CLI smoke test**

Create `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
"""Tests for web.utm_bundle — UTM .utm bundle generator and runtime control.

Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md
"""
import json
import subprocess
import sys


def test_cli_build_echoes_spec_on_stdout(tmp_path):
    """The `build` CLI reads a spec JSON from stdin and echoes the UUID it
    received on stdout as JSON. This proves the Ansible↔Python handoff shape
    before any bundle-writing logic exists.
    """
    spec = {"name": "test", "uuid": "00000000-0000-0000-0000-000000000000"}
    result = subprocess.run(
        [sys.executable, "-m", "web.utm_bundle", "build",
         "--spec", "-", "--out", str(tmp_path / "test.utm")],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(result.stdout)
    assert out["uuid"] == spec["uuid"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_cli_build_echoes_spec_on_stdout -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'web.utm_bundle'`.

- [ ] **Step 3: Write the stub module**

Create `autopilot-proxmox/web/utm_bundle.py`:

```python
"""UTM .utm bundle generator and runtime control.

Produces config.plist, lays out the bundle directory, wraps utmctl.
Spec: docs/superpowers/specs/2026-04-23-utm-native-lifecycle-foundation-design.md

UTM.app version coverage: 4.7.5 (ConfigurationVersion 4).
"""
from __future__ import annotations

import argparse
import json
import sys

UTM_CONFIGURATION_VERSION = 4


def _cmd_build(args: argparse.Namespace) -> int:
    """Read spec JSON from --spec (file path or '-' for stdin), write bundle
    to --out, print {"uuid": ..., "bundle_path": ..., "drive_uuids": [...]}
    as JSON on stdout.
    """
    raw = sys.stdin.read() if args.spec == "-" else open(args.spec).read()
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): utm_bundle.py skeleton with build CLI stub"
```

---

## Task 2: Schema contract extractor (Tier 2 drift defense)

Parse UTM's Swift sources and emit a JSON contract enumerating every plist key and enum value we might reference. This locks down what "valid" means and is the early-warning system when upstream UTM changes shape.

**Files:**
- Create: `autopilot-proxmox/scripts/extract_utm_schema.py`
- Create: `autopilot-proxmox/tests/fixtures/utm_schema_contract_v4.json` (generated)
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing contract-shape test**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
import pathlib


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_schema_contract_has_required_sections():
    """The generated UTM schema contract lists PascalCase keys per section
    and known enum values. If upstream UTM renames a key we emit, the
    renderer tests will fail; this test just confirms the contract file
    itself has the shape we expect."""
    contract = json.loads((FIXTURES / "utm_schema_contract_v4.json").read_text())
    assert contract["ConfigurationVersion"] == 4
    for section in ("System", "QEMU", "Drive", "Display", "Network", "Information"):
        assert section in contract["sections"], f"missing section: {section}"
        assert isinstance(contract["sections"][section], list)
        assert len(contract["sections"][section]) > 0
    # Enum domains used by the renderer
    for enum_name in ("QEMUDriveInterface", "QEMUDriveImageType",
                      "QEMUArchitecture"):
        assert enum_name in contract["enums"]
        assert isinstance(contract["enums"][enum_name], list)
        assert len(contract["enums"][enum_name]) > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_schema_contract_has_required_sections -v
```

Expected: FAIL with `FileNotFoundError: .../utm_schema_contract_v4.json`.

- [ ] **Step 3: Write the extractor script**

Create `autopilot-proxmox/scripts/extract_utm_schema.py`:

```python
#!/usr/bin/env python3
"""Parse UTM's Swift sources and emit a JSON contract describing the
ConfigurationVersion 4 plist schema our renderer targets.

Usage:
    python scripts/extract_utm_schema.py \\
        --utm-source /Users/Adam.Gell/src/UTM \\
        --out tests/fixtures/utm_schema_contract_v4.json

The output lists, per UTM config section (System, QEMU, Drive, ...), the
PascalCase keys UTM's CodingKeys enums expose. It also lists the allowed
values for the enums the renderer references (QEMUDriveInterface, ...).

The script is intentionally regex-based (not a Swift parser) because the
subset we care about follows a narrow, grep-able pattern:

    case camelName = "PascalName"

inside `private enum CodingKeys: String, CodingKey` blocks for sections,
and inside `enum QEMUXxx: String, CaseIterable, QEMUConstant` blocks
for enum values.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# Matches: case foo = "Bar"
#          case foo  =  "Bar"
CASE_LINE = re.compile(r'^\s*case\s+\w+\s*=\s*"([^"]+)"')

# Matches: private enum CodingKeys ...  OR  enum CodingKeys ...
CODING_KEYS_START = re.compile(r'\benum\s+CodingKeys\s*:')

# Matches: enum QEMUDriveInterface: String, CaseIterable, QEMUConstant
ENUM_DECL = re.compile(r'\benum\s+(QEMU\w+)\s*:')

SECTIONS = {
    "UTMQemuConfiguration.swift":            "Root",
    "UTMConfigurationInfo.swift":            "Information",
    "UTMQemuConfigurationSystem.swift":      "System",
    "UTMQemuConfigurationQEMU.swift":        "QEMU",
    "UTMQemuConfigurationDrive.swift":       "Drive",
    "UTMQemuConfigurationDisplay.swift":     "Display",
    "UTMQemuConfigurationInput.swift":       "Input",
    "UTMQemuConfigurationNetwork.swift":     "Network",
    "UTMQemuConfigurationSharing.swift":     "Sharing",
    "UTMQemuConfigurationSerial.swift":      "Serial",
    "UTMQemuConfigurationSound.swift":       "Sound",
}

# We only care about these enum domains in the renderer today; extending is
# cheap (add to the set, re-run the extractor).
ENUM_DOMAINS_OF_INTEREST = {
    "QEMUDriveInterface",
    "QEMUDriveImageType",
    "QEMUArchitecture",
    "QEMUNetworkMode",
    "QEMUDisplayCard",
    "QEMUSoundCard",
    "QEMUNetworkDevice",
}


def extract_section_keys(source_dir: pathlib.Path) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    for filename, section in SECTIONS.items():
        path = source_dir / "Configuration" / filename
        if not path.is_file():
            continue
        keys: list[str] = []
        in_coding_keys = False
        brace_depth = 0
        for line in path.read_text().splitlines():
            if CODING_KEYS_START.search(line):
                in_coding_keys = True
                brace_depth = line.count("{") - line.count("}")
                continue
            if in_coding_keys:
                brace_depth += line.count("{") - line.count("}")
                match = CASE_LINE.match(line)
                if match:
                    keys.append(match.group(1))
                if brace_depth <= 0:
                    in_coding_keys = False
        sections.setdefault(section, [])
        sections[section].extend(keys)
    return sections


def extract_enums(source_dir: pathlib.Path) -> dict[str, list[str]]:
    enums: dict[str, list[str]] = {}
    for swift_file in (source_dir / "Configuration").glob("*.swift"):
        text = swift_file.read_text()
        current_enum: str | None = None
        brace_depth = 0
        for line in text.splitlines():
            decl = ENUM_DECL.search(line)
            if decl and decl.group(1) in ENUM_DOMAINS_OF_INTEREST:
                current_enum = decl.group(1)
                brace_depth = line.count("{") - line.count("}")
                enums.setdefault(current_enum, [])
                continue
            if current_enum:
                brace_depth += line.count("{") - line.count("}")
                match = CASE_LINE.match(line)
                if match:
                    enums[current_enum].append(match.group(1))
                if brace_depth <= 0:
                    current_enum = None
    return enums


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--utm-source", required=True,
                        help="path to utmapp/UTM checkout")
    parser.add_argument("--out", required=True,
                        help="path to write JSON contract")
    args = parser.parse_args(argv)

    source_dir = pathlib.Path(args.utm_source)
    if not (source_dir / "Configuration").is_dir():
        print(f"error: {source_dir} does not look like a UTM checkout "
              "(no Configuration/ directory)", file=sys.stderr)
        return 2

    contract = {
        "ConfigurationVersion": 4,
        "generated_from": str(source_dir),
        "sections": extract_section_keys(source_dir),
        "enums": extract_enums(source_dir),
    }
    pathlib.Path(args.out).write_text(json.dumps(contract, indent=2, sort_keys=True))
    print(f"wrote {args.out} with {sum(len(v) for v in contract['sections'].values())} keys "
          f"across {len(contract['sections'])} sections and "
          f"{sum(len(v) for v in contract['enums'].values())} enum values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Make it executable:

```bash
chmod +x autopilot-proxmox/scripts/extract_utm_schema.py
```

- [ ] **Step 4: Run the extractor to generate the contract**

```bash
mkdir -p autopilot-proxmox/tests/fixtures
python autopilot-proxmox/scripts/extract_utm_schema.py \
    --utm-source /Users/Adam.Gell/src/UTM \
    --out autopilot-proxmox/tests/fixtures/utm_schema_contract_v4.json
```

Expected stdout: something like `wrote .../utm_schema_contract_v4.json with NN keys across 10 sections and MM enum values.`

- [ ] **Step 5: Eyeball the contract**

```bash
python -c "import json; c = json.load(open('autopilot-proxmox/tests/fixtures/utm_schema_contract_v4.json')); print('sections:', sorted(c['sections'].keys())); print('System keys:', c['sections']['System']); print('Drive keys:', c['sections']['Drive']); print('DriveInterface:', c['enums']['QEMUDriveInterface'])"
```

Expected: `System` contains `Architecture`, `Target`, `MemorySize`, `CPUCount`, `Hypervisor`, etc. `Drive` contains `Identifier`, `ImageType`, `Interface`, `InterfaceVersion`, `ReadOnly`, `ImageName`. `QEMUDriveInterface` contains `None`, `IDE`, `SCSI`, `USB`, `VirtIO`, `NVMe`.

- [ ] **Step 6: Run test to verify it passes**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_schema_contract_has_required_sections -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -f autopilot-proxmox/scripts/extract_utm_schema.py \
           autopilot-proxmox/tests/fixtures/utm_schema_contract_v4.json \
           autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): schema contract extractor for upstream drift detection"
```

(`-f` needed only if `tests/fixtures/` is gitignored; if not, the bare `git add` works.)

---

## Task 3: Dataclass tree

Define the dataclasses that mirror the UTM plist schema. No plist emission yet.

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing construction test**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
def test_bundle_spec_win11_template_has_four_drives():
    """Win11 ARM64 template bundle has: installer CD (USB), system qcow2
    (VirtIO), answer ISO CD (USB), virtio-win CD (USB) — in that order.
    Order matters: UTM assigns bootindex=N by drive-array position.
    Note: UTM's schema has no 'External' key — removable-ness is inferred
    from ImageType=CD at decode time."""
    from web import utm_bundle as ub
    spec = ub.BundleSpec(
        name="test-win11",
        uuid="11111111-1111-1111-1111-111111111111",
        system=ub.SystemSpec(),
        qemu=ub.QemuSpec(),
        drives=[
            ub.DriveSpec(identifier="AAAA0001-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="Win11_25H2_English_Arm64.iso"),
            ub.DriveSpec(identifier="AAAA0002-0000-0000-0000-000000000000",
                         image_type="Disk", interface="VirtIO",
                         image_name="AAAA0002-0000-0000-0000-000000000000.qcow2"),
            ub.DriveSpec(identifier="AAAA0003-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="AUTOUNATTEND.iso"),
            ub.DriveSpec(identifier="AAAA0004-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="virtio-win.iso"),
        ],
        display=ub.DisplaySpec(),
        network=ub.NetworkSpec(),
    )
    assert len(spec.drives) == 4
    assert spec.drives[0].image_name.endswith(".iso")
    assert spec.drives[1].image_type == "Disk"
    assert spec.drives[1].interface == "VirtIO"


def test_qemu_spec_defaults_for_windows():
    """Windows 11 ARM64 requires TPM and wants local-time RTC; UEFI boot."""
    from web import utm_bundle as ub
    q = ub.QemuSpec()
    assert q.uefi_boot is True
    assert q.tpm_device is True
    assert q.rtc_local_time is True
    assert q.rng_device is True
    assert q.balloon_device is False


def test_system_spec_defaults_are_arm64_virt_hvf():
    from web import utm_bundle as ub
    s = ub.SystemSpec()
    assert s.architecture == "aarch64"
    assert s.target == "virt"
    assert s.use_hypervisor is True
    assert s.memory_mib == 8192
    assert s.cpu_count == 4
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_bundle_spec_win11_template_has_four_drives \
                               tests/test_utm_bundle.py::test_qemu_spec_defaults_for_windows \
                               tests/test_utm_bundle.py::test_system_spec_defaults_are_arm64_virt_hvf -v
```

Expected: all three FAIL with `AttributeError: module 'web.utm_bundle' has no attribute 'BundleSpec'`.

- [ ] **Step 3: Add dataclasses to `utm_bundle.py`**

Edit `autopilot-proxmox/web/utm_bundle.py`, inserting immediately after the existing `UTM_CONFIGURATION_VERSION` constant:

```python
from dataclasses import dataclass, field


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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all previous tests PASS, three new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): dataclass tree for UTM config plist schema"
```

---

## Task 4: Plist renderer (key shape, no bundle writing yet)

Turn a `BundleSpec` into a Python dict with the exact PascalCase keys UTM expects, then `plistlib.dumps` to XML bytes. Every key emitted must exist in the contract (Task 2).

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing renderer tests**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
def _sample_win11_spec():
    """Stable sample spec used by the renderer and golden-fixture tests.
    A fixed MAC address keeps the golden bytes reproducible; drive
    identifiers are intentionally deterministic for the same reason."""
    from web import utm_bundle as ub
    return ub.BundleSpec(
        name="test-win11",
        uuid="11111111-1111-1111-1111-111111111111",
        system=ub.SystemSpec(),
        qemu=ub.QemuSpec(),
        drives=[
            ub.DriveSpec(identifier="aaaa0001-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="Win11_25H2_English_Arm64.iso"),
            ub.DriveSpec(identifier="aaaa0002-0000-0000-0000-000000000000",
                         image_type="Disk", interface="VirtIO",
                         image_name="aaaa0002-0000-0000-0000-000000000000.qcow2"),
            ub.DriveSpec(identifier="aaaa0003-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="AUTOUNATTEND.iso"),
            ub.DriveSpec(identifier="aaaa0004-0000-0000-0000-000000000000",
                         image_type="CD", interface="USB",
                         image_name="virtio-win.iso"),
        ],
        display=ub.DisplaySpec(),
        network=ub.NetworkSpec(mac_address="02:AA:BB:CC:DD:01"),
    )


def test_render_plist_has_required_top_level_keys():
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    for key in ("ConfigurationVersion", "Backend", "Information",
                "System", "QEMU", "Drive", "Display", "Network",
                "Input", "Sharing"):
        assert key in d, f"missing top-level key: {key}"
    assert d["ConfigurationVersion"] == 4
    assert d["Backend"] == "qemu"


def test_render_plist_uppercases_uuids():
    """UTM rejects mixed-case UUIDs; see commit 1eaa9d5."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert d["Information"]["UUID"] == "11111111-1111-1111-1111-111111111111".upper()
    for drive in d["Drive"]:
        assert drive["Identifier"] == drive["Identifier"].upper()


def test_render_plist_preserves_drive_order():
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert [dr["ImageName"] for dr in d["Drive"]] == [
        "Win11_25H2_English_Arm64.iso",
        "AAAA0002-0000-0000-0000-000000000000.qcow2",
        "AUTOUNATTEND.iso",
        "virtio-win.iso",
    ]


def test_render_plist_emits_win11_invariants():
    """Hypervisor lives under QEMU, not System — see UTM source
    Configuration/UTMQemuConfigurationQEMU.swift."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    assert d["System"]["Architecture"] == "aarch64"
    assert d["QEMU"]["Hypervisor"] is True
    assert d["QEMU"]["UEFIBoot"] is True
    assert d["QEMU"]["TPMDevice"] is True
    assert d["QEMU"]["RTCLocalTime"] is True


def test_render_plist_every_key_exists_in_contract():
    """Contract-based assertion — every section key we emit must appear in
    the extracted schema contract. If upstream UTM renames a key and we
    regenerate the contract, this catches any renderer drift. Note: this
    only catches *extra* keys; the E2E test catches missing required ones
    (UTM decode fails on register)."""
    from web import utm_bundle as ub
    d = ub.render_plist(_sample_win11_spec())
    contract = json.loads((FIXTURES / "utm_schema_contract_v4.json").read_text())

    def _check(section_name: str, obj: dict, allowed: set[str]):
        for emitted in obj.keys():
            assert emitted in allowed, \
                f"{section_name}: emitted key '{emitted}' not in UTM contract"

    _check("Information", d["Information"],  set(contract["sections"]["Information"]))
    _check("System",      d["System"],       set(contract["sections"]["System"]))
    _check("QEMU",        d["QEMU"],         set(contract["sections"]["QEMU"]))
    _check("Input",       d["Input"],        set(contract["sections"]["Input"]))
    _check("Sharing",     d["Sharing"],      set(contract["sections"]["Sharing"]))
    _check("Display",     d["Display"][0],   set(contract["sections"]["Display"]))
    _check("Network",     d["Network"][0],   set(contract["sections"]["Network"]))
    for drive in d["Drive"]:
        _check("Drive",   drive,             set(contract["sections"]["Drive"]))


def test_render_plist_returns_bytes_when_asked():
    """render_plist_bytes() returns a plistlib-formatted XML plist."""
    from web import utm_bundle as ub
    data = ub.render_plist_bytes(_sample_win11_spec())
    assert isinstance(data, bytes)
    assert data.startswith(b'<?xml')
    assert b'<plist version="1.0">' in data
    assert b'<key>ConfigurationVersion</key>' in data
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -k "render_plist" -v
```

Expected: all new tests FAIL with `AttributeError: module 'web.utm_bundle' has no attribute 'render_plist'`.

- [ ] **Step 3: Implement the renderer**

Add to `autopilot-proxmox/web/utm_bundle.py`:

```python
import plistlib
import random


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
        entry["ImageName"] = d.image_name
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
```

**Note on determinism for the golden test:** `_render_network` calls `_random_mac()` when no MAC is provided. For the golden fixture to reproduce byte-for-byte, test specs must either pass an explicit `mac_address` or the test seeds `random` before calling the renderer. The `_sample_win11_spec()` helper will be updated in Task 5 (Step 3) to pass a fixed MAC.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): render_plist() builds ConfigurationVersion 4 plist dict"
```

---

## Task 5: Golden plist fixture

Snapshot the exact bytes of a known-good Win11 template plist. Any future renderer change that shifts bytes fails this test; intentional changes re-generate.

**Files:**
- Create: `autopilot-proxmox/tests/fixtures/win11_template_expected.plist`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing golden comparison test**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
def test_render_plist_bytes_matches_golden_fixture():
    """Snapshot test — ensures we don't accidentally shift the plist bytes
    without noticing. Regenerate with:
        python -m web.utm_bundle _regenerate_golden_fixture
    (committed with a PR comment explaining the intentional change).
    """
    from web import utm_bundle as ub
    actual = ub.render_plist_bytes(_sample_win11_spec())
    expected = (FIXTURES / "win11_template_expected.plist").read_bytes()
    assert actual == expected, (
        "Rendered plist differs from golden fixture.\n"
        "If the change is intentional, regenerate the fixture via:\n"
        "    python -m web.utm_bundle _regenerate_golden_fixture\n"
        "and commit with a PR comment explaining why."
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_render_plist_bytes_matches_golden_fixture -v
```

Expected: FAIL with `FileNotFoundError: .../win11_template_expected.plist`.

- [ ] **Step 3: Add the regenerate-golden helper to `utm_bundle.py`**

Add a helper subcommand in `utm_bundle.py`. In the `main()` function, register the subcommand; add the implementation below `_cmd_build`:

```python
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
```

Add `import pathlib` near the top if not already present. Register the subcommand in `main()` by adding immediately before `args = parser.parse_args(argv)`:

```python
    regen = sub.add_parser("_regenerate_golden_fixture",
                           help="(dev) rewrite tests/fixtures/win11_template_expected.plist")
    regen.set_defaults(func=_cmd_regenerate_golden)
```

- [ ] **Step 4: Generate the golden fixture**

```bash
cd autopilot-proxmox && python -m web.utm_bundle _regenerate_golden_fixture
```

Expected: `wrote tests/fixtures/win11_template_expected.plist`.

- [ ] **Step 5: Inspect the fixture (sanity check)**

```bash
head -30 autopilot-proxmox/tests/fixtures/win11_template_expected.plist
```

Expected: starts with `<?xml version="1.0" ...` / `<!DOCTYPE plist ...` / `<plist version="1.0">` / `<dict>` / `<key>ConfigurationVersion</key>` / `<integer>4</integer>` / `<key>Backend</key>` / `<string>qemu</string>` / ...

- [ ] **Step 6: Run tests to verify PASS**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all tests PASS including the new golden test.

- [ ] **Step 7: Commit**

```bash
git add -f autopilot-proxmox/tests/fixtures/win11_template_expected.plist
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "test(utm): golden plist fixture for Win11 ARM64 template"
```

---

## Task 6: qemu-img wrapper

Create the system disk during bundle write. One function, one shell-out, one test.

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing test**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
def test_create_qcow2_writes_file_of_expected_size(tmp_path):
    """qemu-img create -f qcow2 <path> <size>G produces a qcow2 file. The
    file on disk is small (~200 KB) because qcow2 is sparse; the *virtual*
    size is what we assert."""
    from web import utm_bundle as ub
    disk = tmp_path / "test.qcow2"
    ub.create_qcow2(disk, virtual_size_gib=10)
    assert disk.is_file()
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(disk)],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(info.stdout)
    assert meta["virtual-size"] == 10 * 1024 ** 3
    assert meta["format"] == "qcow2"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_create_qcow2_writes_file_of_expected_size -v
```

Expected: FAIL with `AttributeError: module 'web.utm_bundle' has no attribute 'create_qcow2'`.

- [ ] **Step 3: Implement `create_qcow2`**

Add to `autopilot-proxmox/web/utm_bundle.py`:

```python
import subprocess


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
```

- [ ] **Step 4: Run test to verify PASS**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_create_qcow2_writes_file_of_expected_size -v
```

Expected: PASS. (Requires `qemu-img` on PATH — install via `brew install qemu` if missing.)

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): create_qcow2() wrapper for system disk creation"
```

---

## Task 7: Bundle writer

Lay out a complete `.utm` bundle directory: `config.plist`, `Data/<uuid>.qcow2`, copied ISOs, `efi_vars.fd`. Return a summary dict.

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing bundle-writer tests**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
def _touch(path, size=1024):
    """Create a dummy file of the given byte size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * size)


def test_write_bundle_creates_expected_layout(tmp_path):
    from web import utm_bundle as ub

    # Stage fake ISOs and an efi_vars.fd source
    iso_dir = tmp_path / "isos"
    installer_iso = iso_dir / "Win11.iso"
    answer_iso    = iso_dir / "AUTOUNATTEND.iso"
    virtio_iso    = iso_dir / "virtio-win.iso"
    _touch(installer_iso)
    _touch(answer_iso)
    _touch(virtio_iso)
    efi_src = tmp_path / "efi-source.fd"
    _touch(efi_src)

    spec = _sample_win11_spec()
    bundle = tmp_path / "test-win11.utm"

    result = ub.write_bundle(
        spec,
        bundle_path=bundle,
        disk_size_gib=10,
        efi_vars_source=efi_src,
        iso_sources={
            "Win11_25H2_English_Arm64.iso": installer_iso,
            "AUTOUNATTEND.iso":             answer_iso,
            "virtio-win.iso":               virtio_iso,
        },
    )

    assert (bundle / "config.plist").is_file()
    assert (bundle / "Data").is_dir()
    assert (bundle / "Data" / "Win11_25H2_English_Arm64.iso").is_file()
    assert (bundle / "Data" / "AUTOUNATTEND.iso").is_file()
    assert (bundle / "Data" / "virtio-win.iso").is_file()
    assert (bundle / "Data" / "efi_vars.fd").is_file()
    # System disk uses the VirtIO drive's identifier as filename
    disk_filename = spec.drives[1].identifier.upper() + ".qcow2"
    assert (bundle / "Data" / disk_filename).is_file()

    # Return summary
    assert result["uuid"] == spec.uuid.upper()
    assert pathlib.Path(result["bundle_path"]) == bundle
    assert set(result["drive_uuids"]) == {d.identifier.upper() for d in spec.drives}


def test_write_bundle_plist_matches_renderer(tmp_path):
    """Bytes written to config.plist match render_plist_bytes exactly."""
    from web import utm_bundle as ub
    efi_src = tmp_path / "efi.fd"; _touch(efi_src)
    # minimal spec — no ISOs referenced, drives still listed
    spec = _sample_win11_spec()
    bundle = tmp_path / "b.utm"
    ub.write_bundle(spec, bundle_path=bundle, disk_size_gib=10,
                    efi_vars_source=efi_src, iso_sources={})
    assert (bundle / "config.plist").read_bytes() == ub.render_plist_bytes(spec)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -k "write_bundle" -v
```

Expected: FAIL with `AttributeError: ... 'write_bundle'`.

- [ ] **Step 3: Implement `write_bundle`**

Add to `autopilot-proxmox/web/utm_bundle.py`:

```python
import shutil


def write_bundle(
    spec: BundleSpec,
    bundle_path: pathlib.Path,
    disk_size_gib: int,
    efi_vars_source: pathlib.Path,
    iso_sources: dict[str, pathlib.Path],
    qemu_img: str = "qemu-img",
) -> dict:
    """Create a fully-populated .utm bundle directory.

    Args:
        spec: the BundleSpec describing the VM.
        bundle_path: absolute path to the .utm directory to create.
        disk_size_gib: virtual size of the system qcow2.
        efi_vars_source: file to copy into Data/efi_vars.fd (unmodified at
            this stage; Task 13 will optionally rewrite this).
        iso_sources: maps the drive's image_name (e.g. "Win11.iso") to the
            file on disk to copy into Data/. Drives whose image_name is
            None or missing from this map are skipped — useful for the
            system disk (no ISO) and during tests.
        qemu_img: qemu-img binary path.

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

    # 3. EFI vars
    shutil.copyfile(efi_vars_source, data_dir / "efi_vars.fd")

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
```

- [ ] **Step 4: Run tests to verify PASS**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): write_bundle() lays out .utm directory deterministically"
```

---

## Task 8: UtmctlClient wrapper

Thin wrapper around `utmctl` for `register`, `start`, `stop`, `status`, `exec`, `delete`. Tests mock `subprocess` so they don't need UTM running.

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing wrapper tests**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
from unittest.mock import patch, MagicMock


def test_utmctl_register_returns_uuid_from_stdout():
    """`utmctl register <bundle>` prints the registered VM's UUID."""
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="AAAA1111-2222-3333-4444-555555555555\n",
                     stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake) as run:
        client = ub.UtmctlClient(utmctl="/Applications/UTM.app/Contents/MacOS/utmctl")
        uuid = client.register(pathlib.Path("/tmp/x.utm"))
    run.assert_called_once()
    args, _ = run.call_args
    assert args[0] == ["/Applications/UTM.app/Contents/MacOS/utmctl",
                       "register", "/tmp/x.utm"]
    assert uuid == "AAAA1111-2222-3333-4444-555555555555"


def test_utmctl_start_invokes_start_subcommand():
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake) as run:
        ub.UtmctlClient().start("AAAA1111-2222-3333-4444-555555555555")
    args, _ = run.call_args
    assert args[0][-2:] == ["start", "AAAA1111-2222-3333-4444-555555555555"]


def test_utmctl_status_returns_state_string():
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="started\n", stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake):
        state = ub.UtmctlClient().status("AAAA1111-2222-3333-4444-555555555555")
    assert state == "started"


def test_utmctl_delete_invokes_delete_subcommand():
    from web import utm_bundle as ub
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("web.utm_bundle.subprocess.run", return_value=fake) as run:
        ub.UtmctlClient().delete("AAAA1111-2222-3333-4444-555555555555")
    args, _ = run.call_args
    assert "delete" in args[0]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -k "utmctl" -v
```

Expected: FAIL with `AttributeError: ... 'UtmctlClient'`.

- [ ] **Step 3: Implement `UtmctlClient`**

Add to `autopilot-proxmox/web/utm_bundle.py`:

```python
DEFAULT_UTMCTL = "/Applications/UTM.app/Contents/MacOS/utmctl"


class UtmctlClient:
    """Thin subprocess wrapper around UTM's utmctl CLI."""

    def __init__(self, utmctl: str = DEFAULT_UTMCTL) -> None:
        self.utmctl = utmctl

    def _run(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.utmctl, *args],
            input=input_text, capture_output=True, text=True, check=True,
        )

    def register(self, bundle_path: pathlib.Path) -> str:
        """Register a .utm bundle with UTM and return its assigned UUID."""
        result = self._run("register", str(bundle_path))
        return result.stdout.strip()

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
```

- [ ] **Step 4: Run tests to verify PASS**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): UtmctlClient wraps utmctl register/start/stop/exec/delete"
```

---

## Task 9: Promote `build` CLI to real bundle-writing

Replace the echo stub in `_cmd_build` with real `write_bundle` + optional `utmctl register`.

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py`
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Write failing end-to-end CLI test**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

```python
def test_cli_build_writes_bundle(tmp_path):
    """Feed a full spec JSON to the CLI; bundle directory and files exist."""
    efi_src = tmp_path / "efi.fd"; _touch(efi_src)
    installer = tmp_path / "Win11.iso"; _touch(installer)
    spec_payload = {
        "name": "test-cli",
        "uuid": "22222222-2222-2222-2222-222222222222",
        "system": {},
        "qemu": {},
        "display": {},
        "network": {},
        "drives": [
            {"identifier": "aaaa0001-0000-0000-0000-000000000000",
             "image_type": "CD", "interface": "USB",
             "image_name": "Win11.iso"},
            {"identifier": "aaaa0002-0000-0000-0000-000000000000",
             "image_type": "Disk", "interface": "VirtIO",
             "image_name": "aaaa0002-0000-0000-0000-000000000000.qcow2"},
        ],
        "disk_size_gib": 5,
        "efi_vars_source": str(efi_src),
        "iso_sources": {"Win11.iso": str(installer)},
        "register": False,  # don't hit real UTM
    }
    bundle = tmp_path / "test-cli.utm"
    result = subprocess.run(
        [sys.executable, "-m", "web.utm_bundle", "build",
         "--spec", "-", "--out", str(bundle)],
        input=json.dumps(spec_payload),
        capture_output=True, text=True, check=True,
    )
    out = json.loads(result.stdout)
    assert out["uuid"] == "22222222-2222-2222-2222-222222222222"
    assert (bundle / "config.plist").is_file()
    assert (bundle / "Data" / "Win11.iso").is_file()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_cli_build_writes_bundle -v
```

Expected: FAIL — the CLI still echoes and doesn't create a bundle.

- [ ] **Step 3: Replace `_cmd_build` implementation**

Replace the existing `_cmd_build` in `autopilot-proxmox/web/utm_bundle.py` with:

```python
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
    raw = sys.stdin.read() if args.spec == "-" else open(args.spec).read()
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
    )

    if payload.get("register"):
        client = UtmctlClient()
        assigned = client.register(pathlib.Path(args.out))
        result["registered_uuid"] = assigned

    json.dump(result, sys.stdout)
    return 0
```

Add `"efi_vars_source"` as a required payload field by deleting it from `payload.get(...)` only if absent — the code above uses direct subscript which raises `KeyError` on missing. That's the desired behaviour: Ansible callers MUST pass a path.

- [ ] **Step 4: Delete the obsolete Task 1 echo test**

The new `_cmd_build` requires `drives`, `efi_vars_source`, etc. in the payload, so `test_cli_build_echoes_spec_on_stdout` will fail with KeyError. Delete that test from `autopilot-proxmox/tests/test_utm_bundle.py` — it's been superseded by `test_cli_build_writes_bundle`.

- [ ] **Step 5: Run tests to verify PASS**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): CLI build writes bundle and optionally registers with utmctl"
```

---

## Task 10: Schema drift CI job (Tier 1)

Shell script that diffs UTM's `Configuration/` directory against a pinned known-good sha and reports changes. Not a blocking CI check — a warning-level signal.

**Files:**
- Create: `autopilot-proxmox/scripts/check_utm_schema_drift.sh`
- Create: `autopilot-proxmox/web/utm_schema_known_good.txt`

- [ ] **Step 1: Pin the current UTM sha**

```bash
cd /Users/Adam.Gell/src/UTM && git rev-parse origin/main > /tmp/utm_main_sha.txt && cat /tmp/utm_main_sha.txt
```

Expected: a 40-character SHA. Copy the value; it becomes the initial known-good pin.

- [ ] **Step 2: Write the pin file**

```bash
echo "<SHA_FROM_STEP_1>" > /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/web/utm_schema_known_good.txt
```

Replace `<SHA_FROM_STEP_1>` with the actual SHA. The file is a single line ending in newline.

- [ ] **Step 3: Write the drift-check script**

Create `autopilot-proxmox/scripts/check_utm_schema_drift.sh`:

```bash
#!/usr/bin/env bash
# Diff UTM's configuration schema (Swift sources) against a pinned
# known-good sha. Warning-level CI job — non-zero exit iff drift exists,
# but the calling CI job runs in "continue-on-error" mode.
#
# Usage:
#   scripts/check_utm_schema_drift.sh [utm-source-dir]
#
# Defaults utm-source-dir to $UTM_SOURCE or ~/src/UTM.

set -euo pipefail

UTM_SRC="${1:-${UTM_SOURCE:-$HOME/src/UTM}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIN_FILE="$SCRIPT_DIR/../web/utm_schema_known_good.txt"

if [[ ! -d "$UTM_SRC/Configuration" ]]; then
    echo "ERROR: $UTM_SRC does not look like a UTM checkout (no Configuration/ dir)" >&2
    exit 2
fi
if [[ ! -f "$PIN_FILE" ]]; then
    echo "ERROR: pin file missing: $PIN_FILE" >&2
    exit 2
fi

PIN="$(tr -d '[:space:]' < "$PIN_FILE")"

echo "Fetching UTM upstream…"
git -C "$UTM_SRC" fetch --quiet origin main

SCHEMA_PATHS=(
    "Configuration/UTMQemuConfiguration*.swift"
    "Configuration/QEMUConstant.swift"
    "Configuration/QEMUConstantGenerated.swift"
)

echo "Pinned SHA:  $PIN"
echo "Upstream:    $(git -C "$UTM_SRC" rev-parse origin/main)"
echo

if git -C "$UTM_SRC" diff --quiet "$PIN..origin/main" -- "${SCHEMA_PATHS[@]}"; then
    echo "✓ No schema drift since pinned sha."
    exit 0
fi

echo "⚠ UTM schema files changed upstream since the pinned sha:"
echo
git -C "$UTM_SRC" diff --stat "$PIN..origin/main" -- "${SCHEMA_PATHS[@]}"
echo
echo "Review the diff, decide whether we need to bump the renderer or the contract,"
echo "then update $PIN_FILE with the new sha:"
echo "  git -C $UTM_SRC rev-parse origin/main > $PIN_FILE"
exit 1
```

Make it executable:

```bash
chmod +x autopilot-proxmox/scripts/check_utm_schema_drift.sh
```

- [ ] **Step 4: Run it once to verify it passes against the pinned sha**

```bash
autopilot-proxmox/scripts/check_utm_schema_drift.sh /Users/Adam.Gell/src/UTM
```

Expected: `✓ No schema drift since pinned sha.` and exit 0.

- [ ] **Step 5: Commit**

```bash
git add -f autopilot-proxmox/scripts/check_utm_schema_drift.sh \
           autopilot-proxmox/web/utm_schema_known_good.txt
git commit -m "feat(utm): schema drift notification script (Tier 1)"
```

---

## Task 11: Add `virt-firmware` to requirements

Install the package now so Task 13's experiment has the tool available.

**Files:**
- Modify: `autopilot-proxmox/requirements.txt`

- [ ] **Step 1: Add the dependency line**

Edit `autopilot-proxmox/requirements.txt` by appending at the end:

```
virt-firmware>=24.10,<27
```

- [ ] **Step 2: Install it**

```bash
cd autopilot-proxmox && pip install -r requirements.txt
```

Expected: `virt-firmware` gets installed. Verify with:

```bash
which virt-fw-vars && virt-fw-vars --help | head -10
```

Expected: `virt-fw-vars` is on PATH and prints usage including a `--input` flag.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/requirements.txt
git commit -m "chore(utm): add virt-firmware for EFI variable manipulation"
```

---

## Task 12: Ansible `render_bundle.yml` and main.yml cutover

Replace `create_bundle.yml` + `customize_plist.yml` in the role's `main.yml` with a single `render_bundle.yml` that calls the Python CLI.

**Files:**
- Create: `autopilot-proxmox/roles/utm_template_builder/tasks/render_bundle.yml`
- Modify: `autopilot-proxmox/roles/utm_template_builder/tasks/main.yml`

- [ ] **Step 1: Write `render_bundle.yml`**

Create `autopilot-proxmox/roles/utm_template_builder/tasks/render_bundle.yml`:

```yaml
---
# utm_template_builder/tasks/render_bundle.yml
#
# Writes a .utm bundle from scratch via web.utm_bundle CLI, then registers
# it with UTM. Replaces create_bundle.yml + customize_plist.yml.
#
# Required facts (set by the calling playbook / parent role):
#   vm_name            — UTM display name and bundle directory stem
#   utm_iso_name       — installer ISO filename inside utm_iso_dir
#   utm_iso_dir        — directory containing installer ISOs
#   utm_documents_dir  — sandbox Documents dir where UTM stores bundles
#   utm_answer_iso_path — absolute path to AUTOUNATTEND.iso (from utm_answer_iso role)
#   utm_virtio_win_iso_name — filename of virtio-win ISO
#   utm_app_path       — UTM.app bundle path (default /Applications/UTM.app)
#   vm_cpu_cores, vm_memory_mb, vm_disk_gb — sizing
#
# Exports facts for downstream tasks:
#   _bundle_uuid, _bundle_dest, _plist_path, _data_dir, _cd_uuid, _disk_uuid,
#   _disk_filename, _answer_cd_uuid, _virtio_cd_uuid

- name: "Render bundle: resolve paths and generate UUIDs"
  ansible.builtin.set_fact:
    _bundle_dest: "{{ utm_documents_dir }}/{{ vm_name }}.utm"
    # macOS `uuidgen` produces uppercase UUIDs — feeding them straight into
    # the spec is correct (UTM requires uppercase Identifier / UUID fields).
    _vm_uuid:            "{{ lookup('pipe', 'uuidgen') }}"
    _cd_uuid_gen:        "{{ lookup('pipe', 'uuidgen') }}"
    _disk_uuid_gen:      "{{ lookup('pipe', 'uuidgen') }}"
    _answer_cd_uuid_gen: "{{ lookup('pipe', 'uuidgen') }}"
    _virtio_cd_uuid_gen: "{{ lookup('pipe', 'uuidgen') }}"

- name: "Render bundle: build spec payload"
  ansible.builtin.set_fact:
    _bundle_spec:
      name: "{{ vm_name }}"
      uuid: "{{ _vm_uuid }}"
      system:
        memory_mib: "{{ vm_memory_mb | int }}"
        cpu_count:  "{{ vm_cpu_cores | int }}"
      qemu: {}
      display: {}
      network: {}
      drives:
        - identifier: "{{ _cd_uuid_gen }}"
          image_type: "CD"
          interface:  "USB"
          image_name: "{{ utm_iso_name }}"
        - identifier: "{{ _disk_uuid_gen }}"
          image_type: "Disk"
          interface:  "VirtIO"
          image_name: "{{ _disk_uuid_gen }}.qcow2"
        - identifier: "{{ _answer_cd_uuid_gen }}"
          image_type: "CD"
          interface:  "USB"
          image_name: "AUTOUNATTEND.iso"
        - identifier: "{{ _virtio_cd_uuid_gen }}"
          image_type: "CD"
          interface:  "USB"
          image_name: "{{ utm_virtio_win_iso_name | default('virtio-win.iso') }}"
      disk_size_gib: "{{ vm_disk_gb | int }}"
      efi_vars_source: >-
        {{ utm_app_path | default('/Applications/UTM.app') }}/Contents/Resources/qemu/edk2-arm-secure-vars.fd
      iso_sources:
        "{{ utm_iso_name }}": "{{ utm_iso_dir }}/{{ utm_iso_name }}"
        "AUTOUNATTEND.iso":   "{{ utm_answer_iso_path }}"
        "{{ utm_virtio_win_iso_name | default('virtio-win.iso') }}": >-
          {{ utm_iso_dir }}/{{ utm_virtio_win_iso_name | default('virtio-win.iso') }}
      register: true

- name: "Render bundle: check destination bundle existence"
  ansible.builtin.stat:
    path: "{{ _bundle_dest }}"
  register: _bundle_dest_stat

- name: "Render bundle: fail if bundle exists and utm_overwrite_bundle is false"
  ansible.builtin.fail:
    msg: >-
      Bundle {{ _bundle_dest }} already exists. Delete it via UTM
      (right-click → Delete) or set utm_overwrite_bundle=true.
  when:
    - _bundle_dest_stat.stat.exists
    - not (utm_overwrite_bundle | default(false) | bool)

- name: "Render bundle: delete existing VM via utmctl"
  ansible.builtin.command:
    argv:
      - "{{ utm_app_path | default('/Applications/UTM.app') }}/Contents/MacOS/utmctl"
      - delete
      - "{{ vm_name }}"
  register: _utmctl_delete
  failed_when: false
  changed_when: _utmctl_delete.rc == 0
  when:
    - _bundle_dest_stat.stat.exists
    - utm_overwrite_bundle | default(false) | bool

- name: "Render bundle: remove stale bundle dir"
  ansible.builtin.file:
    path: "{{ _bundle_dest }}"
    state: absent
  when:
    - _bundle_dest_stat.stat.exists
    - utm_overwrite_bundle | default(false) | bool

- name: "Render bundle: invoke utm_bundle CLI"
  ansible.builtin.command:
    argv:
      - python
      - -m
      - web.utm_bundle
      - build
      - --spec
      - "-"
      - --out
      - "{{ _bundle_dest }}"
    chdir: "{{ playbook_dir }}/.."
    stdin: "{{ _bundle_spec | to_json }}"
  register: _utm_build_result
  changed_when: true

- name: "Render bundle: parse CLI JSON output"
  ansible.builtin.set_fact:
    _utm_build_json: "{{ _utm_build_result.stdout | from_json }}"

- name: "Render bundle: export facts for downstream tasks"
  ansible.builtin.set_fact:
    _bundle_uuid:     "{{ _utm_build_json.registered_uuid | default(_utm_build_json.uuid) }}"
    _plist_path:      "{{ _bundle_dest }}/config.plist"
    _data_dir:        "{{ _bundle_dest }}/Data"
    _cd_uuid:         "{{ (_cd_uuid_gen | upper) }}"
    _disk_uuid:       "{{ (_disk_uuid_gen | upper) }}"
    _disk_filename:   "{{ (_disk_uuid_gen | upper) }}.qcow2"
    _answer_cd_uuid:  "{{ (_answer_cd_uuid_gen | upper) }}"
    _virtio_cd_uuid:  "{{ (_virtio_cd_uuid_gen | upper) }}"
```

- [ ] **Step 2: Cut `main.yml` over**

Edit `autopilot-proxmox/roles/utm_template_builder/tasks/main.yml`. Replace the "Phase 1: build and start" section (the two `include_tasks` lines for `create_bundle.yml` and `customize_plist.yml`) with a single `render_bundle.yml` include. The final section becomes:

```yaml
# ── Phase 1: build and start ─────────────────────────────────────────────────

- name: "UTM template builder: render bundle and register with UTM"
  ansible.builtin.include_tasks: render_bundle.yml
  when: not (utm_build_resume | default(false) | bool)

- name: "UTM template builder: start VM and wait for operator (manual mode)"
  ansible.builtin.include_tasks: start_and_wait.yml
  when: not (utm_build_resume | default(false) | bool)

# ── Phase 2: sysprep and finalise ────────────────────────────────────────────

- name: "UTM template builder: sysprep and finalise template"
  ansible.builtin.include_tasks: sysprep_finalize.yml
  when: utm_build_resume | default(false) | bool
```

Leave `create_bundle.yml` and `customize_plist.yml` on disk for now — Task 15 deletes them after the E2E run proves itself.

- [ ] **Step 3: Ansible syntax check**

```bash
cd autopilot-proxmox && ansible-playbook --syntax-check playbooks/utm_build_win11_template.yml
```

Expected: `playbook: playbooks/utm_build_win11_template.yml` with no errors.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/roles/utm_template_builder/tasks/render_bundle.yml \
        autopilot-proxmox/roles/utm_template_builder/tasks/main.yml
git commit -m "feat(utm): render_bundle.yml replaces create_bundle + customize_plist"
```

---

## Task 13: DriverPaths in `unattend.xml.j2`

Add virtio driver paths so Windows Setup auto-loads the virtio-blk driver instead of asking for one at the disk picker.

**Files:**
- Modify: `autopilot-proxmox/roles/utm_answer_iso/templates/unattend.xml.j2`

- [ ] **Step 1: Inspect the current `windowsPE` pass**

```bash
grep -n "windowsPE\|DriverPaths\|Microsoft-Windows-PnpCustomizationsWinPE" \
    autopilot-proxmox/roles/utm_answer_iso/templates/unattend.xml.j2
```

Expected: a `<settings pass="windowsPE">` block exists. No `DriverPaths` or `PnpCustomizationsWinPE` currently.

- [ ] **Step 2: Add the PnpCustomizationsWinPE component**

Edit `autopilot-proxmox/roles/utm_answer_iso/templates/unattend.xml.j2`. Inside the existing `<settings pass="windowsPE">` block, add a sibling `<component>` element alongside whatever is already there (typically `Microsoft-Windows-International-Core-WinPE` and `Microsoft-Windows-Setup`):

```xml
        <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="arm64"
                   publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"
                   xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <DriverPaths>
                <PathAndCredentials wcm:action="add" wcm:keyValue="1">
                    <Path>D:\viostor\w11\ARM64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="2">
                    <Path>E:\viostor\w11\ARM64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="3">
                    <Path>F:\viostor\w11\ARM64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="4">
                    <Path>D:\NetKVM\w11\ARM64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="5">
                    <Path>E:\NetKVM\w11\ARM64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="6">
                    <Path>F:\NetKVM\w11\ARM64</Path>
                </PathAndCredentials>
            </DriverPaths>
        </component>
```

- [ ] **Step 3: Render-check the template manually**

```bash
cd autopilot-proxmox && python -c "
import jinja2, pathlib
tpl = jinja2.Environment(loader=jinja2.FileSystemLoader('roles/utm_answer_iso/templates')).get_template('unattend.xml.j2')
rendered = tpl.render(
    utm_answer_admin_user='Administrator',
    utm_answer_admin_pass='P@ssw0rd!',
    utm_answer_locale='en-US',
    utm_answer_timezone='Pacific Standard Time',
    utm_answer_windows_edition='Windows 11 Pro',
    vm_name='test',
    _oem_profile={'manufacturer': 'Acme', 'organization': 'Acme Org'},
)
assert '<DriverPaths>' in rendered, 'DriverPaths missing from render'
assert 'viostor\\\\w11\\\\ARM64' in rendered.replace('\\\\\\\\', '\\\\'), 'viostor path missing'
print('OK — DriverPaths block rendered correctly')
"
```

Expected: `OK — DriverPaths block rendered correctly`. If the render fails due to undefined variables, the command's `render(**kwargs)` call needs an extra kwarg — the existing template may consume other fields. Copy the failing variable name into the render call and retry.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/roles/utm_answer_iso/templates/unattend.xml.j2
git commit -m "feat(utm): DriverPaths in unattend.xml for unattended virtio load"
```

---

## Task 14: First Phase A E2E build

Run the full playbook against a real Windows 11 ARM64 ISO to confirm the new path produces a working template. This is where the keystroke hack still runs (the experiment in Task 15 will attempt to delete it).

**Files:** (no code changes — this is a validation run)

- [ ] **Step 1: Preflight checks**

```bash
# UTM sandbox Documents dir must exist
test -d ~/Library/Containers/com.utmapp.UTM/Data/Documents && \
    echo "UTM Documents dir exists" || \
    mkdir -p ~/Library/Containers/com.utmapp.UTM/Data/Documents

# UTM-shipped secure-boot EFI vars source file must exist
test -f "/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd" && \
    echo "Secure-boot EFI vars present"

# utm_iso_dir should contain the Win11 installer ISO and virtio-win ISO.
# The AUTOUNATTEND ISO is generated by the utm_answer_iso role at run time
# and does NOT need to be pre-placed.
UTM_ISO_DIR="${UTM_ISO_DIR:-$HOME/Library/Containers/com.utmapp.UTM/Data/Documents}"
test -f "$UTM_ISO_DIR/Win11_25H2_English_Arm64.iso" && echo "Win11 ISO present" || \
    echo "MISSING: $UTM_ISO_DIR/Win11_25H2_English_Arm64.iso"
test -f "$UTM_ISO_DIR/virtio-win.iso" && echo "virtio-win ISO present" || \
    echo "MISSING: $UTM_ISO_DIR/virtio-win.iso (download from fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/)"
```

All five lines should print success. If any "MISSING" appears, obtain the ISO and place it before continuing.

- [ ] **Step 2: Ensure no stale VM exists**

```bash
/Applications/UTM.app/Contents/MacOS/utmctl list | grep -i 'test-win11-new' || echo "clean"
```

Expected: `clean`. If a VM named `test-win11-new` exists, delete it in UTM first.

- [ ] **Step 3: Run the playbook**

```bash
cd autopilot-proxmox && ansible-playbook playbooks/utm_build_win11_template.yml \
    -e vm_name=test-win11-new \
    -e utm_iso_name=Win11_25H2_English_Arm64.iso \
    -e utm_documents_dir="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents" \
    -e @inventory/group_vars/all/vault.yml \
    -e utm_overwrite_bundle=true
```

Expected timeline:
- Bundle render: ~10 s
- `utmctl register`: ~5 s
- VM start: immediate; UTM window opens
- EFI shell drop + keystroke: ~15 s (current hack runs)
- Windows Setup: ~20-30 minutes, fully unattended (no disk-picker click thanks to Task 13 DriverPaths)
- `firstboot.ps1` writes sentinel: ~3 minutes after OOBE completes
- Playbook suspends VM + writes marker file: ~20 s

Total: ~30-40 minutes. Playbook reports `PLAY RECAP` with failed=0.

- [ ] **Step 4: Confirm bundle on disk**

```bash
ls -la ~/Library/Containers/com.utmapp.UTM/Data/Documents/test-win11-new.utm/Data/
test -f ~/Library/Containers/com.utmapp.UTM/Data/Documents/test-win11-new.utm/autopilot-template.ready && \
    echo "Template ready marker present"
```

Expected: `config.plist`, `efi_vars.fd`, `Win11_25H2_English_Arm64.iso`, `AUTOUNATTEND.iso`, `virtio-win.iso`, plus a `.qcow2` file named after the system disk UUID. Marker file present.

- [ ] **Step 5: Confirm UTM sees the bundle**

```bash
/Applications/UTM.app/Contents/MacOS/utmctl list | grep test-win11-new
```

Expected: one row with the VM's UUID and state `suspended` (or `stopped`).

- [ ] **Step 6: Record outcome in the spec's risk log**

No code change; just a quick mental note: did the driver auto-load? did EFI shell still drop? Those answers shape Task 15 and Task 16.

---

## Task 15: EFI-vars experiment (time-boxed, 1 working day)

Attempt to eliminate the osascript keystroke hack by pre-populating `efi_vars.fd` with a `Boot0000` entry + `BootOrder`. Time box: **8 working hours**. If you cross the budget without a clean boot, abandon and move to Task 16.

**Files:**
- Modify: `autopilot-proxmox/web/utm_bundle.py` (add `build_efi_vars`)
- Modify: `autopilot-proxmox/roles/utm_template_builder/tasks/render_bundle.yml` (call it)
- Test: `autopilot-proxmox/tests/test_utm_bundle.py`

- [ ] **Step 1: Study the `virt-fw-vars` CLI**

```bash
virt-fw-vars --help
virt-fw-vars --input /Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd --print | head -40
```

Expected: the `--print` output shows the variables currently in the file (GlobalVariable BootOrder may be absent; EFI_SECURE_BOOT, PK, KEK, db entries present).

- [ ] **Step 2: Try adding a BootOrder + Boot0000 entry via CLI first (faster iteration)**

Construct the command referring to virt-fw-vars docs — the likely shape is:

```bash
cp /Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd /tmp/efi_vars_test.fd

virt-fw-vars --input /tmp/efi_vars_test.fd --output /tmp/efi_vars_test.fd \
    --set-boot-entry 0000 \
    --set-boot-description "Windows Setup USB" \
    --set-boot-device "PciRoot(0x0)/Pci(0x2,0x0)/USB(0x0,0x0)/CDROM(0x0,0x0,0x0)" \
    --set-boot-order 0000 2>&1 | head -40
```

(Exact flag names depend on the installed `virt-firmware` version. If the flags above don't exist, run `virt-fw-vars --help` and adapt; the package exposes equivalents.)

Record the exact command that produces a valid output.

- [ ] **Step 3: Boot-test the modified vars file manually**

Copy the modified `/tmp/efi_vars_test.fd` onto the existing `test-win11-new.utm` bundle (kill the VM in UTM first):

```bash
/Applications/UTM.app/Contents/MacOS/utmctl stop test-win11-new 2>/dev/null || true
cp /tmp/efi_vars_test.fd ~/Library/Containers/com.utmapp.UTM/Data/Documents/test-win11-new.utm/Data/efi_vars.fd
/Applications/UTM.app/Contents/MacOS/utmctl start test-win11-new
```

Watch UTM. Expected outcomes:
- **Success:** VM boots straight to Windows Setup (or to the "Press any key to boot from CD" prompt, which DriverPaths wipes in ~5 s), no EFI shell drop.
- **Partial:** VM drops to EFI shell but our Boot0000 entry is in the menu — try setting `Timeout` UEFI var to a higher value and retry.
- **Failure:** Same EFI shell drop as before — the boot entry's device path is wrong for this QEMU machine topology.

If success: proceed to Step 4. If partial: adjust (e.g. `--set-var Timeout,...,5000`) and re-test. If failure after 2-3 iterations or the budget is blown: abandon, skip to Task 16.

- [ ] **Step 4: Write failing integration test**

Append to `autopilot-proxmox/tests/test_utm_bundle.py`:

Add `import pytest` near the top of `autopilot-proxmox/tests/test_utm_bundle.py` if it's not already present (earlier tests use the `tmp_path` fixture without needing the module, but `pytest.skip` requires the import).

```python
def test_build_efi_vars_adds_boot_entry(tmp_path):
    """build_efi_vars() consumes the UTM-shipped secure-boot vars file and
    produces a file with at least one BootXXXX variable added."""
    from web import utm_bundle as ub
    utm_resources = pathlib.Path("/Applications/UTM.app/Contents/Resources/qemu")
    src = utm_resources / "edk2-arm-secure-vars.fd"
    if not src.is_file():
        pytest.skip("UTM.app not installed; EFI vars experiment requires it")
    dest = tmp_path / "efi_vars.fd"
    ub.build_efi_vars(source=src, dest=dest,
                      usb_cd_description="Test Installer")
    assert dest.is_file()
    assert dest.stat().st_size == src.stat().st_size  # size is fixed
    # Spot-check: expect the text "Test Installer" appears as UCS-2 bytes somewhere
    needle = "Test Installer".encode("utf-16-le")
    assert needle in dest.read_bytes()
```

- [ ] **Step 5: Run test to verify fail**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py::test_build_efi_vars_adds_boot_entry -v
```

Expected: FAIL with `AttributeError: ... 'build_efi_vars'`.

- [ ] **Step 6: Implement `build_efi_vars`**

Add to `autopilot-proxmox/web/utm_bundle.py`, translating the exact CLI from Step 2 into a subprocess call (or, if the Python `virt.firmware` library surface is ergonomic, an in-process call). Example subprocess shape:

```python
def build_efi_vars(
    source: pathlib.Path,
    dest: pathlib.Path,
    usb_cd_description: str = "Windows Installer",
    usb_cd_device_path: str = "PciRoot(0x0)/Pci(0x2,0x0)/USB(0x0,0x0)/CDROM(0x0,0x0,0x0)",
) -> None:
    """Copy `source` to `dest` and inject a Boot0000 entry + BootOrder so
    EDK2 AAVMF boots the USB CD without dropping to the EFI shell.

    The exact `virt-fw-vars` invocation was determined empirically; see the
    comments in this function for the reasoning. If you bump the virt-firmware
    dependency, re-validate against a fresh template build.
    """
    shutil.copyfile(source, dest)
    subprocess.run(
        ["virt-fw-vars",
         "--input",  str(dest),
         "--output", str(dest),
         "--set-boot-entry",       "0000",
         "--set-boot-description", usb_cd_description,
         "--set-boot-device",      usb_cd_device_path,
         "--set-boot-order",       "0000"],
        check=True, capture_output=True, text=True,
    )
```

Replace the flag names in the subprocess call with the ones that actually worked in Step 2.

- [ ] **Step 7: Wire into `write_bundle`**

Add a named keyword arg `inject_boot_entry: bool = True` to `write_bundle`'s signature (after `qemu_img`):

```python
def write_bundle(
    spec: BundleSpec,
    bundle_path: pathlib.Path,
    disk_size_gib: int,
    efi_vars_source: pathlib.Path,
    iso_sources: dict[str, pathlib.Path],
    qemu_img: str = "qemu-img",
    inject_boot_entry: bool = True,
) -> dict:
```

Replace the unconditional `shutil.copyfile(efi_vars_source, data_dir / "efi_vars.fd")` call with:

```python
    # 3. EFI vars — inject a USB-CD boot entry so EDK2 doesn't drop to shell
    if inject_boot_entry:
        build_efi_vars(source=efi_vars_source, dest=data_dir / "efi_vars.fd")
    else:
        shutil.copyfile(efi_vars_source, data_dir / "efi_vars.fd")
```

Thread the flag through `_cmd_build`:

```python
    result = write_bundle(
        spec,
        bundle_path=pathlib.Path(args.out),
        disk_size_gib=int(payload.get("disk_size_gib", 80)),
        efi_vars_source=pathlib.Path(payload["efi_vars_source"]),
        iso_sources=iso_sources,
        inject_boot_entry=bool(payload.get("inject_boot_entry", True)),
    )
```

The existing unit tests in Tasks 6 and 7 passed `inject_boot_entry` nowhere; default `True` means they'd now try to invoke `virt-fw-vars` against a dummy 1 KB `efi_vars.fd` and fail. Update the two `write_bundle` tests in Task 7 to pass `inject_boot_entry=False` explicitly, e.g.:

```python
ub.write_bundle(spec, bundle_path=bundle, disk_size_gib=10,
                efi_vars_source=efi_src, iso_sources={},
                inject_boot_entry=False)
```

Test-only concern: `test_cli_build_writes_bundle` in Task 9 also now needs `"inject_boot_entry": False` in its payload. Add it.

- [ ] **Step 8: Run tests**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all tests PASS. Update golden fixture if plist bytes changed (they shouldn't — plist is unaffected).

- [ ] **Step 9: Re-run E2E build**

```bash
cd autopilot-proxmox && ansible-playbook playbooks/utm_build_win11_template.yml \
    -e vm_name=test-win11-efi \
    -e utm_iso_name=Win11_25H2_English_Arm64.iso \
    -e utm_documents_dir="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents" \
    -e @inventory/group_vars/all/vault.yml \
    -e utm_overwrite_bundle=true
```

**Watch the UTM window.** If Windows Setup starts without the EFI shell appearing, the experiment succeeded. If it drops to EFI shell, the osascript fallback kicks in and Setup starts ~15 s later — the experiment failed, but the keystroke hack is still in place so the build completes.

- [ ] **Step 10: Decide — success or abandon**

**If success (no EFI shell drop across the boot):**
- Proceed to Task 16 step "delete keystroke entirely".
- Commit:

```bash
git add autopilot-proxmox/web/utm_bundle.py autopilot-proxmox/tests/test_utm_bundle.py
git commit -m "feat(utm): build_efi_vars injects USB-CD boot entry (no shell drop)"
```

**If abandoned (budget blown or experiment doesn't converge):**
- Revert the renderer changes but keep the `build_efi_vars` scaffold commented out or gated behind `efi_vars_inject_boot_entry: false`. Don't commit broken code.
- Track follow-up ticket `utm-efi-vars-nvram-boot-entry` in `docs/UTM_MACOS_ARM64.md`.
- Proceed to Task 16 step "shrink keystroke to 5 lines".

---

## Task 16: Clean up osascript & retire old code

Delete the quit-relaunch dance unconditionally; shrink or delete the keystroke block based on Task 15's outcome; delete `create_bundle.yml` + `customize_plist.yml`.

**Files:**
- Modify: `autopilot-proxmox/playbooks/utm_build_win11_template.yml`
- Delete: `autopilot-proxmox/roles/utm_template_builder/tasks/create_bundle.yml`
- Delete: `autopilot-proxmox/roles/utm_template_builder/tasks/customize_plist.yml`
- Modify: `docs/UTM_MACOS_ARM64.md`
- Modify: `autopilot-proxmox/roles/utm_template_builder/defaults/main.yml` (add `utm_boot_fallback_keystrokes`)

- [ ] **Step 1: Remove quit-relaunch dance**

Edit `autopilot-proxmox/playbooks/utm_build_win11_template.yml`. Delete the task block from "Template build: quit UTM so it re-reads patched config.plist" through "Template build: verify VM appears in utmctl list" (approximately lines 179-214 in the pre-edit file). The new path never patches the plist post-hoc, so UTM never needs to re-read it.

- [ ] **Step 2: Handle keystroke block based on Task 15 outcome**

**If Task 15 succeeded (EFI experiment worked):**

Delete the entire `Template build: send EFI shell boot sequence via AppleScript` task and its preceding `wait_for` (approximately lines 243-276 of the pre-edit file).

**If Task 15 was abandoned (fallback path):**

Replace the 30-line keystroke block with this 5-line version, gated by a new default `utm_boot_fallback_keystrokes`:

```yaml
    - name: "Template build: escape EFI shell via AppleScript (fallback)"
      ansible.builtin.shell: |
        /usr/bin/osascript <<OSA
        tell application "UTM"
            set vm to virtual machine id "{{ _bundle_uuid }}"
            input keystroke vm text "fs0:\\efi\\boot\\bootaa64.efi"
            key code 36
            delay 2
            input keystroke vm text " "
        end tell
        OSA
      args:
        executable: /bin/bash
      changed_when: true
      when: utm_boot_fallback_keystrokes | default(true) | bool
```

And add to `autopilot-proxmox/roles/utm_template_builder/defaults/main.yml`:

```yaml
# Fallback path when EDK2 drops to the EFI shell on first boot. The
# structural fix (see build_efi_vars in web/utm_bundle.py) is preferred,
# but on machines where that didn't converge we keep a 5-line osascript
# step behind this flag.
utm_boot_fallback_keystrokes: true
```

- [ ] **Step 3: Delete obsolete task files**

```bash
git rm autopilot-proxmox/roles/utm_template_builder/tasks/create_bundle.yml \
       autopilot-proxmox/roles/utm_template_builder/tasks/customize_plist.yml
```

- [ ] **Step 4: Ansible syntax check**

```bash
cd autopilot-proxmox && ansible-playbook --syntax-check playbooks/utm_build_win11_template.yml
```

Expected: no errors.

- [ ] **Step 5: Re-run the E2E build, confirm green**

```bash
cd autopilot-proxmox && ansible-playbook playbooks/utm_build_win11_template.yml \
    -e vm_name=test-win11-final \
    -e utm_iso_name=Win11_25H2_English_Arm64.iso \
    -e utm_documents_dir="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents" \
    -e @inventory/group_vars/all/vault.yml \
    -e utm_overwrite_bundle=true
```

Expected: build completes. If Task 15 succeeded, UTM window shows Windows Setup directly; if fallback, 5-line keystroke runs once.

- [ ] **Step 6: Update handoff doc**

Edit `docs/UTM_MACOS_ARM64.md`. Under `## Final architecture`, replace the "Automation" bullet with:

```markdown
- **Automation**: `web/utm_bundle.py` writes a valid `config.plist` in
  one shot from a dataclass tree; Ansible role `utm_template_builder`
  invokes its CLI (`python -m web.utm_bundle build`). No `plutil` or
  `PlistBuddy`. UTM runs unmodified `/Applications/UTM.app`; the only
  binary we shell out to for control is `utmctl`.
```

Under `## Critical gotchas`, delete the "AppleScript is insert-only for drives" and "ImageName ignored by `make`" bullets — they no longer apply.

Under `## Known unresolved`, delete the "autounattend.xml does not yet set DriverPaths" bullet (Task 13 fixed it). If Task 15 was abandoned, add:
```markdown
- `build_efi_vars` experiment abandoned in sub-project 1; osascript
  fallback keystrokes remain behind `utm_boot_fallback_keystrokes`.
  See follow-up ticket `utm-efi-vars-nvram-boot-entry`.
```

Under `## Active tasks`, mark `utm-virtio-cd-plumbing` and `utm-driver-load-verify` as resolved.

- [ ] **Step 7: Commit**

```bash
git add -A autopilot-proxmox/playbooks/utm_build_win11_template.yml \
            autopilot-proxmox/roles/utm_template_builder/ \
            docs/UTM_MACOS_ARM64.md
git commit -m "chore(utm): retire plist-patch layer and obsolete playbook steps"
```

---

## Task 17: Second and third E2E passes for acceptance

Success criterion from the spec: "three runs in a row on a clean host." Prove the new path is stable.

**Files:** (no code changes)

- [ ] **Step 1: Clean run 1**

Delete any leftover test VMs in UTM. Run:

```bash
cd autopilot-proxmox && ansible-playbook playbooks/utm_build_win11_template.yml \
    -e vm_name=win11-acc-1 \
    -e utm_iso_name=Win11_25H2_English_Arm64.iso \
    -e utm_documents_dir="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents" \
    -e @inventory/group_vars/all/vault.yml \
    -e utm_overwrite_bundle=true
```

Must pass.

- [ ] **Step 2: Clean run 2**

```bash
cd autopilot-proxmox && ansible-playbook playbooks/utm_build_win11_template.yml \
    -e vm_name=win11-acc-2 \
    -e utm_iso_name=Win11_25H2_English_Arm64.iso \
    -e utm_documents_dir="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents" \
    -e @inventory/group_vars/all/vault.yml \
    -e utm_overwrite_bundle=true
```

Must pass.

- [ ] **Step 3: Clean run 3**

```bash
cd autopilot-proxmox && ansible-playbook playbooks/utm_build_win11_template.yml \
    -e vm_name=win11-acc-3 \
    -e utm_iso_name=Win11_25H2_English_Arm64.iso \
    -e utm_documents_dir="$HOME/Library/Containers/com.utmapp.UTM/Data/Documents" \
    -e @inventory/group_vars/all/vault.yml \
    -e utm_overwrite_bundle=true
```

Must pass.

- [ ] **Step 4: Run the full test suite one more time**

```bash
cd autopilot-proxmox && pytest tests/test_utm_bundle.py -v
```

Expected: all PASS.

- [ ] **Step 5: Clean up acceptance VMs**

```bash
for vm in win11-acc-1 win11-acc-2 win11-acc-3 test-win11-new test-win11-efi test-win11-final; do
    /Applications/UTM.app/Contents/MacOS/utmctl delete "$vm" 2>/dev/null || true
    rm -rf "$HOME/Library/Containers/com.utmapp.UTM/Data/Documents/${vm}.utm"
done
```

- [ ] **Step 6: Final documentation push**

Append a "Sub-project 1 complete" section to `docs/UTM_MACOS_ARM64.md`:

```markdown
## Sub-project 1 complete (2026-NN-NN)

`utm_bundle.py` replaces the AppleScript + plutil hybrid. Win11 ARM64
unattended template build green on three consecutive runs. No plutil in
the codebase. EFI shell drop: [eliminated via build_efi_vars] OR
[mitigated by 5-line keystroke fallback behind utm_boot_fallback_keystrokes].

Next: sub-project 2 (Ubuntu ARM template build) — separate spec.
```

Pick one of the two bracketed options based on Task 15 outcome. Commit:

```bash
git add docs/UTM_MACOS_ARM64.md
git commit -m "docs(utm): sub-project 1 complete — native lifecycle foundation"
```

---

## Appendix A — Debugging reference

**If `python -m web.utm_bundle` fails from Ansible:**
- Confirm `cd autopilot-proxmox` before the `ansible-playbook` command.
- Check `pyproject.toml` pythonpath = `["."]`.
- Run `cd autopilot-proxmox && python -c "import web.utm_bundle"` manually.

**If `utmctl register` fails with "bundle is not valid":**
- Open `config.plist` in Xcode (`open -a Xcode bundle/config.plist`) and look for missing required keys.
- Compare with a known-good bundle UTM made via the UI.
- Regenerate the schema contract (`scripts/extract_utm_schema.py`) and re-run `test_render_plist_every_key_exists_in_contract`.

**If Windows Setup still asks for a driver:**
- Check the virtio-win ISO is in `Data/` and mounted as drive 3.
- Verify the rendered `unattend.xml` contains `DriverPaths` (inspect the generated `AUTOUNATTEND.iso` with `hdiutil`).
- Confirm drive letters D/E/F cover whichever letter Setup assigns — add more `PathAndCredentials` if needed.

**If the EFI shell still appears after Task 15 experiment:**
- Increase `Timeout` UEFI variable via `virt-fw-vars --set-var` to 10 seconds.
- Try `QEMU.AdditionalArguments: ["-boot", "menu=on,splash-time=5000"]` in the spec as a secondary signal to EDK2.
- Verify the UEFI device path by booting the VM, dropping to EFI shell, running `map -r` and `bcfg boot dump` — compare against what was injected.
