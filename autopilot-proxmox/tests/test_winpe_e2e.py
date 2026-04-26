"""End-to-end integration: simulate a PE-side bootstrap walking the manifest."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore
from web.winpe_checkin_db import WinpeCheckinDb
from web.winpe_targets_db import WinpeTargetsDb


@pytest.fixture
def isolated_artifact_root(tmp_path, monkeypatch):
    from web import winpe_routes
    monkeypatch.setattr(winpe_routes, "_artifact_root", lambda: tmp_path)
    return tmp_path


def test_pe_bootstrap_round_trip(isolated_artifact_root):
    """Stand in for the real PE Bootstrap.ps1: pull manifest, fetch each content blob, post checkins."""
    from web.app import app

    # Setup: register an install.wim and a winpe_target referencing it.
    install_content = b"INSTALL_WIM_PAYLOAD"
    install_sha = hashlib.sha256(install_content).hexdigest()
    src = isolated_artifact_root / "install.wim"
    src.write_bytes(install_content)
    store = ArtifactStore(isolated_artifact_root)
    store.register(
        src,
        Sidecar(kind=ArtifactKind.INSTALL_WIM, sha256=install_sha,
                size=len(install_content), metadata={"edition": "Win11 Enterprise ARM64"}),
        extension="wim",
    )
    src.unlink()

    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(
        vm_uuid="e2e-uuid",
        install_wim_sha=install_sha,
        template_id="win11-arm64-baseline",
        params={"computer_name": "E2E-VM-99"},
    )

    client = TestClient(app)

    # Step 1: PE fetches manifest.
    manifest = client.get("/winpe/manifest/e2e-uuid").json()
    assert manifest["vmUuid"] == "e2e-uuid"

    # Step 2: PE walks each step's content reference (where present).
    fetched = {}
    for step in manifest["steps"]:
        if "content" in step:
            sha = step["content"]["sha256"]
            blob = client.get(f"/winpe/content/{sha}")
            assert blob.status_code == 200, f"step {step['id']} content fetch failed"
            fetched[step["id"]] = blob.content

    # Verify the install.wim and unattend.xml round-trip cleanly.
    assert fetched["a1"] == install_content
    assert b"E2E-VM-99" in fetched["u1"]

    # Step 3: PE POSTs a checkin for each step.
    timestamp_base = "2026-04-25T22:00:"
    for i, step in enumerate(manifest["steps"]):
        ts = f"{timestamp_base}{i:02d}Z"
        client.post("/winpe/checkin", json={
            "vmUuid": "e2e-uuid",
            "stepId": step["id"],
            "status": "ok",
            "timestamp": ts,
            "durationSec": 1.0 + i,
            "logTail": f"step {step['id']} done",
            "errorMessage": None,
            "extra": {},
        })

    # Step 4: Verify all checkins persisted in order.
    checkin_db = WinpeCheckinDb(isolated_artifact_root / "checkins.db")
    rows = checkin_db.list_for_vm("e2e-uuid")
    assert len(rows) == len(manifest["steps"])
    assert [r.step_id for r in rows] == [s["id"] for s in manifest["steps"]]
