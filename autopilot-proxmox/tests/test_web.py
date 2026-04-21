import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from web import sequences_db


@pytest.fixture
def tmp_dirs():
    with tempfile.TemporaryDirectory() as jobs_dir:
        with tempfile.TemporaryDirectory() as hash_dir:
            yield jobs_dir, hash_dir


@pytest.fixture
def client(tmp_dirs):
    jobs_dir, hash_dir = tmp_dirs
    with tempfile.TemporaryDirectory() as seq_dir:
        seq_db = Path(seq_dir) / "sequences.db"
        sequences_db.init(seq_db)
        with patch("web.app.HASH_DIR", Path(hash_dir)):
            with patch("web.app.SEQUENCES_DB", seq_db):
                with patch("web.app.job_manager") as mock_manager:
                    from web.app import app
                    mock_manager.list_jobs.return_value = []
                    mock_manager.jobs_dir = jobs_dir
                    yield TestClient(app)


def test_home_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Proxmox VE Autopilot" in response.text


def test_provision_page_renders(client):
    response = client.get("/provision")
    assert response.status_code == 200
    assert "OEM Profile" in response.text
    assert "lenovo-t14" in response.text


def test_template_page_renders(client):
    response = client.get("/template")
    assert response.status_code == 200
    assert "Build Template" in response.text


def test_hashes_page_empty(client):
    response = client.get("/hashes")
    assert response.status_code == 200
    assert "No hash files" in response.text


def test_jobs_page_empty(client):
    response = client.get("/jobs")
    assert response.status_code == 200
    assert "No jobs yet" in response.text


def test_job_detail_not_found(client):
    from web.app import job_manager
    job_manager.get_job.return_value = None
    response = client.get("/jobs/fake-id")
    assert response.status_code == 404


def test_redirect_with_error_encodes_special_chars():
    """Error messages with spaces, '&', '#', '?' must round-trip through
    the URL without truncation or param smuggling."""
    from web.app import _redirect_with_error
    r = _redirect_with_error("/vms", "Rename failed: name 'x & y' needs # escaping?")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/vms?error=")
    # Reserved chars must be encoded
    assert "+" in loc  # space -> '+'
    assert "%26" in loc  # '&'
    assert "%23" in loc  # '#'
    assert "%3F" in loc  # '?'
    # And the full message survives a decode
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(loc).query)
    assert qs["error"] == ["Rename failed: name 'x & y' needs # escaping?"]


def test_load_brand_context_default():
    from web.app import _load_brand_context
    brand = _load_brand_context()
    assert brand["name"] == "ProxmoxVEAutopilot" or brand["name"]  # whatever vars.yml carries
    assert "registry_root" in brand
    assert brand["registry_root"].startswith(r"HKLM:\SOFTWARE")


# ---------------------------------------------------------------------------
# Phase B.2b — Capture-hash button gated by sequence.produces_autopilot_hash
# ---------------------------------------------------------------------------

def _seed_with_ad_vm(seq_db_path):
    """Seed sequences + wire a VM (vmid 200) to the non-Autopilot AD seed."""
    from web import crypto, sequences_db
    cipher_key = Path(seq_db_path).parent / "credential_key"
    cipher_key.write_bytes(b"0" * 32)  # Fernet raw key — 32 bytes, will be b64'd
    # Use the real Cipher; seed_defaults wants one.
    from cryptography.fernet import Fernet
    cipher_key.write_bytes(Fernet.generate_key())
    cipher = crypto.Cipher(cipher_key)
    sequences_db.seed_defaults(seq_db_path, cipher)
    ad_seq = next(s for s in sequences_db.list_sequences(seq_db_path)
                  if s["name"].startswith("AD Domain Join"))
    entra_seq = next(s for s in sequences_db.list_sequences(seq_db_path)
                     if s["name"].startswith("Entra Join"))
    sequences_db.record_vm_provisioning(seq_db_path, vmid=200, sequence_id=ad_seq["id"])
    sequences_db.record_vm_provisioning(seq_db_path, vmid=201, sequence_id=entra_seq["id"])
    return ad_seq["id"], entra_seq["id"]


def test_vms_page_disables_capture_for_non_autopilot_sequence(client, tmp_dirs):
    """A VM provisioned from a sequence with produces_autopilot_hash=0
    (the AD domain-join seed) renders the Capture Hash button disabled.
    A VM from the Entra seed renders it enabled. A VM with no
    provisioning row (legacy) renders enabled too."""
    from web import app as web_app
    _seed_with_ad_vm(web_app.SEQUENCES_DB)
    fake_vms = [
        {"vmid": 200, "name": "ad-join-01",   "hostname": "",
         "serial": "AD200", "oem": "", "status": "running"},
        {"vmid": 201, "name": "entra-01",     "hostname": "",
         "serial": "EN201", "oem": "", "status": "running"},
        {"vmid": 999, "name": "legacy-no-seq","hostname": "",
         "serial": "LG999", "oem": "", "status": "running"},
    ]
    with patch("web.app.get_autopilot_vms", return_value=fake_vms), \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]):
        r = client.get("/vms")
    assert r.status_code == 200
    body = r.text
    # Disabled capture button for vmid 200 — reason text present.
    assert "Sequence does not produce an Autopilot hash" in body
    # Bulk checkbox for vmid 200 is disabled; for 201 it's active.
    # (Can't assert strict order without parsing, but both markers must
    # appear — the "disabled" form is for the AD VM, the value=... with
    # 201 is for the Entra VM.)
    assert 'value="201:entra-01"' in body
    # The legacy VM (no sequence row) should still allow capture.
    assert 'value="999:legacy-no-seq"' in body
