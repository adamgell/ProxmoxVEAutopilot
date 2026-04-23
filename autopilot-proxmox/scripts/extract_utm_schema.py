#!/usr/bin/env python3
"""Parse UTM's Swift sources and emit a JSON contract describing the
ConfigurationVersion 4 plist schema our renderer targets.

Usage:
    python scripts/extract_utm_schema.py \
        --utm-source /Users/Adam.Gell/src/UTM \
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
#          case foo (for enums without explicit values)
CASE_LINE = re.compile(r'^\s*case\s+(`?\w+`?)(?:\s*=\s*"([^"]+)")?')
# For explicit quoted values
CASE_WITH_VALUE = re.compile(r'^\s*case\s+\w+\s*=\s*"([^"]+)"')
# For simple cases, extract just the case name
SIMPLE_CASE = re.compile(r'^\s*case\s+(`?\w+`?)\s*(?://|$)')

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
                match = CASE_WITH_VALUE.match(line)
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
                # Try explicit value pattern first (case foo = "Bar")
                match = CASE_WITH_VALUE.match(line)
                if match:
                    enums[current_enum].append(match.group(1))
                else:
                    # Try simple case pattern (case foo)
                    match = SIMPLE_CASE.match(line)
                    if match:
                        case_name = match.group(1).strip("`")
                        enums[current_enum].append(case_name)
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
