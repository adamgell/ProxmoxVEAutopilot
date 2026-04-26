"""PE-side step-by-step progress events (spec §6 checkin payload).

PE bootstrap POSTs one Checkin per manifest step. Idempotent on
(vm_uuid, step_id, timestamp) so retrying POSTs from a flaky PE network
don't duplicate. The Jobs page (Plan 4) will render rows from here as
per-step progress for a deployment job.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Checkin:
    vm_uuid: str
    step_id: str
    status: str           # 'starting' | 'ok' | 'error'
    timestamp: str        # ISO 8601 'Z'
    duration_sec: float
    log_tail: str
    error_message: str | None
    extra: dict = field(default_factory=dict)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS winpe_checkins (
    vm_uuid       TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    status        TEXT NOT NULL,
    duration_sec  REAL NOT NULL,
    log_tail      TEXT NOT NULL,
    error_message TEXT,
    extra_json    TEXT NOT NULL,
    PRIMARY KEY (vm_uuid, step_id, timestamp)
);

CREATE INDEX IF NOT EXISTS winpe_checkins_by_vm
    ON winpe_checkins(vm_uuid, timestamp);
"""


class WinpeCheckinDb:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def record(self, checkin: Checkin) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO winpe_checkins "
                "(vm_uuid, step_id, timestamp, status, duration_sec, "
                " log_tail, error_message, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkin.vm_uuid,
                    checkin.step_id,
                    checkin.timestamp,
                    checkin.status,
                    checkin.duration_sec,
                    checkin.log_tail,
                    checkin.error_message,
                    json.dumps(checkin.extra),
                ),
            )

    def list_for_vm(self, vm_uuid: str) -> list[Checkin]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT vm_uuid, step_id, status, timestamp, duration_sec, "
                "       log_tail, error_message, extra_json "
                "FROM winpe_checkins WHERE vm_uuid = ? ORDER BY timestamp",
                (vm_uuid,),
            ).fetchall()
        return [
            Checkin(
                vm_uuid=r[0],
                step_id=r[1],
                status=r[2],
                timestamp=r[3],
                duration_sec=r[4],
                log_tail=r[5],
                error_message=r[6],
                extra=json.loads(r[7]),
            )
            for r in rows
        ]
