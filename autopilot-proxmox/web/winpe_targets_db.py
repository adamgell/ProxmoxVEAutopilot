"""Per-VM target records for the WinPE-driven OSD pipeline (spec §8).

Each row links a VM (by SMBIOS UUID) to (a) which install.wim it should
boot into, (b) which template/sequence renders its per-VM artifacts,
(c) the params the renderer needs (computer name, OEM profile, etc.).

Lives in `var/artifacts/index.db` next to the artifact-store table —
single sqlite file for the whole pipeline.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class UnknownVmError(KeyError):
    pass


@dataclass(frozen=True)
class WinpeTarget:
    vm_uuid: str
    install_wim_sha: str
    template_id: str
    params: dict
    created_at: str
    last_manifest_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS winpe_targets (
    vm_uuid          TEXT PRIMARY KEY,
    install_wim_sha  TEXT NOT NULL,
    template_id      TEXT NOT NULL,
    params_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    last_manifest_at TEXT
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WinpeTargetsDb:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def register(
        self,
        *,
        vm_uuid: str,
        install_wim_sha: str,
        template_id: str,
        params: dict,
    ) -> None:
        """Upsert a target. Replaces all fields on the row if vm_uuid already exists."""
        now = _utc_now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO winpe_targets "
                "(vm_uuid, install_wim_sha, template_id, params_json, created_at, last_manifest_at) "
                "VALUES (?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(vm_uuid) DO UPDATE SET "
                "  install_wim_sha = excluded.install_wim_sha,"
                "  template_id     = excluded.template_id,"
                "  params_json     = excluded.params_json",
                (vm_uuid, install_wim_sha, template_id, json.dumps(params), now),
            )

    def lookup(self, vm_uuid: str) -> WinpeTarget | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT vm_uuid, install_wim_sha, template_id, params_json, "
                "       created_at, last_manifest_at "
                "FROM winpe_targets WHERE vm_uuid = ?",
                (vm_uuid,),
            ).fetchone()
        if row is None:
            return None
        return WinpeTarget(
            vm_uuid=row[0],
            install_wim_sha=row[1],
            template_id=row[2],
            params=json.loads(row[3]),
            created_at=row[4],
            last_manifest_at=row[5],
        )

    def list_uuids(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT vm_uuid FROM winpe_targets").fetchall()
        return [r[0] for r in rows]

    def touch_last_manifest_at(self, vm_uuid: str) -> None:
        """Mark that the manifest endpoint just served this VM. Raises UnknownVmError if missing."""
        now = _utc_now()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE winpe_targets SET last_manifest_at = ? WHERE vm_uuid = ?",
                (now, vm_uuid),
            )
            if cur.rowcount == 0:
                raise UnknownVmError(vm_uuid)
