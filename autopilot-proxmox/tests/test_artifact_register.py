import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_registers_artifact(tmp_path):
    artifact_root = tmp_path / "artifacts"
    src = tmp_path / "test.wim"
    content = b"fake wim content"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()

    sidecar_path = tmp_path / "test.json"
    sidecar_path.write_text(json.dumps({
        "kind": "pe-wim",
        "sha256": sha,
        "size": len(content),
        "buildHost": "test-host",
    }))

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable, "-m", "web.artifact_register",
            "--path", str(src),
            "--sidecar", str(sidecar_path),
            "--artifact-root", str(artifact_root),
            "--extension", "wim",
        ],
        cwd=repo_root / "autopilot-proxmox",
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    assert sha in result.stdout
    assert (artifact_root / "store" / f"{sha}.wim").exists()


def test_cli_rejects_sha_mismatch(tmp_path):
    artifact_root = tmp_path / "artifacts"
    src = tmp_path / "test.wim"
    src.write_bytes(b"abc")
    sidecar_path = tmp_path / "test.json"
    sidecar_path.write_text(json.dumps({
        "kind": "pe-wim",
        "sha256": "0" * 64,
        "size": 3,
    }))
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable, "-m", "web.artifact_register",
            "--path", str(src),
            "--sidecar", str(sidecar_path),
            "--artifact-root", str(artifact_root),
            "--extension", "wim",
        ],
        cwd=repo_root / "autopilot-proxmox",
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "sha256 mismatch" in result.stderr


def test_cli_rejects_invalid_sidecar(tmp_path):
    artifact_root = tmp_path / "artifacts"
    src = tmp_path / "test.wim"
    src.write_bytes(b"abc")
    sidecar_path = tmp_path / "test.json"
    sidecar_path.write_text(json.dumps({"kind": "mystery", "sha256": "a" * 64, "size": 3}))
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable, "-m", "web.artifact_register",
            "--path", str(src),
            "--sidecar", str(sidecar_path),
            "--artifact-root", str(artifact_root),
            "--extension", "wim",
        ],
        cwd=repo_root / "autopilot-proxmox",
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unknown kind" in result.stderr.lower()
