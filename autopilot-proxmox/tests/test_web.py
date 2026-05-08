import json
import os
import tempfile
import threading
from contextlib import contextmanager
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
def client(tmp_dirs, pg_conn):
    jobs_dir, hash_dir = tmp_dirs
    with tempfile.TemporaryDirectory() as seq_dir:
        seq_db = None
        sequences_db.reset_for_tests(pg_conn)
        sequences_db.init(seq_db)
        import web.app  # noqa: F401 - make web.app resolvable for patch()
        with patch("web.app.HASH_DIR", Path(hash_dir)):
            with patch("web.app.SEQUENCES_DB", seq_db):
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


def test_job_detail_uses_live_jobs_websocket(client):
    from web.app import job_manager

    job_manager.get_job.return_value = {
        "id": "JLIVE",
        "playbook": "template.yml",
        "status": "running",
        "started": "2026-05-07T12:00:00+00:00",
        "ended": None,
        "exit_code": None,
        "args": {"pause_enabled": True, "pause_signal_path": "/tmp/pause"},
    }
    job_manager.get_log.return_value = "TASK [still building]\nok\n"

    response = client.get("/jobs/JLIVE")

    assert response.status_code == 200
    assert "/api/live/ws" in response.text
    assert "applyJobDetailLive" in response.text
    assert 'topics: [\'jobs\']' in response.text
    assert "window.location.reload()" not in response.text


def test_jobs_page_uses_live_jobs_websocket(client):
    from web.app import job_manager

    job_manager.list_jobs.return_value = [{
        "id": "JOBS1",
        "playbook": "capture.yml",
        "status": "running",
        "started": "2026-05-07T12:00:00+00:00",
        "ended": None,
        "exit_code": None,
        "args": {},
    }]

    response = client.get("/jobs")

    assert response.status_code == 200
    assert "/api/live/ws" in response.text
    assert "applyLiveJobsTable" in response.text
    assert 'topics: [\'jobs\']' in response.text


def test_qga_recovery_script_download(client):
    response = client.get("/api/qga/recovery-script.ps1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment;" in response.headers["content-disposition"]
    assert "QgaWatchdogRecovery.ps1" in response.headers["content-disposition"]
    assert "Restart-Service -Name QEMU-GA" in response.text
    assert "--retry-path" in response.text
    assert r"\\.\Global\org.qemu.guest_agent.0" in response.text
    assert "\\ProxmoxVEAutopilot\\QgaWatchdog" in response.text


def test_qga_recovery_script_is_auth_exempt():
    from web import auth

    assert auth.is_exempt_path("/api/qga/recovery-script.ps1")


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


@pytest.mark.real_app_database_startup
def test_web_writes_service_health_heartbeat_on_startup(monkeypatch, tmp_path):
    """Startup creates the service_health table before routes can serve."""
    from web import app as web_app
    from web import (
        db_pg,
        device_history_pg,
        devices_pg,
        jobs_pg,
        sequences_pg,
        service_health_pg,
        ts_engine_pg,
    )

    calls = []
    heartbeat_seen = threading.Event()

    class FakeConn:
        pass

    @contextmanager
    def fake_connection(dsn):
        calls.append(("connect", dsn))
        yield FakeConn()

    def fake_jobs_init(conn):
        calls.append(("jobs_init", conn.__class__.__name__))

    def fake_sequences_init(conn):
        calls.append(("sequences_init", conn.__class__.__name__))

    def fake_seed_defaults(_handle, _cipher):
        calls.append(("sequences_seed", "ok"))

    def fake_service_health_init(conn=None):
        calls.append((
            "service_health_init",
            conn.__class__.__name__ if conn is not None else "None",
        ))

    def fake_ts_init(conn):
        calls.append(("ts_init", conn.__class__.__name__))

    def fake_device_history_init(conn):
        calls.append(("device_history_init", conn.__class__.__name__))

    def fake_devices_init(conn):
        calls.append(("devices_init", conn.__class__.__name__))

    def fake_heartbeat(**kwargs):
        calls.append(("heartbeat", kwargs))
        heartbeat_seen.set()

    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", "postgresql://startup-test")
    monkeypatch.setattr(db_pg, "connection", fake_connection)
    monkeypatch.setattr(jobs_pg, "init", fake_jobs_init)
    monkeypatch.setattr(sequences_pg, "init", fake_sequences_init)
    monkeypatch.setattr(sequences_pg, "seed_defaults", fake_seed_defaults)
    monkeypatch.setattr(service_health_pg, "init", fake_service_health_init)
    monkeypatch.setattr(service_health_pg, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(ts_engine_pg, "init", fake_ts_init)
    monkeypatch.setattr(device_history_pg, "init", fake_device_history_init)
    monkeypatch.setattr(devices_pg, "init", fake_devices_init)
    monkeypatch.setattr(web_app, "SEQUENCES_DB", tmp_path / "sequences.db")
    monkeypatch.setattr(web_app, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(
        web_app, "CREDENTIAL_KEY", tmp_path / "secrets" / "credential_key"
    )
    web_app._CIPHER = None

    with TestClient(web_app.app):
        assert heartbeat_seen.wait(timeout=2)

    assert ("service_health_init", "FakeConn") in calls
    assert ("ts_init", "FakeConn") in calls
    assert ("device_history_init", "FakeConn") in calls
    assert ("devices_init", "FakeConn") in calls
    heartbeat_calls = [call for call in calls if call[0] == "heartbeat"]
    assert heartbeat_calls == [
        (
            "heartbeat",
            {
                "service_id": "web",
                "service_type": "web",
                "version_sha": web_app._load_version_sha(),
                "detail": "idle",
            },
        )
    ]


def test_api_services_returns_postgres_service_health(pg_conn, client):
    from web import service_health_pg as service_health

    service_health.init(pg_conn)
    pg_conn.execute("TRUNCATE service_health")
    pg_conn.commit()
    service_health.heartbeat(
        service_id="web", service_type="web",
        version_sha="testsha", detail="idle",
    )

    r = client.get("/api/services")

    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["services"][0]["service_id"] == "web"
    assert body["services"][0]["version_sha"] == "testsha"
    assert body["services"][0]["status"] == "ok"


def test_kill_sets_kill_requested_flag(client, pg_conn):
    """POST /api/jobs/<id>/kill flips kill_requested=1 and redirects."""
    from web import jobs_pg as jobs_db

    jobs_db.enqueue(
        job_id="live",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "1"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="test-worker")

    r = client.post("/api/jobs/live/kill", follow_redirects=False)
    assert r.status_code == 303
    row = jobs_db.get_job("live")
    assert row["kill_requested"] is True


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


def test_vms_page_cold_start_uses_monitor_snapshot(client, tmp_path, pg_conn):
    """A cold /vms page load should not block on live guest-agent probes
    when the monitor service already has a completed sweep snapshot."""
    from web import app as app_module, device_history_db

    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    sweep_id = device_history_db.start_sweep()
    device_history_db.insert_pve_snapshot(sweep_id, {
        "vmid": 116,
        "node": "pve2",
        "name": "Gell-EC41E7EB",
        "status": "running",
        "tags_csv": "autopilot",
        "cores": 2,
        "memory_mb": 4096,
        "smbios1": "serial=template-default,uuid=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "args": "-smbios file=/var/lib/vz/snippets/Gell-EC41E7EB.bin",
        "config_digest": "digest",
    })
    device_history_db.insert_device_probe(sweep_id, {
        "vmid": 116,
        "vm_name": "Gell-EC41E7EB",
        "win_name": "Gell-EC41E7EB",
        "serial": "Gell-EC41E7EB",
        "os_build": "26100",
        "intune_found": 1,
        "intune_match_count": 1,
        "intune_matches_json": "[{\"complianceState\": \"compliant\"}]",
        "dsreg_status": "{\"AzureAdJoined\": \"YES\", \"TenantName\": \"home\"}",
    })
    device_history_db.finish_sweep(sweep_id, vm_count=1)

    app_module._VMS_CACHE.update(
        {"data": None, "devices": None, "hash_serials": None,
         "fetched_at": 0.0, "refreshing": False},
    )
    with patch("web.app.DEVICE_MONITOR_DB", monitor_db), \
         patch("web.app.get_autopilot_vms",
               side_effect=AssertionError("live VM probe should not run")) as live_vms, \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]), \
         patch("web.app._refresh_vms_cache_bg"):
        r = client.get("/vms")

    assert r.status_code == 200
    assert "Gell-EC41E7EB" in r.text
    assert 'data-vmid="116"' in r.text
    assert "Intune" in r.text
    assert "Not in Autopilot" not in r.text
    live_vms.assert_not_called()


def test_vms_page_renders_monitor_snapshot_freshness(client, tmp_path, pg_conn):
    """Rows built from monitor snapshots should show when PVE/probe data
    was last checked, and the header should expose sweep freshness."""
    from web import app as app_module, device_history_db

    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    sweep_id = device_history_db.start_sweep()
    device_history_db.insert_pve_snapshot(sweep_id, {
        "vmid": 117,
        "node": "pve2",
        "name": "freshness-vm",
        "status": "running",
        "tags_csv": "autopilot",
        "checked_at": "2026-04-20T23:55:00+00:00",
        "config_digest": "digest",
    })
    device_history_db.insert_device_probe(sweep_id, {
        "vmid": 117,
        "vm_name": "freshness-vm",
        "win_name": "freshness-vm",
        "serial": "FRESH117",
        "checked_at": "2026-04-20T23:56:30+00:00",
    })
    device_history_db.finish_sweep(sweep_id, vm_count=1)

    app_module._VMS_CACHE.update(
        {"data": None, "devices": None, "hash_serials": None,
         "fetched_at": 0.0, "refreshing": False},
    )
    with patch("web.app.DEVICE_MONITOR_DB", monitor_db), \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]), \
         patch("web.app._refresh_vms_cache_bg"):
        r = client.get("/vms")

    assert r.status_code == 200
    assert "Last monitor sweep" in r.text
    assert "Last checked" in r.text
    assert "Last probed" in r.text
    assert 'data-utc="2026-04-20T23:55:00+00:00"' in r.text
    assert 'data-utc="2026-04-20T23:56:30+00:00"' in r.text


def test_monitoring_sweep_now_refreshes_warm_vms_cache(client, tmp_path, monkeypatch, pg_conn):
    """A manual monitoring sweep should not leave a warm /vms cache serving
    the pre-sweep VM rows after the sweep task has completed."""
    from web import app as app_module, device_history_db, monitor_main

    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    app_module._VMS_CACHE.update({
        "data": [{
            "vmid": 199,
            "name": "stale-vm",
            "status": "running",
            "serial": "STALE199",
            "oem": "",
            "hostname": "stale-vm",
            "tags": "autopilot",
        }],
        "devices": ([], None),
        "hash_serials": set(),
        "fetched_at": app_module.time.monotonic(),
        "refreshing": False,
    })

    def fake_sweep():
        sweep_id = device_history_db.start_sweep()
        device_history_db.insert_pve_snapshot(sweep_id, {
            "vmid": 200,
            "node": "pve2",
            "name": "fresh-vm",
            "status": "running",
            "tags_csv": "autopilot",
            "checked_at": "2026-04-21T00:00:00+00:00",
            "config_digest": "digest",
        })
        device_history_db.insert_device_probe(sweep_id, {
            "vmid": 200,
            "vm_name": "fresh-vm",
            "win_name": "fresh-vm",
            "serial": "FRESH200",
            "checked_at": "2026-04-21T00:00:05+00:00",
        })
        device_history_db.finish_sweep(sweep_id, vm_count=1)

    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", monitor_db)
    monkeypatch.setattr(monitor_main, "_do_sweep_tick", fake_sweep)
    monkeypatch.setattr(app_module, "get_autopilot_devices", lambda: ([], None))
    monkeypatch.setattr(app_module, "get_hash_files", lambda: [])

    r = client.post("/api/monitoring/sweep-now")
    assert r.status_code == 202

    r = client.get("/vms")
    assert r.status_code == 200
    assert "fresh-vm" in r.text
    assert "stale-vm" not in r.text


def test_vms_refresh_preserves_monitor_join_evidence_over_live_fallback(
    client, tmp_path, monkeypatch, pg_conn,
):
    """The Refresh button must not replace a Postgres monitor snapshot with
    older live-row shaping that lacks Entra/domain evidence fields."""
    from web import app as app_module, device_history_db

    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    sweep_id = device_history_db.start_sweep()
    device_history_db.insert_pve_snapshot(sweep_id, {
        "vmid": 108,
        "node": "pve2",
        "name": "Gell-60F03E42",
        "status": "running",
        "tags_csv": "autopilot",
        "config_digest": "digest",
    })
    device_history_db.insert_device_probe(sweep_id, {
        "vmid": 108,
        "vm_name": "Gell-60F03E42",
        "win_name": "Gell-60F03E42",
        "serial": "Gell-60F03E42",
        "entra_found": 1,
        "entra_match_count": 1,
        "entra_matches_json": json.dumps([{"trustType": "AzureAd"}]),
    })
    device_history_db.finish_sweep(sweep_id, vm_count=1)

    app_module._VMS_CACHE.update({
        "data": [{
            "vmid": 108,
            "name": "Gell-60F03E42",
            "status": "running",
            "serial": "Gell-60F03E42",
            "hostname": "Gell-60F03E42",
            "tags": "autopilot",
            "part_of_domain": False,
            "hybrid_joined": False,
            "entra_joined": False,
        }],
        "devices": ([], None),
        "hash_serials": set(),
        "fetched_at": app_module.time.monotonic(),
        "refreshing": False,
    })
    stale_live_rows = [{
        "vmid": 108,
        "name": "Gell-60F03E42",
        "status": "running",
        "serial": "Gell-60F03E42",
        "hostname": "Gell-60F03E42",
        "tags": "autopilot",
        "part_of_domain": False,
        "hybrid_joined": False,
        "entra_joined": False,
    }]

    monkeypatch.setattr(app_module, "DEVICE_MONITOR_DB", monitor_db)
    monkeypatch.setattr(app_module, "get_autopilot_vms", lambda: stale_live_rows)
    monkeypatch.setattr(app_module, "get_autopilot_devices", lambda: ([], None))
    monkeypatch.setattr(app_module, "get_hash_files", lambda: [])

    r = client.post("/api/vms/refresh")
    assert r.status_code == 200

    r = client.get("/vms")
    assert r.status_code == 200
    assert ">Entra ID<" in r.text
    assert ">workgroup<" not in r.text


def test_vms_page_hybrid_entra_trust_shows_domain_badge(client, tmp_path, pg_conn):
    """If Entra reports trustType=ServerAd, the hostname badge should show
    domain evidence instead of falling through to workgroup."""
    from web import app as app_module, device_history_db

    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    sweep_id = device_history_db.start_sweep()
    device_history_db.insert_pve_snapshot(sweep_id, {
        "vmid": 106,
        "node": "pve2",
        "name": "Gell-E9C0C757",
        "status": "running",
        "tags_csv": "autopilot",
        "config_digest": "digest",
    })
    device_history_db.insert_device_probe(sweep_id, {
        "vmid": 106,
        "vm_name": "Gell-E9C0C757",
        "win_name": "Gell-E9C0C757",
        "serial": "Gell-E9C0C757",
        "entra_found": 1,
        "entra_match_count": 1,
        "entra_matches_json": "[{\"trustType\": \"ServerAd\"}]",
    })
    device_history_db.finish_sweep(sweep_id, vm_count=1)

    app_module._VMS_CACHE.update(
        {"data": None, "devices": None, "hash_serials": None,
         "fetched_at": 0.0, "refreshing": False},
    )
    with patch("web.app.DEVICE_MONITOR_DB", monitor_db), \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]), \
         patch("web.app._refresh_vms_cache_bg"):
        r = client.get("/vms")

    assert r.status_code == 200
    assert "Hybrid Entra join" in r.text
    assert ">domain<" in r.text
    assert ">workgroup<" not in r.text


def test_vms_page_entra_join_shows_entra_badge_not_workgroup(client, tmp_path, pg_conn):
    """Cloud Entra-joined devices should show Entra ID in the hostname
    bubble. Workgroup is only for devices with no domain or Entra evidence."""
    from web import app as app_module, device_history_db

    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    sweep_id = device_history_db.start_sweep()
    device_history_db.insert_pve_snapshot(sweep_id, {
        "vmid": 118,
        "node": "pve2",
        "name": "Gell-CLOUDJOIN",
        "status": "running",
        "tags_csv": "autopilot",
        "config_digest": "digest",
    })
    device_history_db.insert_device_probe(sweep_id, {
        "vmid": 118,
        "vm_name": "Gell-CLOUDJOIN",
        "win_name": "Gell-CLOUDJOIN",
        "serial": "Gell-CLOUDJOIN",
        "entra_found": 1,
        "entra_match_count": 1,
        "entra_matches_json": "[{\"trustType\": \"AzureAd\"}]",
    })
    device_history_db.finish_sweep(sweep_id, vm_count=1)

    app_module._VMS_CACHE.update(
        {"data": None, "devices": None, "hash_serials": None,
         "fetched_at": 0.0, "refreshing": False},
    )
    with patch("web.app.DEVICE_MONITOR_DB", monitor_db), \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]), \
         patch("web.app._refresh_vms_cache_bg"):
        r = client.get("/vms")

    assert r.status_code == 200
    assert "Entra ID joined" in r.text
    assert ">Entra ID<" in r.text
    assert ">workgroup<" not in r.text


def test_vms_page_entra_badge_explains_intune_entra_link(client, tmp_path, pg_conn):
    """When Entra was found by Intune azureADDeviceId linkage, the
    hostname bubble should expose that source in the tooltip."""
    from web import app as app_module, device_history_db

    device_id = "6a0ba1f9-0090-4683-aee3-31a6abc1e4ad"
    monitor_db = tmp_path / "device_monitor.db"
    device_history_db.reset_for_tests(pg_conn)
    device_history_db.init()
    sweep_id = device_history_db.start_sweep()
    device_history_db.insert_pve_snapshot(sweep_id, {
        "vmid": 108,
        "node": "pve2",
        "name": "Gell-60F03E42",
        "status": "running",
        "tags_csv": "autopilot",
        "config_digest": "digest",
    })
    device_history_db.insert_device_probe(sweep_id, {
        "vmid": 108,
        "vm_name": "Gell-60F03E42",
        "win_name": "Gell-60F03E42",
        "serial": "Gell-60F03E42",
        "entra_found": 1,
        "entra_match_count": 1,
        "entra_matches_json": json.dumps([{
            "displayName": "WIN-C4P3CQ6R5LQ",
            "deviceId": device_id,
            "trustType": "AzureAd",
        }]),
        "intune_found": 1,
        "intune_match_count": 1,
        "intune_matches_json": json.dumps([{
            "deviceName": "WIN-C4P3CQ6R5LQ",
            "azureADDeviceId": device_id,
        }]),
    })
    device_history_db.finish_sweep(sweep_id, vm_count=1)

    app_module._VMS_CACHE.update(
        {"data": None, "devices": None, "hash_serials": None,
         "fetched_at": 0.0, "refreshing": False},
    )
    with patch("web.app.DEVICE_MONITOR_DB", monitor_db), \
         patch("web.app.get_autopilot_devices", return_value=([], None)), \
         patch("web.app.get_hash_files", return_value=[]), \
         patch("web.app._refresh_vms_cache_bg"):
        r = client.get("/vms")

    assert r.status_code == 200
    assert ">Entra ID<" in r.text
    assert "Intune azureADDeviceId -&gt; Entra deviceId" in r.text
    assert ">workgroup<" not in r.text


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


def test_live_websocket_hello_and_fleet_snapshot(web_client, monkeypatch):
    from web import app as app_module

    app_module._LIVE_HUB = None
    app_module._VMS_CACHE.update({
        "data": [{
            "vmid": 108,
            "name": "Gell-60F03E42",
            "status": "running",
            "hostname": "Gell-60F03E42",
            "entra_id_joined": True,
            "hostname_join_label": "Entra ID",
        }],
        "devices": ([], None),
        "hash_serials": set(),
        "fetched_at": 1.0,
        "refreshing": False,
    })
    monkeypatch.setattr(app_module, "_latest_monitor_sweep_status", lambda: {"running": False})

    async def fake_payload():
        return app_module._VMS_CACHE, 0.0

    async def empty_patches(_topics, _vmids, _include_qga):
        return []

    monkeypatch.setattr(app_module, "_get_vms_payload", fake_payload)
    monkeypatch.setattr(app_module, "_live_patch_provider", empty_patches)

    with web_client.websocket_connect("/api/live/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "subscribe", "topics": ["fleet"], "vmids": [108]})
        snapshot = ws.receive_json()
        while snapshot["type"] != "snapshot" or snapshot.get("topic") != "fleet":
            snapshot = ws.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["topic"] == "fleet"
    assert snapshot["data"]["rows"][0]["vmid"] == 108
    assert snapshot["data"]["rows"][0]["hostname_join_label"] == "Entra ID"


def test_live_websocket_clients_share_one_collector(web_client, monkeypatch):
    from web import app as app_module

    app_module._LIVE_HUB = None

    async def empty_snapshots(_topics, _vmids):
        return []

    async def empty_patches(_topics, _vmids, _include_qga):
        return []

    monkeypatch.setattr(app_module, "_live_snapshot_provider", empty_snapshots)
    monkeypatch.setattr(app_module, "_live_patch_provider", empty_patches)

    with web_client.websocket_connect("/api/live/ws") as ws1:
        assert ws1.receive_json()["type"] == "hello"
        with web_client.websocket_connect("/api/live/ws") as ws2:
            assert ws2.receive_json()["type"] == "hello"
            assert app_module._get_live_hub().collector_starts == 1


def test_live_hub_disables_automatic_qga_polling():
    from web import app as app_module

    app_module._LIVE_HUB = None
    hub = app_module._get_live_hub()

    assert hub.qga_interval_seconds is None


def test_monitor_context_guest_details_avoid_qga_by_default(monkeypatch):
    from web import app as app_module

    monkeypatch.delenv("AUTOPILOT_MONITOR_QGA_DETAILS", raising=False)
    monkeypatch.setattr(app_module, "_load_proxmox_config", lambda: {})

    def fake_proxmox_api(path):
        if path == "/nodes/pve2/qemu/106/config":
            return {
                "name": "Gell-E9C0C757",
                "tags": "autopilot",
                "smbios1": "uuid=11111111-2222-3333-4444-555555555555",
            }
        raise AssertionError(f"unexpected Proxmox API call {path}")

    monkeypatch.setattr(app_module, "_proxmox_api", fake_proxmox_api)
    monkeypatch.setattr(
        app_module,
        "_fetch_guest_windows_details",
        lambda _node, _vmid: (_ for _ in ()).throw(
            AssertionError("monitor should not guest-exec by default")
        ),
    )

    ctx = app_module._build_live_monitor_context()
    guest = ctx.fetch_guest_details(106, "pve2")

    assert guest["win_name"] == "Gell-E9C0C757"
    assert guest["serial"] == "Gell-E9C0C757"
    assert guest["uuid"] == "11111111-2222-3333-4444-555555555555"


def test_monitor_context_guest_details_can_opt_into_qga(monkeypatch):
    from web import app as app_module

    monkeypatch.setenv("AUTOPILOT_MONITOR_QGA_DETAILS", "1")
    monkeypatch.setattr(app_module, "_load_proxmox_config", lambda: {})

    def fake_proxmox_api(path):
        if path == "/nodes/pve2/qemu/106/config":
            return {"name": "Gell-E9C0C757", "tags": "autopilot"}
        raise AssertionError(f"unexpected Proxmox API call {path}")

    monkeypatch.setattr(app_module, "_proxmox_api", fake_proxmox_api)
    monkeypatch.setattr(
        app_module,
        "_fetch_guest_windows_details",
        lambda _node, _vmid: {
            "Name": "WINDOWS-RENAMED",
            "SerialNumber": "Gell-E9C0C757",
            "OSBuild": "26100",
        },
    )

    ctx = app_module._build_live_monitor_context()
    guest = ctx.fetch_guest_details(106, "pve2")

    assert guest["win_name"] == "WINDOWS-RENAMED"
    assert guest["serial"] == "Gell-E9C0C757"
    assert guest["os_build"] == "26100"


def test_live_websocket_rejects_unauthenticated_client(web_client):
    from starlette.websockets import WebSocketDisconnect
    from web import app as app_module

    with patch.object(app_module, "_AUTH_BYPASS", False):
        with pytest.raises(WebSocketDisconnect) as exc:
            with web_client.websocket_connect("/api/live/ws"):
                pass

    assert exc.value.code == 1008


def test_live_websocket_screenshot_result_and_image_url(web_client, monkeypatch):
    from web import app as app_module

    app_module._LIVE_HUB = None
    monkeypatch.setattr(
        app_module,
        "_capture_vm_screenshot_png",
        lambda vmid: b"\x89PNG\r\n\x1a\nfake",
    )

    with web_client.websocket_connect("/api/live/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({
            "type": "screenshot.request",
            "correlation_id": "shot-1",
            "vmid": 114,
            "format": "png",
        })
        result = ws.receive_json()

    assert result["type"] == "screenshot.result"
    assert result["correlation_id"] == "shot-1"
    assert result["vmid"] == 114
    assert result["image_url"].startswith("/api/live/screenshots/")

    image = web_client.get(result["image_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == b"\x89PNG\r\n\x1a\nfake"


def test_live_fleet_qga_check_uses_only_agent_info(monkeypatch):
    from web import app as app_module

    app_module._LIVE_QGA_FAILURES.clear()
    app_module._VMS_CACHE.update({
        "data": [{"vmid": 106}],
        "devices": ([], None),
        "hash_serials": set(),
        "fetched_at": 1.0,
        "refreshing": False,
    })
    monkeypatch.setattr(app_module, "_resolve_vm_node", lambda vmid: "pve2")

    get_paths = []

    def fake_get(path):
        get_paths.append(path)
        if path == "/nodes/pve2/qemu/106/status/current":
            return {"status": "running", "qmpstatus": "running"}
        if path == "/nodes/pve2/qemu/106/agent/info":
            return {"result": {"supported_commands": []}}
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(app_module, "_proxmox_api", fake_get)
    monkeypatch.setattr(
        app_module,
        "_proxmox_api_post",
        lambda path, data=None: (_ for _ in ()).throw(
            AssertionError(f"unexpected POST {path}")
        ),
    )

    rows = app_module._live_collect_fleet_patch(set(), include_qga=True)

    assert rows[0]["qga"] == "ready"
    assert "/nodes/pve2/qemu/106/agent/info" in get_paths
    assert not any("/agent/get-host-name" in path for path in get_paths)
    assert not any("/agent/network-get-interfaces" in path for path in get_paths)


def test_live_fleet_qga_failure_backoff_skips_repeat_probe(monkeypatch):
    from web import app as app_module

    app_module._LIVE_QGA_FAILURES.clear()
    app_module._VMS_CACHE.update({
        "data": [{"vmid": 106}],
        "devices": ([], None),
        "hash_serials": set(),
        "fetched_at": 1.0,
        "refreshing": False,
    })
    monkeypatch.setattr(app_module, "_resolve_vm_node", lambda vmid: "pve2")

    get_paths = []

    def fake_get(path):
        get_paths.append(path)
        if path == "/nodes/pve2/qemu/106/status/current":
            return {"status": "running", "qmpstatus": "running"}
        if path == "/nodes/pve2/qemu/106/agent/info":
            raise RuntimeError("QEMU guest agent is not running")
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(app_module, "_proxmox_api", fake_get)

    first = app_module._live_collect_fleet_patch(set(), include_qga=True)
    second = app_module._live_collect_fleet_patch(set(), include_qga=True)

    assert first[0]["qga"] == "unavailable"
    assert "QEMU guest agent is not running" in first[0]["qga_error"]
    assert second[0]["qga"] == "unavailable"
    assert "QEMU guest agent is not running" in second[0]["qga_error"]
    assert second[0]["qga_retry_in_seconds"] > 0
    assert get_paths.count("/nodes/pve2/qemu/106/agent/info") == 1
    app_module._LIVE_QGA_FAILURES.clear()


@pytest.mark.anyio
async def test_live_qga_probe_handler_uses_only_agent_info(monkeypatch):
    from web import app as app_module

    app_module._LIVE_QGA_FAILURES.clear()
    monkeypatch.setattr(app_module, "_resolve_vm_node", lambda vmid: "pve2")

    get_paths = []

    def fake_get(path):
        get_paths.append(path)
        if path == "/nodes/pve2/qemu/106/agent/info":
            return {"result": {"version": "110.0.2", "supported_commands": []}}
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(app_module, "_proxmox_api", fake_get)
    monkeypatch.setattr(
        app_module,
        "_fetch_guest_windows_details",
        lambda node, vmid: (_ for _ in ()).throw(
            AssertionError("qga_probe must not run guest Windows details")
        ),
    )

    result = await app_module._live_qga_probe_handler(106)

    assert result["qga"] == "ready"
    assert result["version"] == "110.0.2"
    assert get_paths == ["/nodes/pve2/qemu/106/agent/info"]
    app_module._LIVE_QGA_FAILURES.clear()


@pytest.mark.anyio
async def test_live_qga_probe_handler_backs_off_after_failure(monkeypatch):
    from web import app as app_module

    app_module._LIVE_QGA_FAILURES.clear()
    monkeypatch.setattr(app_module, "_resolve_vm_node", lambda vmid: "pve2")

    get_paths = []

    def fake_get(path):
        get_paths.append(path)
        if path == "/nodes/pve2/qemu/107/agent/info":
            raise RuntimeError("QEMU guest agent is not running")
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(app_module, "_proxmox_api", fake_get)

    first = await app_module._live_qga_probe_handler(107)
    second = await app_module._live_qga_probe_handler(107)

    assert first["qga"] == "unavailable"
    assert "QEMU guest agent is not running" in first["qga_error"]
    assert second["qga"] == "unavailable"
    assert second["qga_retry_in_seconds"] > 0
    assert get_paths == ["/nodes/pve2/qemu/107/agent/info"]
    app_module._LIVE_QGA_FAILURES.clear()


def test_screenshot_capture_ssh_targets_resolved_vm_node(monkeypatch):
    from web import app as app_module
    from web import answer_floppy_cache

    runner_hosts = []
    ssh_commands = []

    monkeypatch.setattr(
        app_module,
        "_load_proxmox_config",
        lambda: {
            "proxmox_host": "192.168.2.200",
            "proxmox_node": "pve2",
            "vault_proxmox_root_password": "secret",
            "vault_proxmox_root_username": "root@pam",
        },
    )
    monkeypatch.setattr(app_module, "_resolve_vm_node", lambda vmid: "pve2")
    monkeypatch.setattr(
        app_module,
        "_proxmox_api",
        lambda path: [
            {"type": "cluster", "name": "homelab"},
            {"type": "node", "name": "pve1", "ip": "192.168.2.200"},
            {"type": "node", "name": "pve2", "ip": "192.168.2.48"},
        ] if path == "/cluster/status" else [],
    )
    monkeypatch.setattr(app_module, "_ppm_to_png", lambda ppm: b"png")

    def fake_runner(*, host, password, user):
        runner_hosts.append(host)

        def ssh(cmd):
            ssh_commands.append(cmd)
            return 0, b"P6\n1 1\n255\n\x00\x00\x00", b""

        return ssh

    monkeypatch.setattr(answer_floppy_cache, "make_sshpass_runner", fake_runner)

    assert app_module._capture_vm_screenshot_png(106) == b"png"
    assert runner_hosts == ["192.168.2.48"]
    assert "/nodes/pve2/qemu/106/monitor" in ssh_commands[0]


def test_live_websocket_screenshot_failure_keeps_socket_open(web_client, monkeypatch):
    from web import app as app_module

    app_module._LIVE_HUB = None

    def fail(_vmid):
        raise RuntimeError("display plane unavailable")

    monkeypatch.setattr(app_module, "_capture_vm_screenshot_png", fail)

    with web_client.websocket_connect("/api/live/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({
            "type": "screenshot.request",
            "correlation_id": "shot-2",
            "vmid": 114,
            "format": "png",
        })
        error = ws.receive_json()
        ws.send_json({"type": "not-real", "correlation_id": "still-open"})
        still_open = ws.receive_json()

    assert error["type"] == "error"
    assert error["error"] == "screenshot_failed"
    assert "display plane unavailable" in error["detail"]
    assert still_open["error"] == "unknown_message_type"


def test_live_screenshot_url_expires(web_client):
    from web import app as app_module

    stored = app_module._store_screenshot(vmid=114, png_bytes=b"png")
    screenshot_id = stored["image_url"].rsplit("/", 1)[-1]
    app_module._SCREENSHOT_CACHE[screenshot_id]["expires_at_monotonic"] = 0

    response = web_client.get(stored["image_url"])

    assert response.status_code == 404


def test_vms_page_includes_live_socket_and_screenshot_action(client):
    fake_vms = [{
        "vmid": 114,
        "name": "LAB-E3DF41BD",
        "status": "running",
        "serial": "LAB-E3DF41BD",
        "oem": "",
        "hostname": "LAB-E3DF41BD",
        "mem_mb": 4096,
        "cpus": 2,
        "tags": "autopilot",
        "in_intune": False,
        "in_autopilot": False,
        "aad_joined": False,
        "part_of_domain": False,
    }]
    async def fake_payload():
        return ({
            "data": fake_vms,
            "devices": ([], None),
            "hash_serials": set(),
            "fetched_at": 1.0,
            "refreshing": False,
        }, 0.0)

    with patch("web.app._get_vms_payload", fake_payload):
        response = client.get("/vms")

    assert response.status_code == 200
    assert "/api/live/ws" in response.text
    assert "data-vm-action=\"screenshot\"" in response.text
    assert "/api/qga/recovery-script.ps1" in response.text
    assert "Download QGA recovery script" in response.text
    assert "data-live-vmid=\"114\"" in response.text
    assert '<table id="vm-fleet-table" data-disable-row-pulse="true">' in response.text
    assert '<table id="vm-fleet-table" class="cockpit-scanline">' not in response.text
    assert "download=\"vm-' + _htmlEsc(String(vmid)) + '-screenshot.png\"" in response.text
    assert "width:max-content" in response.text
    assert "qga.title = row.qga_error || ''" in response.text
