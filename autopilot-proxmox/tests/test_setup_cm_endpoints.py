from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient


def _client_with_artifact_root(monkeypatch, tmp_path):
    from web import app as web_app, setup_artifacts

    artifact_root = tmp_path / "setup-artifacts"
    monkeypatch.setattr(setup_artifacts, "ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(
        setup_artifacts,
        "REGISTRY_PATH",
        artifact_root / "artifact_registry.json",
    )
    return TestClient(web_app.app)


def test_setup_cm_module_artifact_upload_requires_matching_hash(monkeypatch, tmp_path):
    client = _client_with_artifact_root(monkeypatch, tmp_path)

    response = client.post(
        "/api/setup-cm/v1/module-artifacts",
        files={"file": ("setup-cm.zip", b"module", "application/zip")},
        data={"sha256": "a" * 64, "source_commit": "b" * 40},
    )

    assert response.status_code == 422
    assert not list((tmp_path / "setup-artifacts" / "setup-cm-module").glob("*.zip"))


def test_setup_cm_module_artifact_upload_registers_private_artifact(monkeypatch, tmp_path):
    client = _client_with_artifact_root(monkeypatch, tmp_path)
    body = b"module"

    response = client.post(
        "/api/setup-cm/v1/module-artifacts",
        files={"file": ("setup-cm.zip", body, "application/zip")},
        data={
            "sha256": hashlib.sha256(body).hexdigest(),
            "source_commit": "b" * 40,
        },
    )

    assert response.status_code == 201
    assert response.json()["kind"] == "setup-cm-module"
    assert "download_url" not in response.json()
