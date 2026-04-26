"""Integration tests for /winpe/* routes."""
from fastapi.testclient import TestClient


def test_winpe_router_is_mounted():
    """Smoke test: importing app.py mounts the winpe router and serves a 404 (not 500) on a stub path."""
    from web.app import app
    client = TestClient(app)
    # The router exists; an unknown sha returns 404 (route exists but content not found),
    # not 500 (route would 500 if not mounted at all).
    resp = client.get("/winpe/content/0000000000000000000000000000000000000000000000000000000000000000")
    assert resp.status_code == 404


import hashlib

import pytest
from fastapi.testclient import TestClient

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore


@pytest.fixture
def isolated_artifact_root(tmp_path, monkeypatch):
    """Redirect web.winpe_routes._artifact_root to a per-test tmp_path."""
    from web import winpe_routes
    monkeypatch.setattr(winpe_routes, "_artifact_root", lambda: tmp_path)
    return tmp_path


def _seed_install_wim(root, content: bytes = b"fake install wim") -> str:
    src = root / "src.wim"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store = ArtifactStore(root)
    store.register(src, Sidecar(kind=ArtifactKind.INSTALL_WIM, sha256=sha, size=len(content), metadata={}), extension="wim")
    src.unlink()
    return sha


def test_manifest_returns_404_for_unknown_uuid(isolated_artifact_root):
    from web.app import app
    client = TestClient(app)
    resp = client.get("/winpe/manifest/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert "unknown" in resp.json()["detail"].lower()


def test_manifest_renders_for_registered_target(isolated_artifact_root):
    from web.app import app
    from web.winpe_targets_db import WinpeTargetsDb

    install_sha = _seed_install_wim(isolated_artifact_root)
    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(
        vm_uuid="aaaa-bbbb",
        install_wim_sha=install_sha,
        template_id="win11-arm64-baseline",
        params={"computer_name": "TEST-MANIFEST-01"},
    )

    client = TestClient(app)
    resp = client.get("/winpe/manifest/aaaa-bbbb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vmUuid"] == "aaaa-bbbb"
    apply_step = next(s for s in body["steps"] if s["type"] == "apply-wim")
    assert apply_step["content"]["sha256"] == install_sha
    # Rendered unattend was cached and is now servable via /winpe/content
    unattend_step = next(s for s in body["steps"] if s["type"] == "write-unattend")
    unattend_sha = unattend_step["content"]["sha256"]
    follow_up = client.get(f"/winpe/content/{unattend_sha}")
    assert follow_up.status_code == 200
    assert b"TEST-MANIFEST-01" in follow_up.content


def test_manifest_503_when_target_install_wim_missing(isolated_artifact_root):
    from web.app import app
    from web.winpe_targets_db import WinpeTargetsDb

    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(
        vm_uuid="vvv",
        install_wim_sha="0" * 64,  # not registered
        template_id="t",
        params={},
    )
    client = TestClient(app)
    resp = client.get("/winpe/manifest/vvv")
    assert resp.status_code == 503
    assert "install.wim" in resp.json()["detail"].lower()


def test_manifest_request_touches_last_manifest_at(isolated_artifact_root):
    from web.app import app
    from web.winpe_targets_db import WinpeTargetsDb

    install_sha = _seed_install_wim(isolated_artifact_root)
    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(vm_uuid="touchme", install_wim_sha=install_sha, template_id="t", params={})

    assert db.lookup("touchme").last_manifest_at is None
    client = TestClient(app)
    client.get("/winpe/manifest/touchme")
    assert db.lookup("touchme").last_manifest_at is not None
