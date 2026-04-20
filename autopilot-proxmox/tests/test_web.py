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


def test_provision_form_includes_hostname_pattern(client):
    response = client.get("/provision")
    assert response.status_code == 200
    assert 'name="hostname_pattern"' in response.text
    # Default pattern should be pre-filled
    assert "autopilot-{serial}" in response.text


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


def test_vms_page_shows_check_enrollment_for_ubuntu_vm(client):
    """Ubuntu-provisioned VMs get a Check Enrollment button and their
    Capture Hash button is rendered disabled (no Autopilot hash on Linux).
    Windows VMs keep the normal Capture Hash action."""
    from web import app as app_module

    # Seed an Ubuntu sequence and record a vmid → sequence provisioning.
    seq_id = sequences_db.create_sequence(
        app_module.SEQUENCES_DB,
        name="Test Ubuntu Plain",
        description="",
        target_os="ubuntu",
    )
    sequences_db.record_vm_provisioning(
        app_module.SEQUENCES_DB, vmid=107, sequence_id=seq_id,
    )

    fake_vms = [
        {
            "vmid": 107, "name": "ubu-test", "status": "running",
            "serial": "UB0001", "oem": "",
            "hostname": "ubu", "mem_mb": 2048, "cpus": 2,
            "tags": "autopilot;enroll-intune-healthy;enroll-mde-missing",
        },
        {
            "vmid": 108, "name": "win-test", "status": "running",
            "serial": "WN0001", "oem": "",
            "hostname": "win", "mem_mb": 2048, "cpus": 2,
            "tags": "autopilot",
        },
    ]
    with patch("web.app.get_autopilot_vms", return_value=fake_vms):
        with patch("web.app.get_autopilot_devices", return_value=([], None)):
            with patch("web.app.get_hash_files", return_value=[]):
                resp = client.get("/vms")
    assert resp.status_code == 200
    body = resp.text
    # Ubuntu VM: Check Enrollment button wired to checkEnroll(107, ...)
    assert "checkEnroll(107" in body
    # Ubuntu VM: enrollment chips rendered from persisted tags
    assert "chip-enroll-intune-healthy" in body
    assert "chip-enroll-mde-missing" in body
    # Windows VM keeps its normal Capture Hash action
    assert "postAction('/api/jobs/capture',{vmid:'108'" in body


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
