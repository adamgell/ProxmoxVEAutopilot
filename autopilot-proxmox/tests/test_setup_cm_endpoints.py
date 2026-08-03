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


def test_setup_cm_module_publication_rejects_non_dc02_target(
    pg_conn,
    monkeypatch,
    tmp_path,
):
    client = _client_with_artifact_root(monkeypatch, tmp_path)

    response = client.post(
        "/api/setup-cm/v1/agents/agent-other/module-publications",
        json={"artifact_id": "00000000-0000-0000-0000-000000000001"},
    )

    assert response.status_code == 422


def test_setup_cm_module_publication_derives_request_from_registered_artifact(
    pg_conn,
    monkeypatch,
    tmp_path,
):
    from web import agent_telemetry_pg, setup_artifacts

    client = _client_with_artifact_root(monkeypatch, tmp_path)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-labz1-dc02",
        token="dc02-test-token",
        vmid=115,
    )
    archive = tmp_path / "setup-artifacts" / "setup-cm-module" / "setup-cm.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"module")
    artifact = setup_artifacts.register_existing_artifact(
        kind="setup-cm-module",
        path=archive,
        metadata={
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "source_commit": "b" * 40,
        },
    )

    response = client.post(
        "/api/setup-cm/v1/agents/agent-labz1-dc02/module-publications",
        json={"artifact_id": artifact["artifact_id"]},
    )

    assert response.status_code == 202
    assert response.json()["kind"] == "publish_setup_cm_module"
    assert response.json()["request"] == {
        "artifact_id": artifact["artifact_id"],
        "archive_sha256": artifact["sha256"],
        "source_commit": "b" * 40,
    }
