"""End-to-end API tests for credentials and sequences routes."""
import os
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
            # Init DB without seeds so "empty" tests remain valid.
            from web import sequences_db as _sdb
            secrets.mkdir(parents=True, exist_ok=True)
            _sdb.init(db)
            yield TestClient(app)


def test_credentials_list_empty(app_env):
    r = app_env.get("/api/credentials")
    assert r.status_code == 200
    assert r.json() == []


def test_create_credential(app_env):
    r = app_env.post("/api/credentials", json={
        "name": "acme-svc", "type": "domain_join",
        "payload": {"username": "acme\\svc", "password": "p@ss",
                    "domain_fqdn": "acme.local"},
    })
    assert r.status_code == 201
    cid = r.json()["id"]

    r = app_env.get("/api/credentials")
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "acme-svc"

    # Full get includes payload
    r = app_env.get(f"/api/credentials/{cid}")
    assert r.status_code == 200
    assert r.json()["payload"]["password"] == "p@ss"


def test_create_credential_duplicate_name(app_env):
    body = {"name": "a", "type": "local_admin",
            "payload": {"username": "x", "password": "y"}}
    assert app_env.post("/api/credentials", json=body).status_code == 201
    assert app_env.post("/api/credentials", json=body).status_code == 409


def test_update_credential_partial(app_env):
    cid = app_env.post("/api/credentials", json={
        "name": "a", "type": "local_admin",
        "payload": {"username": "x", "password": "y"},
    }).json()["id"]
    r = app_env.patch(f"/api/credentials/{cid}", json={"name": "a-new"})
    assert r.status_code == 200
    assert app_env.get(f"/api/credentials/{cid}").json()["name"] == "a-new"


def test_delete_credential_blocked(app_env):
    cid = app_env.post("/api/credentials", json={
        "name": "a", "type": "domain_join",
        "payload": {"username": "x", "password": "y", "domain_fqdn": "z"},
    }).json()["id"]
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "",
        "steps": [
            {"step_type": "join_ad_domain",
             "params": {"credential_id": cid, "ou_path": "OU=X"},
             "enabled": True},
        ],
    }).json()["id"]
    r = app_env.delete(f"/api/credentials/{cid}")
    assert r.status_code == 409
    assert sid in r.json()["sequence_ids"]


def test_sequences_list_empty(app_env):
    assert app_env.get("/api/sequences").json() == []


def test_create_sequence_with_steps(app_env):
    r = app_env.post("/api/sequences", json={
        "name": "Entra", "description": "d", "is_default": True,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": "dell-latitude-5540"}, "enabled": True},
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    })
    assert r.status_code == 201
    sid = r.json()["id"]
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["name"] == "Entra"
    assert got["is_default"] is True
    assert [s["step_type"] for s in got["steps"]] == [
        "set_oem_hardware", "autopilot_entra"]


def test_update_sequence_replaces_steps(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "",
        "steps": [
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }).json()["id"]
    r = app_env.put(f"/api/sequences/{sid}", json={
        "name": "S", "description": "updated",
        "steps": [
            {"step_type": "local_admin",
             "params": {"credential_id": 99}, "enabled": True},
        ],
    })
    assert r.status_code == 200
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["description"] == "updated"
    assert [s["step_type"] for s in got["steps"]] == ["local_admin"]


def test_duplicate_sequence(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "Original", "description": "",
        "steps": [
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }).json()["id"]
    r = app_env.post(f"/api/sequences/{sid}/duplicate",
                     json={"new_name": "Original (copy)"})
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert app_env.get(f"/api/sequences/{new_id}").json()["name"] == \
        "Original (copy)"


def test_delete_sequence(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "", "steps": [],
    }).json()["id"]
    assert app_env.delete(f"/api/sequences/{sid}").status_code == 200
    assert app_env.get(f"/api/sequences/{sid}").status_code == 404


def test_only_one_default_via_api(app_env):
    a = app_env.post("/api/sequences", json={
        "name": "A", "description": "", "is_default": True, "steps": [],
    }).json()["id"]
    b = app_env.post("/api/sequences", json={
        "name": "B", "description": "", "is_default": True, "steps": [],
    }).json()["id"]
    got_a = app_env.get(f"/api/sequences/{a}").json()
    got_b = app_env.get(f"/api/sequences/{b}").json()
    assert got_a["is_default"] is False
    assert got_b["is_default"] is True


def test_startup_seeds_defaults(tmp_path):
    """When the app starts on an empty DB, the three seed sequences appear."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from fastapi.testclient import TestClient

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
            # Importing triggers @on_event("startup"); TestClient replays it.
            from web.app import app
            with TestClient(app) as c:
                got = c.get("/api/sequences").json()
    names = [s["name"] for s in got]
    assert "Entra Join (default)" in names
    assert "AD Domain Join — Local Admin" in names
    assert "Hybrid Autopilot (stub)" in names
