import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tmp_dirs():
    with tempfile.TemporaryDirectory() as jobs_dir:
        with tempfile.TemporaryDirectory() as hash_dir:
            yield jobs_dir, hash_dir


@pytest.fixture
def client(tmp_dirs):
    jobs_dir, hash_dir = tmp_dirs
    with patch("web.app.HASH_DIR", Path(hash_dir)):
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
