"""MDE onboarding credential round-trip.

Uses multipart upload against POST /api/credentials for the new
mde_onboarding type. The existing domain_join / local_admin / odj_blob
types continue to use the JSON payload shape; mde_onboarding accepts
multipart so the .py onboarding script can be streamed in directly.
"""
from __future__ import annotations

import base64
import json
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
        # Reset the process-wide cipher cache so this test's patched
        # CREDENTIAL_KEY is actually used (prevents cross-test contamination
        # when a previous test left a stale Fernet in web.app._CIPHER).
        import web.app as _wa
        _wa._CIPHER = None
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
            yield TestClient(app), db, secrets / "credential_key"


def test_create_mde_onboarding_credential_multipart(app_env):
    client, db, key_path = app_env
    payload_bytes = b"#!/usr/bin/env python3\n# onboarding payload\n"

    # Multipart POST — filename and type carried as form fields, script as file.
    resp = client.post(
        "/api/credentials",
        data={"name": "tenant-a-mde", "type": "mde_onboarding"},
        files={"onboarding_file": ("Onboard.py", payload_bytes, "text/x-python")},
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]

    # The list endpoint must NOT leak the encrypted blob or the script.
    lst = client.get("/api/credentials").json()
    entry = next(c for c in lst if c["id"] == cid)
    assert entry["type"] == "mde_onboarding"
    assert "encrypted_blob" not in entry
    assert "script_b64" not in entry

    # Decrypted round-trip via the sequences_db DAL restores original bytes.
    from web import crypto, sequences_db
    cipher = crypto.Cipher(key_path)
    cred = sequences_db.get_credential(db, cipher, cid)
    assert cred is not None
    assert cred["payload"]["filename"] == "Onboard.py"
    assert base64.b64decode(cred["payload"]["script_b64"]) == payload_bytes
    assert "uploaded_at" in cred["payload"]


def test_create_mde_onboarding_requires_file(app_env):
    client, _db, _key = app_env
    # Multipart submission with an empty file part -> 400 on create.
    resp = client.post(
        "/api/credentials",
        data={"name": "tenant-b-mde", "type": "mde_onboarding"},
        files={"onboarding_file": ("", b"", "application/octet-stream")},
    )
    assert resp.status_code == 400, resp.text


def test_update_mde_onboarding_keeps_script_when_no_file(app_env):
    client, db, key_path = app_env
    payload_bytes = b"# original\n"
    resp = client.post(
        "/api/credentials",
        data={"name": "tenant-c-mde", "type": "mde_onboarding"},
        files={"onboarding_file": ("A.py", payload_bytes, "text/x-python")},
    )
    assert resp.status_code == 201
    cid = resp.json()["id"]

    # Rename via the HTML edit endpoint with no new file — script must stay.
    resp = client.post(
        f"/credentials/{cid}/edit",
        data={"name": "tenant-c-renamed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from web import crypto, sequences_db
    cipher = crypto.Cipher(key_path)
    cred = sequences_db.get_credential(db, cipher, cid)
    assert cred["name"] == "tenant-c-renamed"
    assert base64.b64decode(cred["payload"]["script_b64"]) == payload_bytes
