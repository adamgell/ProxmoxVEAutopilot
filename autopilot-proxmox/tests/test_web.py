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
        jobs_db_path = Path(seq_dir) / "jobs.db"
        sequences_db.init(seq_db)
        # Also init jobs.db here — post-Task-13 tests enqueue/claim
        # against app_module.JOBS_DB and a fresh temp DB prevents
        # UNIQUE-constraint collisions across tests.
        from web import jobs_db
        jobs_db.init(jobs_db_path)
        with patch("web.app.HASH_DIR", Path(hash_dir)):
            with patch("web.app.SEQUENCES_DB", seq_db):
                with patch("web.app.JOBS_DB", jobs_db_path):
                    with patch("web.app.job_manager") as mock_manager:
                        from web.app import app
                        mock_manager.list_jobs.return_value = []
                        mock_manager.jobs_dir = jobs_dir
                        with TestClient(app) as tc:
                            yield tc


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
    # Ubuntu VM: Check Enrollment button wired via data-* attributes
    # (delegated handler routes data-vm-action="check-enroll" to
    # checkEnroll(vmid, btn)).
    assert 'data-vm-action="check-enroll"' in body
    assert 'data-vmid="107"' in body
    # Ubuntu VM: enrollment chips rendered from persisted tags
    assert "chip-enroll-intune-healthy" in body
    assert "chip-enroll-mde-missing" in body
    # Windows VM keeps its normal Capture Hash action (same data-*
    # pattern, action=capture).
    assert 'data-vm-action="capture"' in body
    assert 'data-vmid="108"' in body


def test_template_form_post_without_pause_leaves_args_unannotated(client):
    """Baseline: ticking nothing keeps the existing command unchanged and
    doesn't add pause args — avoids a regression on the default flow."""
    from web.app import job_manager
    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd); captured["args"] = args or {}
        return {"id": "fake-id"}
    job_manager.start.side_effect = fake_start
    r = client.post("/api/jobs/template", data={"profile": "lenovo-t14"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "pause_signal_path" not in captured["args"]
    assert not any("template_pause_signal_path" in t for t in captured["cmd"])


def test_template_form_post_with_pause_passes_signal_path(client):
    """Ticking the checkbox adds template_pause_signal_path to the
    ansible -e args and stashes the absolute path in job.args so the UI
    can build a Resume button."""
    from web.app import job_manager
    captured = {}
    def fake_start(name, cmd, args=None):
        captured["cmd"] = list(cmd); captured["args"] = args or {}
        return {"id": "fake-id"}
    job_manager.start.side_effect = fake_start
    r = client.post(
        "/api/jobs/template",
        data={"profile": "lenovo-t14", "pause_before_sysprep": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    pause_flag = next(
        (t for t in captured["cmd"] if t.startswith("template_pause_signal_path=")),
        None,
    )
    assert pause_flag is not None
    # Path is absolute + lives inside the job_manager.jobs_dir.
    path = pause_flag.split("=", 1)[1]
    assert path.startswith("/")
    assert captured["args"]["pause_enabled"] is True
    assert captured["args"]["pause_signal_path"] == path


def test_resume_template_build_touches_signal_file(client, tmp_path):
    """POST /api/jobs/{id}/resume-template-build creates the signal file
    (Ansible's wait_for unblocks on its appearance)."""
    from web.app import job_manager
    signal_path = tmp_path / "template-resume-xyz"
    job_manager.get_job.return_value = {
        "id": "J1", "status": "running",
        "args": {"pause_enabled": True, "pause_signal_path": str(signal_path)},
    }
    assert not signal_path.exists()
    r = client.post("/api/jobs/J1/resume-template-build")
    assert r.status_code == 200
    assert signal_path.exists()
    # Second call returns 409 (already resumed) rather than silently
    # re-touching and masking a confused operator clicking twice.
    r2 = client.post("/api/jobs/J1/resume-template-build")
    assert r2.status_code == 409


def test_resume_template_build_rejects_job_without_pause(client):
    from web.app import job_manager
    job_manager.get_job.return_value = {
        "id": "J2", "status": "running", "args": {"profile": "lenovo-t14"},
    }
    r = client.post("/api/jobs/J2/resume-template-build")
    assert r.status_code == 404


def test_resume_template_build_404_on_unknown_job(client):
    from web.app import job_manager
    job_manager.get_job.return_value = None
    r = client.post("/api/jobs/nope/resume-template-build")
    assert r.status_code == 404


def test_detect_template_pause_states():
    """Unit test the log-scan pause detector — it's what drives the
    PAUSED badge on both /jobs and /jobs/<id>."""
    from web.app import _detect_template_pause

    # Not a pause-enabled job → always False regardless of log content.
    assert _detect_template_pause(
        {"args": {}},
        "TASK [PAUSE — install software in VMID 9001]",
    ) is False

    # Pause-enabled but wait_for hasn't started yet (early job).
    assert _detect_template_pause(
        {"args": {"pause_enabled": True}},
        "TASK [Install Windows] ...\nPLAY RECAP",
    ) is False

    # Pause-enabled AND wait_for has started AND cleanup hasn't run.
    assert _detect_template_pause(
        {"args": {"pause_enabled": True}},
        "TASK [PAUSE — install software in VMID 9001]\nok: [localhost]",
    ) is True

    # After Resume: wait_for unblocked, cleanup task has started → no
    # longer paused even though the PAUSE marker is still in the log.
    assert _detect_template_pause(
        {"args": {"pause_enabled": True}},
        "TASK [PAUSE — install software in VMID 9001]\n"
        "ok: [localhost]\n"
        "TASK [Remove resume signal file (cleanup after pause)]\n",
    ) is False


def test_jobs_page_marks_paused_jobs(client):
    """/jobs renders a PAUSED badge for jobs with pause_enabled AND whose
    log shows the wait_for task has started but cleanup hasn't run yet."""
    from web.app import job_manager
    job_manager.list_jobs.return_value = [
        {"id": "paused-1", "playbook": "build_template",
         "status": "running", "started": "2026-04-21T12:00:00Z",
         "args": {"pause_enabled": True, "pause_signal_path": "/tmp/x"}},
        {"id": "running-1", "playbook": "provision_clone",
         "status": "running", "started": "2026-04-21T12:00:00Z",
         "args": {}},
    ]
    job_manager.get_log.return_value = (
        "TASK [PAUSE — install software in VMID 9001]\nok: [localhost]\n"
    )
    r = client.get("/jobs")
    assert r.status_code == 200
    body = r.text
    assert "PAUSED" in body
    assert "paused-1" in body
    assert "running-1" in body


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


def test_web_writes_service_health_heartbeat_on_startup(client):
    """Starting the app creates a 'web' row in service_health."""
    from web import app as web_app, service_health
    # Force one heartbeat synchronously via the module-level helper;
    # we don't need to wait for the async loop to tick.
    service_health.heartbeat(
        web_app.DEVICE_MONITOR_DB,
        service_id="web", service_type="web",
        version_sha="testsha", detail="idle",
    )
    rows = service_health.list_services(web_app.DEVICE_MONITOR_DB)
    ids = [r["service_id"] for r in rows]
    assert "web" in ids


def test_kill_sets_kill_requested_flag(client):
    """POST /api/jobs/<id>/kill flips kill_requested=1 and redirects."""
    from web import app as app_module, jobs_db
    jobs_db.enqueue(app_module.JOBS_DB, job_id="live",
                    job_type="capture_hash", playbook="x",
                    cmd=["sleep", "1"], args={})
    jobs_db.claim_next_job(app_module.JOBS_DB, worker_id="test-worker")

    r = client.post("/api/jobs/live/kill", follow_redirects=False)
    assert r.status_code == 303
    row = jobs_db.get_job(app_module.JOBS_DB, "live")
    assert row["kill_requested"] == 1


def test_healthz_ok_after_startup(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_vms_page_escapes_vm_name_in_data_attributes(client):
    """VM names can contain characters that would break inline onclick
    JS strings. After the fix, vm.name lands only in data-* attributes
    which are HTML-context-safe."""
    from web import app as app_module
    # The /vms page is backed by a process-wide cache that survives
    # across tests in the same module. Force a cold-start so our
    # patched get_autopilot_vms is actually invoked.
    app_module._VMS_CACHE.update(
        {"data": None, "devices": None, "hash_serials": None,
         "fetched_at": 0.0, "refreshing": False},
    )
    fake_vms = [{
        "vmid": 42, "name": "evil'); alert(1)//",
        "status": "running", "serial": "X0001", "oem": "",
        "hostname": "evil", "tags": "", "target_os": "windows",
    }]
    with patch("web.app.get_autopilot_vms", return_value=fake_vms), \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]):
        r = client.get("/vms")
    assert r.status_code == 200
    body = r.text
    assert 'data-vm-name="evil&#39;); alert(1)//"' in body \
           or 'data-vm-name="evil\'); alert(1)//"' in body
    assert "vm_name:'evil" not in body


def test_healthz_503_before_full_init():
    """Simulate schema init not complete: /healthz must 503, not 200.
    Exercised by temporarily patching both flags to False."""
    from fastapi.testclient import TestClient
    from web import app as app_module
    from unittest.mock import patch
    with patch.object(app_module, "_SEQUENCES_READY", False), \
         patch.object(app_module, "_JOBS_READY", False):
        client = TestClient(app_module.app)
        r = client.get("/healthz")
    assert r.status_code == 503
    assert "not complete" in r.json().get("detail", "")


def test_detect_template_pause_ignores_debug_echo_of_marker(client):
    """False-trigger guard: if a debug task's msg= contains the raw
    pause phrase, _detect_template_pause must NOT report paused
    because we anchor on the TASK [...] header."""
    from web.app import _detect_template_pause
    job = {"args": {"pause_enabled": True, "pause_signal_path": "/x"}}
    log = (
        "TASK [Install apps]\n"
        "ok: [localhost] => {\"msg\": \"PAUSE — install software in VMID 108\"}\n"
    )
    assert _detect_template_pause(job, log) is False


def test_detect_template_pause_anchored_on_task_header(client):
    from web.app import _detect_template_pause
    job = {"args": {"pause_enabled": True, "pause_signal_path": "/x"}}
    log = "TASK [PAUSE — install software in VMID 108 now, then click Resume]\nok\n"
    assert _detect_template_pause(job, log) is True
