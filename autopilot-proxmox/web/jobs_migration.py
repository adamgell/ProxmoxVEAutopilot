"""One-shot migration from jobs/index.json to jobs.db.

Called from the web container's startup hook. After the first successful
run, the old index.json is renamed to index.json.pre-split.bak so future
boots are no-ops.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from web import jobs_db

_log = logging.getLogger(__name__)


def migrate_legacy_index(*, jobs_dir: Path, db_path: Path) -> int:
    """Read jobs_dir/index.json, insert into jobs.db, rename the file.
    Returns count of migrated rows. Idempotent after the rename.

    Partial-failure semantics: per-row exceptions are caught and the
    other rows still migrate. The file is renamed to .pre-split.bak
    regardless so future boots no-op, but any dropped IDs are logged
    at WARNING level with the backup path so operators can recover
    them offline.
    """
    index_path = Path(jobs_dir) / "index.json"
    if not index_path.exists():
        return 0
    try:
        entries = json.loads(index_path.read_text())
    except Exception:
        _log.exception("failed to read %s; skipping migration", index_path)
        return 0
    total = len(entries)
    inserted = 0
    failed_ids: list[str] = []
    for entry in entries:
        status = entry.get("status")
        if status == "running":
            # Running at migration = subprocess died with the old web.
            # Mark orphaned so operators see what happened.
            status = "orphaned"
        job_type = entry.get("playbook") or "unknown"
        playbook_path = entry.get("playbook") or "unknown"
        try:
            jobs_db._insert_migrated(
                db_path,
                job_id=entry["id"],
                job_type=job_type,
                playbook=playbook_path,
                args=entry.get("args") or {},
                status=status,
                started_at=entry.get("started") or entry.get("started_at", ""),
                ended_at=entry.get("ended"),
                exit_code=entry.get("exit_code"),
            )
            inserted += 1
        except Exception:
            _log.exception("failed to migrate job %r", entry.get("id"))
            failed_ids.append(entry.get("id") or "<unknown>")
    backup = index_path.with_suffix(".json.pre-split.bak")
    index_path.rename(backup)
    if failed_ids:
        _log.warning(
            "jobs migration dropped %d of %d rows: %s \u2014 "
            "original data preserved at %s (grep by id to recover)",
            len(failed_ids), total, failed_ids, backup,
        )
    else:
        _log.info(
            "migrated %d jobs cleanly; legacy index backed up to %s",
            inserted, backup,
        )
    return inserted
