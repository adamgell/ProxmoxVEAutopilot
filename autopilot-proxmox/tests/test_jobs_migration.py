import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_dirs():
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d) / "jobs"
        jobs_dir.mkdir()
        db_path = Path(d) / "jobs.db"
        yield jobs_dir, db_path


def test_migrate_noop_when_no_index_json(tmp_dirs):
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    n = jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    assert n == 0


def test_migrate_inserts_rows_and_renames_index(tmp_dirs):
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    index = [
        {"id": "20260420-aaaa", "playbook": "build_template",
         "status": "complete", "started": "2026-04-20T10:00:00+00:00",
         "ended": "2026-04-20T10:30:00+00:00", "exit_code": 0,
         "args": {"profile": "lenovo-t14"}},
        {"id": "20260420-bbbb", "playbook": "provision_clone",
         "status": "failed", "started": "2026-04-20T11:00:00+00:00",
         "ended": "2026-04-20T11:05:00+00:00", "exit_code": 2,
         "args": {"vm_count": 3}},
    ]
    (jobs_dir / "index.json").write_text(json.dumps(index))
    n = jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    assert n == 2
    got = {r["id"]: r for r in jobs_db.list_jobs(db_path)}
    assert got["20260420-aaaa"]["status"] == "complete"
    assert got["20260420-aaaa"]["exit_code"] == 0
    assert got["20260420-bbbb"]["args"]["vm_count"] == 3
    # index.json renamed to backup
    assert not (jobs_dir / "index.json").exists()
    assert (jobs_dir / "index.json.pre-split.bak").exists()


def test_migrate_idempotent_after_rename(tmp_dirs):
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    (jobs_dir / "index.json").write_text(json.dumps(
        [{"id": "x", "playbook": "y", "status": "complete",
          "started": "2026-04-20T10:00:00+00:00", "ended": None,
          "exit_code": 0, "args": {}}]
    ))
    jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    n2 = jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    assert n2 == 0


def test_migrate_marks_inflight_running_as_orphaned(tmp_dirs):
    """Old index.json may have 'running' rows at migration time (graceful
    shutdown mid-job). Those should land as 'orphaned' so operators see
    them, not appear live."""
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    (jobs_dir / "index.json").write_text(json.dumps(
        [{"id": "stuck", "playbook": "p", "status": "running",
          "started": "2026-04-20T10:00:00+00:00", "ended": None,
          "exit_code": None, "args": {}}]
    ))
    jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    got = jobs_db.get_job(db_path, "stuck")
    assert got["status"] == "orphaned"


def test_migrate_logs_warning_for_dropped_rows(tmp_dirs, caplog):
    """Partial-failure migration must log a WARNING naming the lost
    row IDs so operators can recover from the .pre-split.bak file."""
    import logging
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    (jobs_dir / "index.json").write_text(json.dumps([
        {"id": "good",
         "playbook": "p", "status": "complete",
         "started": "2026-04-20T10:00:00+00:00", "ended": None,
         "exit_code": 0, "args": {}},
        {"id": "bad",  # missing required field will fail _insert_migrated
         "playbook": "p", "status": "complete",
         # deliberately mangle to force an exception path — e.g. omit
         # "id" by making it None (and we'll check the code tolerates it)
         "started": None,  # non-string will fail SQLite bind OR pass through
         "ended": None, "exit_code": 0, "args": {}},
    ]))

    # Force the second row to raise by patching _insert_migrated.
    from unittest.mock import patch
    real_insert = jobs_db._insert_migrated

    def flaky_insert(db_path, *, job_id, **kwargs):
        if job_id == "bad":
            raise RuntimeError("simulated insert failure")
        return real_insert(db_path, job_id=job_id, **kwargs)

    with caplog.at_level(logging.WARNING, logger="web.jobs_migration"):
        with patch("web.jobs_migration.jobs_db._insert_migrated",
                   side_effect=flaky_insert):
            jobs_migration.migrate_legacy_index(
                jobs_dir=jobs_dir, db_path=db_path,
            )

    assert any(
        "dropped 1 of 2 rows" in r.message and "bad" in r.message
        for r in caplog.records
    ), f"expected dropped-rows WARNING, got: {[r.message for r in caplog.records]}"
    # Backup file still created so future boots no-op.
    assert (jobs_dir / "index.json.pre-split.bak").exists()
    # Good row still migrated.
    assert jobs_db.get_job(db_path, "good") is not None


def test_migrate_clean_run_logs_info_not_warning(tmp_dirs, caplog):
    """Happy path: no WARNING, just an INFO line."""
    import logging
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    (jobs_dir / "index.json").write_text(json.dumps([
        {"id": "a", "playbook": "p", "status": "complete",
         "started": "2026-04-20T10:00:00+00:00", "ended": None,
         "exit_code": 0, "args": {}},
    ]))
    with caplog.at_level(logging.INFO, logger="web.jobs_migration"):
        jobs_migration.migrate_legacy_index(
            jobs_dir=jobs_dir, db_path=db_path,
        )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"clean migration logged WARNINGs: {warnings}"
    assert any("migrated 1 jobs cleanly" in r.message
               for r in caplog.records)
