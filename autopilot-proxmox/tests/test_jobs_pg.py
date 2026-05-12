from __future__ import annotations


def test_claim_respects_per_type_cap(pg_conn):
    from web import jobs_pg

    jobs_pg.init(pg_conn)
    jobs_pg.enqueue(
        job_id="j1",
        job_type="build_template",
        playbook="build.yml",
        cmd=["ansible-playbook", "build.yml"],
        args={"vmid": 101},
    )
    jobs_pg.enqueue(
        job_id="j2",
        job_type="build_template",
        playbook="build.yml",
        cmd=["ansible-playbook", "build.yml"],
        args={"vmid": 102},
    )

    first = jobs_pg.claim_next_job(worker_id="builder-1")
    second = jobs_pg.claim_next_job(worker_id="builder-2")

    assert first["id"] == "j1"
    assert second is None


def test_claim_skips_capped_type_and_claims_other_pending_job(pg_conn):
    from web import jobs_pg

    jobs_pg.enqueue(
        job_id="b1",
        job_type="build_template",
        playbook="build.yml",
        cmd=[],
        args={},
    )
    jobs_pg.enqueue(
        job_id="p1",
        job_type="provision_clone",
        playbook="provision.yml",
        cmd=[],
        args={},
    )

    jobs_pg.claim_next_job(worker_id="builder-1")
    second = jobs_pg.claim_next_job(worker_id="builder-2")

    assert second["id"] == "p1"


def test_finalize_kill_heartbeat_and_reap_behaviors(pg_conn):
    from datetime import datetime, timedelta, timezone

    from web import jobs_pg

    jobs_pg.enqueue(
        job_id="j1",
        job_type="hash_capture",
        playbook="capture.yml",
        cmd=["true"],
        args={},
    )
    jobs_pg.claim_next_job(worker_id="worker-a")

    assert jobs_pg.touch_heartbeat("j1", "wrong-worker") == 0
    assert jobs_pg.touch_heartbeat("j1", "worker-a") == 1
    jobs_pg.request_kill("j1")
    assert jobs_pg.get_job("j1")["kill_requested"] is True
    assert jobs_pg.finalize_job("j1", exit_code=0) == 1
    row = jobs_pg.get_job("j1")
    assert row["status"] == "complete"
    assert row["exit_code"] == 0
    assert row["kill_requested"] is False

    jobs_pg.enqueue(
        job_id="j2",
        job_type="hash_capture",
        playbook="capture.yml",
        cmd=["true"],
        args={},
    )
    jobs_pg.claim_next_job(worker_id="worker-b")
    stale = datetime.now(timezone.utc) - timedelta(seconds=600)
    pg_conn.execute(
        "UPDATE jobs SET last_heartbeat=%s WHERE id=%s",
        (stale, "j2"),
    )
    pg_conn.commit()
    assert jobs_pg.reap_stale_running_jobs(older_than_seconds=300) == 1
    assert jobs_pg.get_job("j2")["status"] == "orphaned"
    assert jobs_pg.finalize_job("j2", exit_code=0) == 0


def test_limits_can_be_listed_and_updated(pg_conn):
    from web import jobs_pg

    caps = {r["job_type"]: r["max_concurrent"] for r in jobs_pg.list_job_type_limits()}
    assert caps["build_template"] == 1

    row = jobs_pg.update_job_type_limit("build_template", 2)

    assert row == {"job_type": "build_template", "max_concurrent": 2}
    caps = {r["job_type"]: r["max_concurrent"] for r in jobs_pg.list_job_type_limits()}
    assert caps["build_template"] == 2


def test_cloudosd_limit_is_migrated_to_current_default(pg_conn):
    from web import jobs_pg

    jobs_pg.update_job_type_limit("provision_cloudosd", 2)

    jobs_pg.init(pg_conn)

    caps = {r["job_type"]: r["max_concurrent"] for r in jobs_pg.list_job_type_limits()}
    assert caps["provision_cloudosd"] == 4


def test_complete_interrupted_winpe_jobs_for_run(pg_conn):
    from web import jobs_pg

    for job_id, run_id, exit_code in [
        ("interrupted", 42, -15),
        ("other-run", 99, -15),
        ("real-failure", 42, 2),
    ]:
        jobs_pg.enqueue(
            job_id=job_id,
            job_type="provision_winpe",
            playbook="provision.yml",
            cmd=[],
            args={"run_id": run_id},
        )
        jobs_pg.claim_next_job(worker_id=f"worker-{job_id}")
        jobs_pg.finalize_job(job_id, exit_code=exit_code)

    assert jobs_pg.complete_interrupted_provision_winpe_jobs_for_run(run_id=42) == 1
    assert jobs_pg.get_job("interrupted")["status"] == "complete"
    assert jobs_pg.get_job("other-run")["status"] == "failed"
    assert jobs_pg.get_job("real-failure")["status"] == "failed"
