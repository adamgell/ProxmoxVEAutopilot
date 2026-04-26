import json
from pathlib import Path

import pytest

from web.artifact_sidecar import (
    ArtifactKind,
    SidecarValidationError,
    load_sidecar,
)


def _write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "sidecar.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_valid_install_wim_sidecar(tmp_path):
    p = _write(tmp_path, {
        "kind": "install-wim",
        "sha256": "a" * 64,
        "size": 4_500_000_000,
        "edition": "Windows 11 Enterprise",
        "architecture": "arm64",
    })
    sc = load_sidecar(p)
    assert sc.kind is ArtifactKind.INSTALL_WIM
    assert sc.sha256 == "a" * 64
    assert sc.size == 4_500_000_000


def test_valid_pe_wim_sidecar(tmp_path):
    p = _write(tmp_path, {
        "kind": "pe-wim",
        "sha256": "b" * 64,
        "size": 350_000_000,
        "architecture": "arm64",
    })
    sc = load_sidecar(p)
    assert sc.kind is ArtifactKind.PE_WIM


def test_unknown_kind_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "mystery", "sha256": "c" * 64, "size": 1})
    with pytest.raises(SidecarValidationError, match="kind"):
        load_sidecar(p)


def test_missing_sha256_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "install-wim", "size": 1})
    with pytest.raises(SidecarValidationError, match="sha256"):
        load_sidecar(p)


def test_short_sha_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "pe-wim", "sha256": "abcd", "size": 1})
    with pytest.raises(SidecarValidationError, match="sha256"):
        load_sidecar(p)


def test_uppercase_sha_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "pe-wim", "sha256": "A" * 64, "size": 1})
    with pytest.raises(SidecarValidationError, match="lowercase"):
        load_sidecar(p)


def test_negative_size_rejected(tmp_path):
    p = _write(tmp_path, {"kind": "pe-wim", "sha256": "d" * 64, "size": -1})
    with pytest.raises(SidecarValidationError, match="size"):
        load_sidecar(p)


def test_extra_metadata_preserved(tmp_path):
    p = _write(tmp_path, {
        "kind": "install-wim",
        "sha256": "e" * 64,
        "size": 100,
        "buildHost": "buildhost",
        "buildTimestamp": "2026-04-25T12:00:00Z",
        "driversInjected": ["viostor", "NetKVM"],
    })
    sc = load_sidecar(p)
    assert sc.metadata["buildHost"] == "buildhost"
    assert sc.metadata["driversInjected"] == ["viostor", "NetKVM"]
