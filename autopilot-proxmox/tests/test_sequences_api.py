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
            from web.app import app, _init_sequences_db
            _init_sequences_db()
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
