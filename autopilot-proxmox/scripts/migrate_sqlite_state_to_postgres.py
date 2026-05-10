#!/usr/bin/env python3
"""One-time SQLite state repair for the Postgres runtime migration.

Runtime code is intentionally Postgres-only. This operator script reads the
legacy ``output/*.db`` files when they are still present, imports the state into
Postgres idempotently, and preserves existing newer Postgres rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.crypto import Cipher  # noqa: E402
from web import device_history_pg, devices_pg, sequences_pg  # noqa: E402


CREDENTIAL_REF_KEYS = {
    "credential_id",
    "ad_credential_id",
    "domain_join_credential_id",
    "local_admin_credential_id",
    "mde_onboarding_credential_id",
}


class _DryRunRollback(Exception):
    """Internal sentinel used to roll back a dry-run transaction."""


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _blob(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value)


def _credential_rewrapper(
    legacy_key: Path | None,
    target_key: Path | None,
) -> Callable[[bytes], bytes] | None:
    if legacy_key is None:
        return None
    if target_key is None:
        raise ValueError("--target-credential-key is required with --legacy-credential-key")
    legacy_cipher = Cipher(legacy_key)
    target_cipher = Cipher(target_key)

    def rewrap(blob: bytes) -> bytes:
        payload = legacy_cipher.decrypt_json(blob)
        return target_cipher.encrypt_json(payload)

    return rewrap


def _sqlite_rows(db_path: Path, table: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return []
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def _used_ids(conn: Connection, table: str) -> set[int]:
    rows = conn.execute(f"SELECT id FROM {table}").fetchall()
    return {int(row["id"]) for row in rows}


def _next_id(conn: Connection, table: str, legacy_ids: Iterable[int]) -> int:
    legacy_max = max([0, *[int(i or 0) for i in legacy_ids]])
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}").fetchone()
    return max(int(row["max_id"] or 0), legacy_max) + 1


def _choose_id(row_id: int | None, used: set[int], next_ref: list[int]) -> int:
    candidate = int(row_id or 0)
    if candidate > 0 and candidate not in used:
        used.add(candidate)
        return candidate
    while next_ref[0] in used:
        next_ref[0] += 1
    chosen = next_ref[0]
    used.add(chosen)
    next_ref[0] += 1
    return chosen


def _sync_sequence(conn: Connection, table: str, column: str = "id") -> None:
    conn.execute(
        """
        SELECT setval(
            pg_get_serial_sequence(%s, %s),
            GREATEST((SELECT COALESCE(MAX(id), 1) FROM """ + table + """), 1),
            true
        )
        """,
        (table, column),
    )


def _remap_credential_refs(value: Any, credential_map: dict[int, int]) -> Any:
    if isinstance(value, list):
        return [_remap_credential_refs(item, credential_map) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in CREDENTIAL_REF_KEYS:
            try:
                old_id = int(item)
            except (TypeError, ValueError):
                out[key] = item
            else:
                out[key] = credential_map.get(old_id, old_id)
        else:
            out[key] = _remap_credential_refs(item, credential_map)
    return out


def _record(summary: dict[str, dict[str, int]], area: str, key: str, amount: int = 1) -> None:
    summary.setdefault(area, {}).setdefault(key, 0)
    summary[area][key] += amount


def _migrate_credentials(
    conn: Connection,
    rows: list[dict[str, Any]],
    summary: dict[str, dict[str, int]],
    rewrap_credential: Callable[[bytes], bytes] | None = None,
) -> dict[int, int]:
    credential_map: dict[int, int] = {}
    used = _used_ids(conn, "credentials")
    next_ref = [_next_id(conn, "credentials", (row.get("id") for row in rows))]
    by_name = {
        row["name"]: int(row["id"])
        for row in conn.execute("SELECT id, name FROM credentials").fetchall()
    }

    for row in rows:
        old_id = int(row["id"])
        encrypted_blob = _blob(row.get("encrypted_blob"))
        if rewrap_credential is not None:
            encrypted_blob = rewrap_credential(encrypted_blob)
        existing_id = by_name.get(str(row["name"]))
        if existing_id:
            conn.execute(
                """
                UPDATE credentials
                SET type = %s, encrypted_blob = %s, updated_at = COALESCE(%s, updated_at)
                WHERE id = %s
                """,
                (
                    row.get("type") or "local_admin",
                    encrypted_blob,
                    _dt(row.get("updated_at")),
                    existing_id,
                ),
            )
            credential_map[old_id] = existing_id
            _record(summary, "sequences", "credentials_updated")
            continue

        new_id = _choose_id(old_id, used, next_ref)
        conn.execute(
            """
            INSERT INTO credentials
                (id, name, type, encrypted_blob, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                new_id,
                row.get("name") or f"legacy-{old_id}",
                row.get("type") or "local_admin",
                encrypted_blob,
                _dt(row.get("created_at")) or datetime.now(timezone.utc),
                _dt(row.get("updated_at")) or datetime.now(timezone.utc),
            ),
        )
        by_name[str(row["name"])] = new_id
        credential_map[old_id] = new_id
        _record(summary, "sequences", "credentials_inserted")

    if rows:
        _sync_sequence(conn, "credentials")
    return credential_map


def _migrate_sequences(
    conn: Connection,
    rows: list[dict[str, Any]],
    step_rows: list[dict[str, Any]],
    credential_map: dict[int, int],
    summary: dict[str, dict[str, int]],
) -> dict[int, int]:
    sequence_map: dict[int, int] = {}
    used = _used_ids(conn, "task_sequences")
    next_ref = [_next_id(conn, "task_sequences", (row.get("id") for row in rows))]
    by_name = {
        row["name"]: int(row["id"])
        for row in conn.execute("SELECT id, name FROM task_sequences").fetchall()
    }

    for row in rows:
        old_id = int(row["id"])
        existing_id = by_name.get(str(row["name"]))
        if existing_id:
            sequence_map[old_id] = existing_id
            conn.execute(
                """
                UPDATE task_sequences
                SET description = %s,
                    is_default = %s,
                    produces_autopilot_hash = %s,
                    target_os = %s,
                    hash_capture_phase = %s,
                    updated_at = COALESCE(%s, updated_at)
                WHERE id = %s
                """,
                (
                    row.get("description") or "",
                    _bool(row.get("is_default")),
                    _bool(row.get("produces_autopilot_hash")),
                    row.get("target_os") or "windows",
                    row.get("hash_capture_phase") or "oobe",
                    _dt(row.get("updated_at")),
                    existing_id,
                ),
            )
            conn.execute("DELETE FROM task_sequence_steps WHERE sequence_id = %s", (existing_id,))
            _record(summary, "sequences", "task_sequences_updated")
            continue

        new_id = _choose_id(old_id, used, next_ref)
        conn.execute(
            """
            INSERT INTO task_sequences
                (id, name, description, is_default, produces_autopilot_hash,
                 target_os, hash_capture_phase, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_id,
                row.get("name") or f"legacy-sequence-{old_id}",
                row.get("description") or "",
                _bool(row.get("is_default")),
                _bool(row.get("produces_autopilot_hash")),
                row.get("target_os") or "windows",
                row.get("hash_capture_phase") or "oobe",
                _dt(row.get("created_at")) or datetime.now(timezone.utc),
                _dt(row.get("updated_at")) or datetime.now(timezone.utc),
            ),
        )
        by_name[str(row["name"])] = new_id
        sequence_map[old_id] = new_id
        _record(summary, "sequences", "task_sequences_inserted")

    for row in sorted(step_rows, key=lambda r: (int(r.get("sequence_id") or 0), int(r.get("order_index") or 0))):
        target_sequence = sequence_map.get(int(row.get("sequence_id") or 0))
        if not target_sequence:
            continue
        params = _remap_credential_refs(_json(row.get("params_json"), {}), credential_map)
        conn.execute(
            """
            INSERT INTO task_sequence_steps
                (sequence_id, order_index, step_type, params_json, enabled)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (sequence_id, order_index) DO UPDATE
            SET step_type = EXCLUDED.step_type,
                params_json = EXCLUDED.params_json,
                enabled = EXCLUDED.enabled
            """,
            (
                target_sequence,
                int(row.get("order_index") or 0),
                row.get("step_type") or "",
                Jsonb(params),
                _bool(row.get("enabled", 1)),
            ),
        )
        _record(summary, "sequences", "task_sequence_steps_upserted")

    if rows:
        _sync_sequence(conn, "task_sequences")
    if step_rows:
        _sync_sequence(conn, "task_sequence_steps")
    return sequence_map


def _migrate_sequence_state(
    conn: Connection,
    output_dir: Path,
    sequence_map: dict[int, int],
    credential_map: dict[int, int],
    summary: dict[str, dict[str, int]],
) -> None:
    db_path = output_dir / "sequences.db"

    for row in _sqlite_rows(db_path, "vm_provisioning"):
        seq_id = sequence_map.get(int(row.get("sequence_id") or 0))
        conn.execute(
            """
            INSERT INTO vm_provisioning (vmid, sequence_id, provisioned_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (vmid) DO UPDATE
            SET sequence_id = EXCLUDED.sequence_id,
                provisioned_at = EXCLUDED.provisioned_at
            """,
            (
                int(row["vmid"]),
                seq_id,
                _dt(row.get("provisioned_at")) or datetime.now(timezone.utc),
            ),
        )
        _record(summary, "sequences", "vm_provisioning_upserted")

    for row in _sqlite_rows(db_path, "answer_iso_cache"):
        conn.execute(
            """
            INSERT INTO answer_iso_cache
                (hash, short_hash, volid, compiled_at, last_used_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (hash) DO UPDATE
            SET short_hash = EXCLUDED.short_hash,
                volid = EXCLUDED.volid,
                compiled_at = EXCLUDED.compiled_at,
                last_used_at = EXCLUDED.last_used_at
            """,
            (
                row.get("hash") or "",
                row.get("short_hash") or "",
                row.get("volid") or "",
                _dt(row.get("compiled_at")) or datetime.now(timezone.utc),
                _dt(row.get("last_used_at")),
            ),
        )
        _record(summary, "sequences", "answer_iso_cache_upserted")

    run_rows = _sqlite_rows(db_path, "provisioning_runs")
    used_runs = _used_ids(conn, "provisioning_runs")
    next_run = [_next_id(conn, "provisioning_runs", (row.get("id") for row in run_rows))]
    run_map: dict[int, int] = {}
    new_run_ids: set[int] = set()
    for row in sorted(run_rows, key=lambda r: int(r.get("id") or 0)):
        old_id = int(row["id"])
        started_at = _dt(row.get("started_at")) or datetime.now(timezone.utc)
        vm_uuid = (row.get("vm_uuid") or "").strip().lower() or None
        existing = None
        if vm_uuid:
            existing = conn.execute(
                """
                SELECT id FROM provisioning_runs
                WHERE lower(COALESCE(vm_uuid, '')) = %s AND started_at = %s
                LIMIT 1
                """,
                (vm_uuid, started_at),
            ).fetchone()
        if existing is None:
            existing = conn.execute(
                """
                SELECT id FROM provisioning_runs
                WHERE vmid IS NOT DISTINCT FROM %s
                  AND started_at = %s
                  AND state = %s
                LIMIT 1
                """,
                (row.get("vmid"), started_at, row.get("state") or ""),
            ).fetchone()
        if existing:
            run_map[old_id] = int(existing["id"])
            _record(summary, "sequences", "provisioning_runs_existing")
            continue

        seq_id = sequence_map.get(int(row.get("sequence_id") or 0))
        if not seq_id:
            _record(summary, "sequences", "provisioning_runs_skipped_missing_sequence")
            continue
        new_id = _choose_id(old_id, used_runs, next_run)
        conn.execute(
            """
            INSERT INTO provisioning_runs
                (id, vmid, sequence_id, provision_path, state, vm_uuid,
                 started_at, finished_at, last_error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_id,
                row.get("vmid"),
                seq_id,
                row.get("provision_path") or "winpe",
                row.get("state") or "unknown",
                vm_uuid,
                started_at,
                _dt(row.get("finished_at")),
                row.get("last_error"),
            ),
        )
        run_map[old_id] = new_id
        new_run_ids.add(new_id)
        _record(summary, "sequences", "provisioning_runs_inserted")

    for row in sorted(_sqlite_rows(db_path, "provisioning_run_steps"), key=lambda r: (int(r.get("run_id") or 0), int(r.get("order_index") or 0))):
        run_id = run_map.get(int(row.get("run_id") or 0))
        if not run_id or run_id not in new_run_ids:
            continue
        params = _remap_credential_refs(_json(row.get("params_json"), {}), credential_map)
        conn.execute(
            """
            INSERT INTO provisioning_run_steps
                (run_id, order_index, phase, kind, params_json, state,
                 started_at, finished_at, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, order_index) DO NOTHING
            """,
            (
                run_id,
                int(row.get("order_index") or 0),
                row.get("phase") or "",
                row.get("kind") or "",
                Jsonb(params),
                row.get("state") or "pending",
                _dt(row.get("started_at")),
                _dt(row.get("finished_at")),
                row.get("error"),
            ),
        )
        _record(summary, "sequences", "provisioning_run_steps_inserted")

    if run_rows:
        _sync_sequence(conn, "provisioning_runs")
    if _sqlite_rows(db_path, "provisioning_run_steps"):
        _sync_sequence(conn, "provisioning_run_steps")


def _migrate_device_monitor(
    conn: Connection,
    output_dir: Path,
    credential_map: dict[int, int],
    summary: dict[str, dict[str, int]],
    *,
    include_history: bool,
) -> None:
    db_path = output_dir / "device_monitor.db"
    settings = _sqlite_rows(db_path, "monitoring_settings")
    if settings:
        row = settings[0]
        old_cred = int(row.get("ad_credential_id") or 0)
        conn.execute(
            """
            UPDATE monitoring_settings
            SET enabled = %s,
                interval_seconds = %s,
                ad_credential_id = %s,
                updated_at = %s
            WHERE id = 1
            """,
            (
                _bool(row.get("enabled", 1)),
                int(row.get("interval_seconds") or 900),
                credential_map.get(old_cred, old_cred),
                _dt(row.get("updated_at")) or datetime.now(timezone.utc),
            ),
        )
        _record(summary, "monitoring", "settings_updated")

    for row in _sqlite_rows(db_path, "monitoring_search_ous"):
        conn.execute(
            """
            INSERT INTO monitoring_search_ous
                (dn, label, enabled, sort_order, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (dn) DO UPDATE
            SET label = EXCLUDED.label,
                enabled = EXCLUDED.enabled,
                sort_order = EXCLUDED.sort_order,
                updated_at = EXCLUDED.updated_at
            """,
            (
                row.get("dn") or "",
                row.get("label") or "",
                _bool(row.get("enabled", 1)),
                int(row.get("sort_order") or 0),
                _dt(row.get("created_at")) or datetime.now(timezone.utc),
                _dt(row.get("updated_at")) or datetime.now(timezone.utc),
            ),
        )
        _record(summary, "monitoring", "search_ous_upserted")

    if not include_history:
        return

    sweep_rows = _sqlite_rows(db_path, "monitoring_sweeps")
    used_sweeps = _used_ids(conn, "monitoring_sweeps")
    next_sweep = [_next_id(conn, "monitoring_sweeps", (row.get("id") for row in sweep_rows))]
    sweep_map: dict[int, int] = {}
    new_sweep_ids: set[int] = set()
    for row in sorted(sweep_rows, key=lambda r: int(r.get("id") or 0)):
        old_id = int(row["id"])
        started_at = _dt(row.get("started_at")) or datetime.now(timezone.utc)
        existing = conn.execute(
            "SELECT id FROM monitoring_sweeps WHERE started_at = %s LIMIT 1",
            (started_at,),
        ).fetchone()
        if existing:
            sweep_map[old_id] = int(existing["id"])
            continue
        new_id = _choose_id(old_id, used_sweeps, next_sweep)
        conn.execute(
            """
            INSERT INTO monitoring_sweeps
                (id, started_at, ended_at, vm_count, errors_json)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                new_id,
                started_at,
                _dt(row.get("ended_at")),
                int(row.get("vm_count") or 0),
                Jsonb(_json(row.get("errors_json"), {})),
            ),
        )
        sweep_map[old_id] = new_id
        new_sweep_ids.add(new_id)
        _record(summary, "monitoring", "sweeps_inserted")

    for row in _sqlite_rows(db_path, "pve_snapshots"):
        sweep_id = sweep_map.get(int(row.get("sweep_id") or 0))
        if not sweep_id or sweep_id not in new_sweep_ids:
            continue
        tags = [
            tag.strip()
            for tag in str(row.get("tags_csv") or "").replace(";", ",").split(",")
            if tag.strip()
        ]
        conn.execute(
            """
            INSERT INTO pve_snapshots
                (sweep_id, checked_at, vmid, present, node, name, status,
                 tags_json, lock_mode, cores, sockets, memory_mb, balloon_mb,
                 machine, bios, smbios1, args, vmgenid, disks_json, net_json,
                 config_digest, probe_error)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s, %s)
            """,
            (
                sweep_id,
                _dt(row.get("checked_at")) or datetime.now(timezone.utc),
                int(row.get("vmid") or 0),
                _bool(row.get("present", 1)),
                row.get("node"),
                row.get("name"),
                row.get("status"),
                Jsonb(tags),
                row.get("lock_mode"),
                row.get("cores"),
                row.get("sockets"),
                row.get("memory_mb"),
                row.get("balloon_mb"),
                row.get("machine"),
                row.get("bios"),
                row.get("smbios1"),
                row.get("args"),
                row.get("vmgenid"),
                Jsonb(_json(row.get("disks_json"), [])),
                Jsonb(_json(row.get("net_json"), [])),
                row.get("config_digest") or "",
                row.get("probe_error"),
            ),
        )
        _record(summary, "monitoring", "pve_snapshots_inserted")

    for row in _sqlite_rows(db_path, "device_probes"):
        sweep_id = sweep_map.get(int(row.get("sweep_id") or 0))
        if not sweep_id or sweep_id not in new_sweep_ids:
            continue
        conn.execute(
            """
            INSERT INTO device_probes
                (sweep_id, checked_at, vmid, vm_name, win_name, serial, uuid,
                 os_build, dsreg_status, ad_found, ad_match_count,
                 ad_matches_json, entra_found, entra_match_count,
                 entra_matches_json, intune_found, intune_match_count,
                 intune_matches_json, probe_errors_json)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s)
            """,
            (
                sweep_id,
                _dt(row.get("checked_at")) or datetime.now(timezone.utc),
                int(row.get("vmid") or 0),
                row.get("vm_name"),
                row.get("win_name"),
                row.get("serial"),
                row.get("uuid"),
                row.get("os_build"),
                Jsonb(_json(row.get("dsreg_status"), {})),
                _bool(row.get("ad_found", 0)),
                int(row.get("ad_match_count") or 0),
                Jsonb(_json(row.get("ad_matches_json"), [])),
                _bool(row.get("entra_found", 0)),
                int(row.get("entra_match_count") or 0),
                Jsonb(_json(row.get("entra_matches_json"), [])),
                _bool(row.get("intune_found", 0)),
                int(row.get("intune_match_count") or 0),
                Jsonb(_json(row.get("intune_matches_json"), [])),
                Jsonb(_json(row.get("probe_errors_json"), {})),
            ),
        )
        _record(summary, "monitoring", "device_probes_inserted")

    for table in ("monitoring_sweeps", "pve_snapshots", "device_probes", "monitoring_search_ous"):
        _sync_sequence(conn, table)


def _migrate_device_deletions(
    conn: Connection,
    output_dir: Path,
    summary: dict[str, dict[str, int]],
) -> None:
    rows = _sqlite_rows(output_dir / "devices.db", "deletions")
    used = _used_ids(conn, "deletions")
    next_ref = [_next_id(conn, "deletions", (row.get("id") for row in rows))]
    for row in sorted(rows, key=lambda r: int(r.get("id") or 0)):
        deleted_at = _dt(row.get("deleted_at")) or datetime.now(timezone.utc)
        existing = conn.execute(
            """
            SELECT id FROM deletions
            WHERE source = %s AND object_id = %s AND deleted_at = %s
            LIMIT 1
            """,
            (row.get("source") or "", row.get("object_id") or "", deleted_at),
        ).fetchone()
        if existing:
            _record(summary, "devices", "deletions_existing")
            continue
        new_id = _choose_id(int(row.get("id") or 0), used, next_ref)
        conn.execute(
            """
            INSERT INTO deletions
                (id, deleted_at, source, object_id, serial, display_name, status, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_id,
                deleted_at,
                row.get("source") or "",
                row.get("object_id") or "",
                row.get("serial"),
                row.get("display_name"),
                row.get("status") or "unknown",
                row.get("message"),
            ),
        )
        _record(summary, "devices", "deletions_inserted")
    if rows:
        _sync_sequence(conn, "deletions")


def migrate(
    *,
    output_dir: Path,
    database_url: str,
    dry_run: bool = False,
    include_monitor_history: bool = True,
    legacy_credential_key: Path | None = None,
    target_credential_key: Path | None = None,
) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    rewrap_credential = _credential_rewrapper(
        legacy_credential_key,
        target_credential_key,
    )
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        sequences_pg.init(conn)
        device_history_pg.init(conn)
        devices_pg.init(conn)

        try:
            with conn.transaction():
                sequence_db = output_dir / "sequences.db"
                credential_map = _migrate_credentials(
                    conn,
                    _sqlite_rows(sequence_db, "credentials"),
                    summary,
                    rewrap_credential,
                )
                sequence_map = _migrate_sequences(
                    conn,
                    _sqlite_rows(sequence_db, "task_sequences"),
                    _sqlite_rows(sequence_db, "task_sequence_steps"),
                    credential_map,
                    summary,
                )
                _migrate_sequence_state(conn, output_dir, sequence_map, credential_map, summary)
                _migrate_device_monitor(
                    conn,
                    output_dir,
                    credential_map,
                    summary,
                    include_history=include_monitor_history,
                )
                _migrate_device_deletions(conn, output_dir, summary)

                if dry_run:
                    raise _DryRunRollback()
                _record(summary, "migration", "committed")
        except _DryRunRollback:
            _record(summary, "migration", "dry_run_rolled_back")
    return summary


def _default_output_dir() -> Path:
    return Path(os.environ.get("AUTOPILOT_OUTPUT_DIR") or ROOT / "output")


def _default_database_url() -> str:
    return (
        os.environ.get("AUTOPILOT_DATABASE_URL")
        or os.environ.get("AUTOPILOT_TS_ENGINE_DATABASE_URL")
        or ""
    )


def _default_credential_key() -> Path:
    return Path(
        os.environ.get("AUTOPILOT_CREDENTIAL_KEY")
        or Path(os.environ.get("AUTOPILOT_SECRETS_DIR", "/app/secrets")) / "credential_key"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_default_output_dir())
    parser.add_argument("--database-url", default=_default_database_url())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--legacy-credential-key",
        type=Path,
        help="Decrypt legacy SQLite credential blobs with this Fernet key.",
    )
    parser.add_argument(
        "--target-credential-key",
        type=Path,
        default=_default_credential_key(),
        help="Re-encrypt imported credential blobs with this Fernet key.",
    )
    parser.add_argument(
        "--skip-monitor-history",
        action="store_true",
        help="Migrate monitoring settings/OUs but skip historical sweeps/probes.",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("set --database-url or AUTOPILOT_DATABASE_URL")
    summary = migrate(
        output_dir=args.output_dir,
        database_url=args.database_url,
        dry_run=args.dry_run,
        include_monitor_history=not args.skip_monitor_history,
        legacy_credential_key=args.legacy_credential_key,
        target_credential_key=args.target_credential_key,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
