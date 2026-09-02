from __future__ import annotations

import hashlib

import pytest


def _configure_registry(monkeypatch, tmp_path):
    from web import setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(
        setup_artifacts,
        "REGISTRY_PATH",
        artifact_root / "artifact_registry.json",
    )
    return setup_artifacts, artifact_root


def test_setup_cm_module_metadata_requires_matching_hash_and_source_commit(
    tmp_path,
    monkeypatch,
):
    setup_artifacts, artifact_root = _configure_registry(monkeypatch, tmp_path)
    archive = artifact_root / "setup-cm-module" / "setup-cm.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"module")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="source_commit"):
        setup_artifacts.register_existing_artifact(
            kind="setup-cm-module",
            path=archive,
            metadata={"sha256": digest},
        )

    with pytest.raises(ValueError, match="sha256"):
        setup_artifacts.register_existing_artifact(
            kind="setup-cm-module",
            path=archive,
            metadata={"sha256": "a" * 64, "source_commit": "b" * 40},
        )

    row = setup_artifacts.register_existing_artifact(
        kind="setup-cm-module",
        path=archive,
        metadata={"sha256": digest, "source_commit": "b" * 40},
    )

    assert row["metadata"] == {"sha256": digest, "source_commit": "b" * 40}
    assert setup_artifacts.get_artifact(
        row["artifact_id"],
        kind="setup-cm-module",
    )["artifact_id"] == row["artifact_id"]
