from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_collect_deployment_timings_from_jobs_winpe_and_agent_work(pg_conn):
    from web import agent_telemetry_pg, deployment_health, jobs_pg, sequences_pg

    deployment_health.reset_for_tests(pg_conn)
    sequences_pg.reset_for_tests(pg_conn)
    sequences_pg.init(pg_conn)
    agent_telemetry_pg.reset_for_tests(pg_conn)
    agent_telemetry_pg.init(pg_conn)

    jobs_pg.enqueue(
        job_id="job-build",
        job_type="build_template",
        playbook="build.yml",
        cmd=["true"],
        args={"vm_vmid": 110},
    )
    jobs_pg.claim_next_job(worker_id="builder-1")
    jobs_pg.finalize_job("job-build", exit_code=0)

    seq_id = sequences_pg.create_sequence(
        None,
        name="WinPE deploy",
        description="",
        steps=[],
    )
    run_id = sequences_pg.create_provisioning_run(
        None,
        sequence_id=seq_id,
        provision_path="winpe",
    )
    step = sequences_pg.append_run_step(
        None,
        run_id=run_id,
        phase="winpe",
        kind="apply_wim",
        params={},
    )
    sequences_pg.update_run_step_state(None, step_id=step["id"], state="running")
    sequences_pg.update_run_step_state(None, step_id=step["id"], state="ok")
    sequences_pg.update_provisioning_run_state(None, run_id=run_id, state="done")

    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="agent-build",
        token="agent-token",
        vmid=1200,
        computer_name="PVE-BUILD",
    )
    work = agent_telemetry_pg.create_work_item(
        pg_conn,
        agent_id="agent-build",
        kind="build_winpe",
        request={"download_url": "https://download.microsoft.com/Win11.iso"},
        vmid=1200,
    )
    claimed = agent_telemetry_pg.claim_next_work_item(
        pg_conn,
        agent_id="agent-build",
        supported_kinds=["build_winpe"],
    )
    assert claimed["id"] == work["id"]
    agent_telemetry_pg.complete_work_item(
        pg_conn,
        work["id"],
        agent_id="agent-build",
        result={"artifact": "winpe.iso"},
    )

    payload = deployment_health.build_deployments_payload(pg_conn)
    keys = {row["deployment_key"] for row in payload["runs"]}

    assert "job:job-build" in keys
    assert f"winpe:{run_id}" in keys
    assert f"agent-work:{work['id']}" in keys
    assert payload["summary"]["total"] >= 3
    assert payload["summary"]["completed"] >= 3
    assert payload["summary"]["failed"] == 0
    assert all("download_url" not in str(row["evidence"]) for row in payload["runs"])


def test_deployment_detail_flags_stuck_phase(pg_conn):
    from web import deployment_health, deployment_health_pg

    deployment_health.reset_for_tests(pg_conn)
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    deployment_health_pg.record_phase(
        pg_conn,
        deployment_key="job:stuck",
        deployment_type="provision_clone",
        source="jobs",
        source_id="stuck",
        phase_key="execution",
        phase_label="Execution",
        state="running",
        started_at=started,
        last_progress_at=started,
    )

    detail = deployment_health.build_deployment_detail(pg_conn, "job:stuck")

    assert detail["deployment_key"] == "job:stuck"
    assert detail["health"] == "stuck"
    assert detail["phases"][0]["health"] == "stuck"


def test_cloudosd_timeline_backfills_run_and_readiness_phases(pg_conn):
    from web import cloudosd_pg, deployment_health, ts_engine_pg

    deployment_health.reset_for_tests(pg_conn)
    cloudosd_pg.reset_for_tests(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    cloudosd_pg.init(pg_conn)
    artifact = cloudosd_pg.create_artifact(
        pg_conn,
        architecture="amd64",
        osdcloud_module_version="26.4.17.1",
        build_sha="abc1234",
        iso_path="/app/output/cloudosd.iso",
        wim_path="/app/output/cloudosd.wim",
        manifest_path="/app/output/cloudosd.json",
        iso_sha256="a" * 64,
        wim_sha256="b" * 64,
        built_by_host="build-host",
    )
    run = cloudosd_pg.create_run(
        pg_conn,
        artifact_id=artifact["id"],
        vm_name="GELL-MONITOR-01",
        node="pve1",
        storage="local-lvm",
        network_bridge="vmbr0",
    )
    cloudosd_pg.mark_pe_registered(pg_conn, run_id=run["run_id"])
    cloudosd_pg.mark_osdcloud_started(pg_conn, run_id=run["run_id"])
    cloudosd_pg.mark_osdcloud_finished(pg_conn, run_id=run["run_id"])
    cloudosd_pg.mark_complete_from_heartbeat(
        pg_conn,
        run_id=run["run_id"],
        heartbeat_at=datetime.now(timezone.utc),
        heartbeat={"ComputerName": "GELL-MONITOR-01"},
    )
    upload_started = datetime.now(timezone.utc) - timedelta(seconds=45)
    cloudosd_pg.upsert_autopilot_readiness(
        pg_conn,
        run_id=run["run_id"],
        state="ready",
        hash_status="uploaded",
        upload_status="complete",
        upload_started_at=upload_started,
        upload_finished_at=upload_started + timedelta(seconds=30),
        assignment_status="assigned",
        enrollment_status="enrolled",
        contact_state="contacted",
    )

    detail = deployment_health.build_deployment_detail(
        pg_conn,
        f"cloudosd:{run['run_id']}",
    )
    phase_keys = {phase["phase_key"] for phase in detail["phases"]}

    assert detail["deployment_type"] == "cloudosd"
    assert {
        "proxmox_provision",
        "osdcloud",
        "first_boot",
        "hash_upload",
    }.issubset(phase_keys)
    assert detail["evidence"]["assignment_status"] == "assigned"


def test_task_engine_steps_feed_deployment_payload(pg_conn):
    from web import deployment_health, ts_engine_pg

    deployment_health.reset_for_tests(pg_conn)
    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Task engine deploy")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Apply OS",
        kind="apply_os_image",
        phase="winpe",
        position=0,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={"vmid": 140},
    )
    now = datetime.now(timezone.utc)
    pg_conn.execute(
        """
        UPDATE ts_run_plan_steps
        SET state='done', started_at=%s, finished_at=%s
        WHERE run_id=%s
        """,
        (now - timedelta(seconds=60), now, run_id),
    )
    pg_conn.execute(
        "UPDATE ts_provisioning_runs SET state='done', finished_at=%s WHERE id=%s",
        (now, run_id),
    )
    pg_conn.commit()

    detail = deployment_health.build_deployment_detail(pg_conn, f"ts:{run_id}")

    assert detail["deployment_type"] == "task_engine"
    assert detail["state"] == "done"
    assert "partition_apply_wim" in {phase["phase_key"] for phase in detail["phases"]}


def test_live_recent_ts_runs_reads_finished_at(pg_conn):
    from web import app as app_module, ts_engine_pg

    ts_engine_pg.reset_for_tests(pg_conn)
    ts_engine_pg.init(pg_conn)
    sequence_id = ts_engine_pg.create_sequence(pg_conn, name="Live TS")
    ts_engine_pg.add_step(
        pg_conn,
        sequence_id=sequence_id,
        parent_id=None,
        name="Apply OS",
        kind="apply_os_image",
        phase="winpe",
        position=0,
    )
    version_id = ts_engine_pg.compile_sequence(pg_conn, sequence_id)
    run_id = ts_engine_pg.create_run_from_version(
        pg_conn,
        sequence_version_id=version_id,
        deployment_target={"vmid": 130},
    )
    pg_conn.execute(
        "UPDATE ts_provisioning_runs SET state='done', finished_at=now() WHERE id=%s",
        (run_id,),
    )
    pg_conn.commit()

    rows = app_module._live_recent_ts_runs()

    row = next(item for item in rows if item["id"] == run_id)
    assert row["completed_at"]
    assert row["finished_at"] == row["completed_at"]
