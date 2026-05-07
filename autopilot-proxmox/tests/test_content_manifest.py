from __future__ import annotations

import pytest

from web.content_manifest import (
    ContentManifestValidationError,
    manifest_digest,
    validate_manifest,
)


def _valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "items": [
            {
                "id": "qemu-guest-agent",
                "kind": "package",
                "name": "QEMU Guest Agent",
                "version": "107.0",
                "source_uri": "https://content.local/qga-107.msi",
                "sha256": "D" * 64,
                "size_bytes": 1_048_576,
                "architecture": "x64",
                "target_os": "windows",
                "reboot_behavior": "none",
                "conditions": {"phase": "full_os", "min_build": 22631},
                "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
            }
        ],
    }


def test_valid_manifest_is_normalized():
    manifest = validate_manifest(_valid_manifest())

    assert manifest.to_dict() == {
        "schema_version": 1,
        "items": [
            {
                "id": "qemu-guest-agent",
                "kind": "package",
                "name": "QEMU Guest Agent",
                "version": "107.0",
                "source_uri": "https://content.local/qga-107.msi",
                "sha256": "d" * 64,
                "size_bytes": 1_048_576,
                "architecture": "x64",
                "target_os": "windows",
                "reboot_behavior": "none",
                "conditions": {"phase": "full_os", "min_build": 22631},
                "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
            }
        ],
    }


def test_invalid_kind_is_rejected():
    raw = _valid_manifest()
    raw["items"][0]["kind"] = "firmware"

    with pytest.raises(ContentManifestValidationError, match="kind"):
        validate_manifest(raw)


def test_invalid_sha256_is_rejected():
    raw = _valid_manifest()
    raw["items"][0]["sha256"] = "not-a-sha"

    with pytest.raises(ContentManifestValidationError, match="sha256"):
        validate_manifest(raw)


def test_manifest_digest_is_deterministic_for_equivalent_mapping_order():
    left = _valid_manifest()
    right = {
        "items": [
            {
                "metadata": {"install_command": "msiexec.exe /i {path} /qn"},
                "conditions": {"min_build": 22631, "phase": "full_os"},
                "reboot_behavior": "none",
                "target_os": "windows",
                "architecture": "x64",
                "size_bytes": 1_048_576,
                "sha256": "d" * 64,
                "source_uri": "https://content.local/qga-107.msi",
                "version": "107.0",
                "name": "QEMU Guest Agent",
                "kind": "package",
                "id": "qemu-guest-agent",
            }
        ],
        "schema_version": 1,
    }

    assert manifest_digest(left) == manifest_digest(right)
    assert len(manifest_digest(left)) == 64


def test_conditions_and_reboot_behavior_are_preserved():
    raw = _valid_manifest()
    raw["items"][0]["reboot_behavior"] = "required"
    raw["items"][0]["conditions"] = {
        "all": [
            {"target_os": "windows"},
            {"architecture": ["x64", "arm64"]},
        ]
    }

    item = validate_manifest(raw).items[0]

    assert item.reboot_behavior == "required"
    assert item.conditions == {
        "all": [
            {"target_os": "windows"},
            {"architecture": ["x64", "arm64"]},
        ]
    }
