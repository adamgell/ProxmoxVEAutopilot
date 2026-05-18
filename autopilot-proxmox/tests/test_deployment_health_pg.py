from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_phase_recording_is_idempotent_and_computes_duration(pg_conn):
    from web import deployment_health_pg

    deployment_health_pg.reset_for_tests(pg_conn)
    deployment_health_pg.init(pg_conn)
    started = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=90)

    deployment_health_pg.start_phase(
        pg_conn,
        deployment_key="job:j1",
        deployment_type="build_template",
        source="jobs",
        source_id="j1",
        phase_key="execution",
        phase_label="Execution",
        started_at=started,
        evidence={"job_id": "j1", "token": "must-not-render"},
    )
    deployment_health_pg.start_phase(
        pg_conn,
        deployment_key="job:j1",
        deployment_type="build_template",
        source="jobs",
        source_id="j1",
        phase_key="execution",
        phase_label="Execution",
        started_at=started + timedelta(seconds=5),
        evidence={"job_id": "j1", "extra": "kept"},
    )
    deployment_health_pg.end_phase(
        pg_conn,
        deployment_key="job:j1",
        phase_key="execution",
        ended_at=ended,
        evidence={"exit_code": 0},
    )

    phases = deployment_health_pg.list_phases(pg_conn, "job:j1")
    assert len(phases) == 1
    assert phases[0]["state"] == "done"
    assert phases[0]["started_at"] == started.isoformat()
    assert phases[0]["ended_at"] == ended.isoformat()
    assert phases[0]["duration_seconds"] == 90
    assert phases[0]["evidence"]["job_id"] == "j1"
    assert phases[0]["evidence"]["extra"] == "kept"
    assert phases[0]["evidence"]["exit_code"] == 0
    assert "token" not in phases[0]["evidence"]


def test_baselines_are_learning_until_five_successful_samples(pg_conn):
    from web import deployment_health_pg

    deployment_health_pg.reset_for_tests(pg_conn)
    deployment_health_pg.init(pg_conn)
    base = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)

    for idx, duration in enumerate([10, 20, 30, 40]):
        deployment_health_pg.record_phase(
            pg_conn,
            deployment_key=f"job:learning-{idx}",
            deployment_type="provision_clone",
            source="jobs",
            source_id=f"learning-{idx}",
            phase_key="execution",
            phase_label="Execution",
            state="done",
            started_at=base + timedelta(minutes=idx),
            ended_at=base + timedelta(minutes=idx, seconds=duration),
        )

    baselines = deployment_health_pg.recompute_baselines(pg_conn)
    execution = next(row for row in baselines if row["phase_key"] == "execution")
    assert execution["sample_count"] == 4
    assert execution["health"] == "learning"

    deployment_health_pg.record_phase(
        pg_conn,
        deployment_key="job:ready",
        deployment_type="provision_clone",
        source="jobs",
        source_id="ready",
        phase_key="execution",
        phase_label="Execution",
        state="done",
        started_at=base + timedelta(minutes=10),
        ended_at=base + timedelta(minutes=10, seconds=50),
    )
    baselines = deployment_health_pg.recompute_baselines(pg_conn)
    execution = next(row for row in baselines if row["phase_key"] == "execution")

    assert execution["sample_count"] == 5
    assert execution["health"] == "healthy"
    assert execution["p50_seconds"] == 30
    assert execution["p95_seconds"] == 50


def test_terminal_source_truth_can_recover_failed_phase(pg_conn):
    from web import deployment_health_pg

    deployment_health_pg.reset_for_tests(pg_conn)
    deployment_health_pg.init(pg_conn)
    started = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)

    deployment_health_pg.record_phase(
        pg_conn,
        deployment_key="cloudosd:run-1",
        deployment_type="cloudosd",
        source="cloudosd_autopilot_readiness",
        source_id="run-1",
        phase_key="hash_upload",
        phase_label="Hash upload",
        state="failed",
        started_at=started,
        ended_at=started + timedelta(seconds=30),
        error="Missing Entra credentials",
    )
    deployment_health_pg.record_phase(
        pg_conn,
        deployment_key="cloudosd:run-1",
        deployment_type="cloudosd",
        source="cloudosd_autopilot_readiness",
        source_id="run-1",
        phase_key="hash_upload",
        phase_label="Hash upload",
        state="skipped",
        started_at=started,
        ended_at=started + timedelta(seconds=45),
        evidence={"upload_status": "not_configured"},
    )

    phase = deployment_health_pg.list_phases(pg_conn, "cloudosd:run-1")[0]

    assert phase["state"] == "skipped"
    assert phase["error"] is None
