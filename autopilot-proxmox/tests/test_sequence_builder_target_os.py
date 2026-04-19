"""Route-level smoke test for the sequence builder target_os selector
and Ubuntu step filtering."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"), \
             patch("web.app.HASH_DIR", tmp / "hashes"), \
             patch("web.app.job_manager") as jm:
            jm.list_jobs.return_value = []
            jm.jobs_dir = str(tmp / "jobs")
            from web.app import app
            from web import sequences_db as _sdb
            secrets.mkdir(parents=True, exist_ok=True)
            _sdb.init(db)
            yield TestClient(app)


def test_builder_renders_with_target_os_and_ubuntu_options(app_env):
    r = app_env.get("/sequences/new")
    assert r.status_code == 200
    body = r.text
    # target_os selector present
    assert 'id="target_os"' in body
    # Both OS options present
    assert 'value="windows"' in body
    assert 'value="ubuntu"' in body
    # Ubuntu step options are wired up
    assert "Install Ubuntu core" in body
    assert 'data-os="ubuntu"' in body
    assert 'data-os="windows"' in body
    assert 'data-os="both"' in body
    # Windows baseline still present
    assert "autopilot_entra" in body


def test_list_page_shows_target_os_badge(app_env):
    # Create a sequence explicitly ubuntu
    cr = app_env.post("/api/sequences", json={
        "name": "Ubuntu test", "description": "", "target_os": "ubuntu",
        "steps": [],
    })
    assert cr.status_code == 201
    # And a default windows one
    cr2 = app_env.post("/api/sequences", json={
        "name": "Win test", "description": "", "target_os": "windows",
        "steps": [],
    })
    assert cr2.status_code == 201

    r = app_env.get("/sequences")
    assert r.status_code == 200
    assert "os-badge" in r.text
    assert "os-ubuntu" in r.text
    assert "os-windows" in r.text


def test_api_create_accepts_target_os_ubuntu(app_env):
    r = app_env.post("/api/sequences", json={
        "name": "u1", "description": "", "target_os": "ubuntu",
        "steps": [],
    })
    assert r.status_code == 201
    sid = r.json()["id"]
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["target_os"] == "ubuntu"


def test_api_create_defaults_target_os_windows(app_env):
    r = app_env.post("/api/sequences", json={
        "name": "w1", "description": "", "steps": [],
    })
    assert r.status_code == 201
    sid = r.json()["id"]
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["target_os"] == "windows"


def test_api_update_changes_target_os(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "s1", "description": "", "steps": [],
    }).json()["id"]
    r = app_env.put(f"/api/sequences/{sid}", json={
        "target_os": "ubuntu",
    })
    assert r.status_code == 200
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["target_os"] == "ubuntu"
