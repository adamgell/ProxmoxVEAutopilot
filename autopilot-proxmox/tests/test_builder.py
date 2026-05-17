import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_env(pg_conn):
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d) / "jobs"
        jobs_dir.mkdir()
        output_dir = Path(d) / "output"
        output_dir.mkdir()
        yield jobs_dir, output_dir


def _create_osdeploy_run_for_builder_test(pg_conn):
    from web import osdeploy_pg, sequences_pg, ts_engine_pg

    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    osdeploy_pg.reset_for_tests(pg_conn)
    osdeploy_pg.init(pg_conn)
    artifact = osdeploy_pg.create_artifact(
        pg_conn,
        build_sha="osdeploytest",
        iso_path="/app/output/osdeploy-server.iso",
        wim_path="/app/output/osdeploy-server.wim",
        manifest_path="/app/output/osdeploy-server.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        source_media="Windows Server 2025",
        image_name="Windows Server 2025 Datacenter",
        image_index=4,
        os_version="Windows Server 2025",
        os_edition="Datacenter",
        os_language="en-us",
        built_by_host="builder-01",
        proxmox_volid="local:iso/osdeploy-server.iso",
    )
    return osdeploy_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="OSDEPLOY-E2E-006",
        node="pvetest",
        iso_storage="local",
        storage="local-lvm",
        network_bridge="vmbr0",
    )


def _write_osdeploy_preidentity_failure_log(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "TASK [proxmox_vm_clone : Clone template to new VM 105] ********",
                "changed: [localhost]",
                "TASK [proxmox_vm_clone : Update VM config] ********************",
                "fatal: [localhost]: FAILED! => "
                '{"msg": "Status code was 500 and not [200]: '
                "HTTP Error 500: storage 'isos' does not exist\"}",
            ]
        ),
        encoding="utf-8",
    )


def test_builder_runs_one_job_and_exits_on_stop(tmp_env):
    """Happy path: enqueue a job, builder claims + spawns + finalizes."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["echo", "ok"],
        args={},
    )

    stop = threading.Event()

    def _fake_run(row, log_path, db_path, worker_id, stop_event):
        jobs_db.finalize_job(row["id"], exit_code=0)

    with patch("web.builder._run_one_job", side_effect=_fake_run):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={
                "jobs_dir": jobs_dir,
                "db_path": output_dir / "jobs.db",
                "monitor_db_path": output_dir / "device_monitor.db",
                "worker_id": "test-worker",
                "stop_event": stop,
                "poll_interval_seconds": 0.1,
            },
            daemon=True,
        )
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

    assert jobs_db.get_job("j1")["status"] == "complete"


def test_builder_run_one_job_kills_on_kill_requested(tmp_env):
    """With kill_requested=1, the heartbeat tick should terminate the subprocess."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "30"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="test-worker")

    proc = MagicMock()
    proc.poll.side_effect = [None, 0, 0]
    proc.terminate = MagicMock()

    row = jobs_db.get_job("j1")
    jobs_db.request_kill("j1")

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()

    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="test-worker",
            stop_event=stop,
            heartbeat_seconds=0.05,
        )

    proc.terminate.assert_called_once()
    assert jobs_db.get_job("j1")["exit_code"] == 0


def test_builder_idle_sleeps_when_no_jobs(tmp_env):
    """When claim returns None, the loop sleeps and retries without busy-looping."""
    from web import builder

    jobs_dir, output_dir = tmp_env
    stop = threading.Event()
    claims = []

    def _mock_claim(*args, **kwargs):
        claims.append(time.monotonic())
        return None

    with patch("web.builder.jobs_db.claim_next_job", side_effect=_mock_claim):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={
                "jobs_dir": jobs_dir,
                "db_path": output_dir / "jobs.db",
                "monitor_db_path": output_dir / "device_monitor.db",
                "worker_id": "t",
                "stop_event": stop,
                "poll_interval_seconds": 0.1,
            },
            daemon=True,
        )
        t.start()
        time.sleep(0.35)
        stop.set()
        t.join(timeout=1)

    assert 2 <= len(claims) <= 6


def test_builder_run_one_job_exits_cleanly_on_reap(tmp_env, pg_conn):
    """A reaped row terminates the subprocess and stays orphaned."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "30"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="w")

    proc = MagicMock()
    proc.poll.return_value = None
    proc.terminate = MagicMock()

    row = jobs_db.get_job("j1")
    pg_conn.execute("UPDATE jobs SET status='orphaned' WHERE id=%s", ("j1",))
    pg_conn.commit()

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()
    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="w",
            stop_event=stop,
            heartbeat_seconds=0.01,
        )
    assert proc.terminate.called
    assert jobs_db.get_job("j1")["status"] == "orphaned"


def test_builder_swallows_transient_db_errors(tmp_env):
    """Transient heartbeat DB errors are logged and retried."""
    from web import builder, jobs_pg as jobs_db

    jobs_dir, output_dir = tmp_env
    jobs_db.enqueue(
        job_id="j1",
        job_type="capture_hash",
        playbook="x",
        cmd=["sleep", "30"],
        args={},
    )
    jobs_db.claim_next_job(worker_id="w")

    proc = MagicMock()
    proc.poll.side_effect = [None, 0, 0]

    row = jobs_db.get_job("j1")
    call_count = {"n": 0}
    real_touch = jobs_db.touch_heartbeat

    def flaky_touch(jid, worker_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("database is locked")
        return real_touch(jid, worker_id)

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()
    with patch("web.builder.subprocess.Popen", return_value=proc), patch(
        "web.builder.jobs_db.touch_heartbeat", side_effect=flaky_touch
    ):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="w",
            stop_event=stop,
            heartbeat_seconds=0.01,
        )
    assert jobs_db.get_job("j1")["status"] == "complete"


def test_builder_marks_osdeploy_run_failed_when_provision_dies_before_identity(
    tmp_env,
    pg_conn,
):
    from web import builder, jobs_pg as jobs_db, osdeploy_pg

    jobs_dir, output_dir = tmp_env
    run = _create_osdeploy_run_for_builder_test(pg_conn)
    jobs_db.enqueue(
        job_id="job-osdeploy-preidentity-fail",
        job_type="provision_osdeploy",
        playbook="provision_proxmox_osdeploy.yml",
        cmd=["ansible-playbook", "provision_proxmox_osdeploy.yml"],
        args={"osdeploy_run_id": run["run_id"]},
    )
    row = jobs_db.claim_next_job(worker_id="w")
    log_path = jobs_dir / "job-osdeploy-preidentity-fail.log"
    _write_osdeploy_preidentity_failure_log(log_path)
    proc = MagicMock()
    proc.poll.return_value = 2

    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row,
            log_path=log_path,
            db_path=output_dir / "jobs.db",
            worker_id="w",
            stop_event=threading.Event(),
            heartbeat_seconds=0.01,
        )

    failed = osdeploy_pg.get_run(pg_conn, run["run_id"])
    readiness = osdeploy_pg.get_readiness(pg_conn, run["run_id"])
    events = osdeploy_pg.list_events(pg_conn, run["run_id"])
    ts_run = pg_conn.execute(
        "SELECT state, finished_at, last_error, vmid FROM ts_provisioning_runs WHERE id = %s",
        (run["run_id"],),
    ).fetchone()

    assert jobs_db.get_job("job-osdeploy-preidentity-fail")["status"] == "failed"
    assert failed["state"] == "failed"
    assert failed["vmid"] == 105
    assert readiness["state"] == "failed"
    assert readiness["agent_status"] == "failed"
    assert readiness["errors"][0]["job_id"] == "job-osdeploy-preidentity-fail"
    assert "storage 'isos' does not exist" in readiness["errors"][0]["message"]
    assert events[-1]["event_type"] == "provision_job_failed"
    assert events[-1]["severity"] == "error"
    assert events[-1]["data"]["vmid"] == 105
    assert ts_run["state"] == "failed"
    assert ts_run["finished_at"] is not None
    assert ts_run["vmid"] == 105


def test_builder_startup_reconciles_existing_failed_osdeploy_jobs(tmp_env, pg_conn):
    from web import builder, jobs_pg as jobs_db, osdeploy_pg

    jobs_dir, _output_dir = tmp_env
    run = _create_osdeploy_run_for_builder_test(pg_conn)
    jobs_db.enqueue(
        job_id="job-osdeploy-old-fail",
        job_type="provision_osdeploy",
        playbook="provision_proxmox_osdeploy.yml",
        cmd=["ansible-playbook", "provision_proxmox_osdeploy.yml"],
        args={"osdeploy_run_id": run["run_id"]},
    )
    jobs_db.claim_next_job(worker_id="w")
    jobs_db.finalize_job("job-osdeploy-old-fail", exit_code=2)
    _write_osdeploy_preidentity_failure_log(jobs_dir / "job-osdeploy-old-fail.log")

    assert osdeploy_pg.get_run(pg_conn, run["run_id"])["state"] == "created"

    assert builder.reconcile_failed_related_deployments(jobs_dir=jobs_dir) == 1
    assert builder.reconcile_failed_related_deployments(jobs_dir=jobs_dir) == 0

    failed = osdeploy_pg.get_run(pg_conn, run["run_id"])
    events = [
        event
        for event in osdeploy_pg.list_events(pg_conn, run["run_id"])
        if event["event_type"] == "provision_job_failed"
    ]
    assert failed["state"] == "failed"
    assert failed["vmid"] == 105
    assert len(events) == 1
    assert events[0]["data"]["job_id"] == "job-osdeploy-old-fail"
