from __future__ import annotations

from pathlib import Path


def _write_fake_msi(path: Path, *, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + (b"\0" * (size - 2)))
    return path


def test_latest_agent_release_ignores_tiny_placeholder(tmp_path, monkeypatch):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(
        setup_artifacts,
        "REGISTRY_PATH",
        artifact_root / "artifact_registry.json",
    )

    placeholder = artifact_root / "agent-msi" / "AutopilotAgent.msi"
    placeholder.parent.mkdir(parents=True, exist_ok=True)
    placeholder.write_text("placeholder", encoding="utf-8")
    setup_artifacts.register_existing_artifact(kind="agent-msi", path=placeholder)

    assert setup_artifacts.latest_agent_release() is None


def test_latest_agent_release_returns_newest_x64_release(tmp_path, monkeypatch):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(
        setup_artifacts,
        "REGISTRY_PATH",
        artifact_root / "artifact_registry.json",
    )

    old_msi = _write_fake_msi(
        artifact_root / "agent-msi" / "AutopilotAgent-0.1.2-win-x64.msi"
    )
    new_msi = _write_fake_msi(
        artifact_root / "agent-msi" / "AutopilotAgent-0.1.3-win-x64.msi"
    )
    arm_msi = _write_fake_msi(
        artifact_root / "agent-msi" / "AutopilotAgent-0.1.3-win-arm64.msi"
    )
    setup_artifacts.register_existing_artifact(
        kind="agent-msi",
        path=old_msi,
        metadata={"version": "0.1.2", "rid": "win-x64"},
    )
    setup_artifacts.register_existing_artifact(
        kind="agent-msi",
        path=arm_msi,
        metadata={"version": "0.1.3", "rid": "win-arm64"},
    )
    setup_artifacts.register_existing_artifact(
        kind="agent-msi",
        path=new_msi,
        metadata={"version": "0.1.3", "rid": "win-x64"},
    )

    release = setup_artifacts.latest_agent_release(runtime_identifier="win-x64")

    assert release is not None
    assert release["version"] == "0.1.3"
    assert release["runtime_identifier"] == "win-x64"
    assert release["path"] == str(new_msi)
    assert release["sha256"]
    assert release["size_bytes"] >= 4096
