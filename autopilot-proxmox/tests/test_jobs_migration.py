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
