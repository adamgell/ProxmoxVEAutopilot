"""SQLite store for device state monitoring.

Five tables:

- ``monitoring_sweeps`` — one row per sweep pass
- ``pve_snapshots``    — one row per (sweep, VM); Proxmox-side config
- ``device_probes``    — one row per (sweep, VM); AD/Entra/Intune matches
- ``monitoring_settings`` (single-row) — interval, enable, AD cred
- ``monitoring_search_ous`` — additive list of AD search OUs; the DAL
  enforces "at least one enabled row" on delete/disable via
  :class:`CannotDeleteLastOu`

All timestamps are ISO8601 UTC; the viewer's browser formats them.
The design spec is ``docs/specs/2026-04-20-device-state-monitoring-design.md``.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS monitoring_sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    vm_count    INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS pve_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_id      INTEGER NOT NULL REFERENCES monitoring_sweeps(id) ON DELETE CASCADE,
    checked_at    TEXT    NOT NULL,
    vmid          INTEGER NOT NULL,
    present       INTEGER NOT NULL DEFAULT 1,
    node          TEXT,
    name          TEXT,
    status        TEXT,
    tags_csv      TEXT NOT NULL DEFAULT '',
    lock_mode     TEXT,
    cores         INTEGER,
    sockets       INTEGER,
    memory_mb     INTEGER,
    balloon_mb    INTEGER,
    machine       TEXT,
    bios          TEXT,
    smbios1       TEXT,
    args          TEXT,
    vmgenid       TEXT,
    disks_json    TEXT NOT NULL DEFAULT '[]',
    net_json      TEXT NOT NULL DEFAULT '[]',
    config_digest TEXT NOT NULL,
    probe_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pve_vmid_time
    ON pve_snapshots (vmid, checked_at DESC);

CREATE TABLE IF NOT EXISTS device_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_id            INTEGER NOT NULL REFERENCES monitoring_sweeps(id) ON DELETE CASCADE,
    checked_at          TEXT    NOT NULL,
    vmid                INTEGER NOT NULL,
    vm_name             TEXT,
    win_name            TEXT,
    serial              TEXT,
    uuid                TEXT,
    os_build            TEXT,
    dsreg_status        TEXT,
    ad_found            INTEGER NOT NULL DEFAULT 0,
    ad_match_count      INTEGER NOT NULL DEFAULT 0,
    ad_matches_json     TEXT    NOT NULL DEFAULT '[]',
    entra_found         INTEGER NOT NULL DEFAULT 0,
    entra_match_count   INTEGER NOT NULL DEFAULT 0,
    entra_matches_json  TEXT    NOT NULL DEFAULT '[]',
    intune_found        INTEGER NOT NULL DEFAULT 0,
    intune_match_count  INTEGER NOT NULL DEFAULT 0,
    intune_matches_json TEXT    NOT NULL DEFAULT '[]',
    probe_errors_json   TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_probe_vmid_time
    ON device_probes (vmid, checked_at DESC);

CREATE TABLE IF NOT EXISTS monitoring_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled          INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 900,
    ad_credential_id INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS monitoring_search_ous (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dn         TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Single-row keytab health telemetry. Populated by probe_keytab()
-- (every sweep) and refresh_keytab() (daily). The /monitoring
-- dashboard banner + /monitoring/settings panel read from here.
CREATE TABLE IF NOT EXISTS keytab_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    keytab_path          TEXT,
    keytab_mtime         TEXT,
    keytab_principal     TEXT,
    keytab_kvno_local    INTEGER,
    keytab_kvno_ad       INTEGER,
    last_probe_at        TEXT,
    last_probe_status    TEXT,    -- ok / stale / missing / broken / kvno-mismatch
    last_probe_message   TEXT,
    last_kinit_at        TEXT,
    last_kinit_ok        INTEGER,
    last_kinit_error     TEXT,
    last_refresh_at      TEXT,
    last_refresh_ok      INTEGER,
    last_refresh_message TEXT,
    updated_at           TEXT NOT NULL
);
"""


# DN components: OU=, CN=, DC= only. Values accept any printable
# non-separator character — no commas, semicolons, equals signs, or
# nulls. Real-world DNs with escaped separators aren't supported
# (our AD doesn't use them, and ldap3 handles its own escaping on
# search). The separator restrictions catch most typos like
# "OU=X;DC=bad" where a semicolon was used instead of a comma.
_DN_RE = re.compile(
    r"^(?:OU|CN|DC)=[^,;=\x00]+(?:,(?:OU|CN|DC)=[^,;=\x00]+)*$",
    re.IGNORECASE,
)


class CannotDeleteLastOu(Exception):
    """Deleting or disabling this OU would leave zero enabled rows."""


class InvalidDn(ValueError):
    """DN did not match the expected OU=…,DC=… syntax."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: Path, *, seed_default_ou: bool = True) -> None:
    """Create the schema (idempotent) and seed the default search OU.

    Seeding only happens when the table is empty, so operator edits
    persist across restarts.
    """
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        now = _now()
        # Scalar settings row — insert with defaults if missing.
        conn.execute(
            "INSERT OR IGNORE INTO monitoring_settings "
            "(id, enabled, interval_seconds, ad_credential_id, updated_at) "
            "VALUES (1, 1, 900, 0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO keytab_health (id, updated_at) "
            "VALUES (1, ?)",
            (now,),
        )
        if seed_default_ou:
            existing = conn.execute(
                "SELECT COUNT(*) FROM monitoring_search_ous"
            ).fetchone()[0]
            if existing == 0:
                conn.execute(
                    "INSERT INTO monitoring_search_ous "
                    "(dn, label, enabled, sort_order, created_at, updated_at) "
                    "VALUES (?, ?, 1, 0, ?, ?)",
                    (
                        "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
                        "WorkspaceLabs",
                        now,
                        now,
                    ),
                )


# ---------------------------------------------------------------------------
# Settings (scalar)
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    enabled: bool
    interval_seconds: int
    ad_credential_id: int
    updated_at: str


def get_settings(db_path: Path) -> Settings:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT enabled, interval_seconds, ad_credential_id, updated_at "
            "FROM monitoring_settings WHERE id = 1"
        ).fetchone()
    return Settings(
        enabled=bool(row["enabled"]),
        interval_seconds=int(row["interval_seconds"]),
        ad_credential_id=int(row["ad_credential_id"]),
        updated_at=row["updated_at"],
    )


def update_settings(db_path: Path, *,
                    enabled: Optional[bool] = None,
                    interval_seconds: Optional[int] = None,
                    ad_credential_id: Optional[int] = None) -> None:
    sets, params = [], []
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if interval_seconds is not None:
        # 60-second floor lets operators test at 1-minute cadence; the
        # UI warns below 900 per the spec.
        if interval_seconds < 60:
            raise ValueError(
                f"interval_seconds must be >= 60, got {interval_seconds}"
            )
        sets.append("interval_seconds = ?")
        params.append(int(interval_seconds))
    if ad_credential_id is not None:
        sets.append("ad_credential_id = ?")
        params.append(int(ad_credential_id))
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE monitoring_settings SET {', '.join(sets)} WHERE id = 1",
            params,
        )


# ---------------------------------------------------------------------------
# Search OUs (additive, last-enabled-row protected)
# ---------------------------------------------------------------------------

@dataclass
class SearchOu:
    id: int
    dn: str
    label: str
    enabled: bool
    sort_order: int
    created_at: str
    updated_at: str


def _row_to_search_ou(row: sqlite3.Row) -> SearchOu:
    return SearchOu(
        id=int(row["id"]),
        dn=row["dn"],
        label=row["label"] or "",
        enabled=bool(row["enabled"]),
        sort_order=int(row["sort_order"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_dn(dn: str) -> str:
    cleaned = (dn or "").strip()
    if not _DN_RE.match(cleaned):
        raise InvalidDn(
            f"not a valid DN: {dn!r} — "
            "expected OU=…,DC=… with only OU/CN/DC components"
        )
    return cleaned


def list_search_ous(db_path: Path) -> list[SearchOu]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_search_ous "
            "ORDER BY sort_order, id"
        ).fetchall()
    return [_row_to_search_ou(r) for r in rows]


def list_enabled_search_ous(db_path: Path) -> list[SearchOu]:
    return [o for o in list_search_ous(db_path) if o.enabled]


def add_search_ou(db_path: Path, *, dn: str, label: str = "",
                  enabled: bool = True,
                  sort_order: Optional[int] = None) -> int:
    clean_dn = _validate_dn(dn)
    now = _now()
    with _connect(db_path) as conn:
        if sort_order is None:
            # Next after the current max, so new rows land at the end.
            sort_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 "
                "FROM monitoring_search_ous"
            ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO monitoring_search_ous "
            "(dn, label, enabled, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (clean_dn, label or "", 1 if enabled else 0,
             int(sort_order), now, now),
        )
        return int(cur.lastrowid)


def update_search_ou(db_path: Path, ou_id: int, *,
                     dn: Optional[str] = None,
                     label: Optional[str] = None,
                     enabled: Optional[bool] = None,
                     sort_order: Optional[int] = None) -> None:
    """Update a search-OU row. Disabling the last enabled row raises
    :class:`CannotDeleteLastOu`."""
    sets, params = [], []
    if dn is not None:
        sets.append("dn = ?")
        params.append(_validate_dn(dn))
    if label is not None:
        sets.append("label = ?")
        params.append(label)
    if sort_order is not None:
        sets.append("sort_order = ?")
        params.append(int(sort_order))
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    with _connect(db_path) as conn:
        # Invariant check must see the *result* of the update, so we
        # run it inside the same transaction and rollback if it would
        # drop enabled-count to zero.
        conn.execute(
            f"UPDATE monitoring_search_ous SET {', '.join(sets)} WHERE id = ?",
            [*params, int(ou_id)],
        )
        enabled_count = conn.execute(
            "SELECT COUNT(*) FROM monitoring_search_ous WHERE enabled = 1"
        ).fetchone()[0]
        if enabled_count == 0:
            conn.rollback()
            raise CannotDeleteLastOu(
                "at least one search OU must stay enabled; "
                "enable another before disabling this one"
            )


def delete_search_ou(db_path: Path, ou_id: int) -> None:
    """Delete a search-OU row. Deleting the only row, or the last
    enabled row, raises :class:`CannotDeleteLastOu`."""
    with _connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM monitoring_search_ous"
        ).fetchone()[0]
        if total <= 1:
            raise CannotDeleteLastOu(
                "at least one search OU must exist; "
                "add another before deleting this one"
            )
        # Was this row the last enabled? Check before we delete.
        target = conn.execute(
            "SELECT enabled FROM monitoring_search_ous WHERE id = ?",
            (int(ou_id),),
        ).fetchone()
        if target is None:
            return  # nothing to delete; caller treats 404 upstream
        enabled_count = conn.execute(
            "SELECT COUNT(*) FROM monitoring_search_ous WHERE enabled = 1"
        ).fetchone()[0]
        if target["enabled"] == 1 and enabled_count <= 1:
            raise CannotDeleteLastOu(
                "at least one search OU must stay enabled; "
                "enable another before deleting this one"
            )
        conn.execute(
            "DELETE FROM monitoring_search_ous WHERE id = ?",
            (int(ou_id),),
        )


# ---------------------------------------------------------------------------
# Sweeps + probes + pve_snapshots (append-only)
# ---------------------------------------------------------------------------

def start_sweep(db_path: Path) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO monitoring_sweeps (started_at) VALUES (?)",
            (_now(),),
        )
        return int(cur.lastrowid)


def finish_sweep(db_path: Path, sweep_id: int, *,
                 vm_count: int,
                 errors: Optional[dict] = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE monitoring_sweeps "
            "SET ended_at = ?, vm_count = ?, errors_json = ? "
            "WHERE id = ?",
            (_now(), int(vm_count),
             json.dumps(errors or {}, sort_keys=True),
             int(sweep_id)),
        )


def _insert_with_defaults(conn: sqlite3.Connection, table: str,
                          required: dict, optional: dict) -> int:
    """Insert a row, letting SQLite's column DEFAULTs handle keys whose
    ``optional`` value is None. Missing-from-dict keys are also
    omitted. Required keys are always inserted even if None (intended
    for caller-provided NOT NULL values like ``sweep_id``)."""
    cols = list(required)
    vals = [required[k] for k in cols]
    for k, v in optional.items():
        if v is None:
            continue
        cols.append(k)
        vals.append(v)
    placeholders = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    return int(cur.lastrowid)


def insert_pve_snapshot(db_path: Path, sweep_id: int, snap: dict) -> int:
    """Insert a single pve_snapshots row.

    ``snap`` keys map 1:1 to the column names; missing keys (or keys
    with value ``None``) fall through to the schema's DEFAULT clauses.
    ``disks_json`` / ``net_json`` are pre-serialized JSON strings.
    """
    required = {
        "sweep_id": int(sweep_id),
        "checked_at": snap.get("checked_at") or _now(),
        "vmid": snap.get("vmid"),
        "config_digest": snap.get("config_digest") or "",
    }
    optional_cols = [
        "present", "node", "name", "status", "tags_csv", "lock_mode",
        "cores", "sockets", "memory_mb", "balloon_mb",
        "machine", "bios", "smbios1", "args", "vmgenid",
        "disks_json", "net_json", "probe_error",
    ]
    optional = {c: snap.get(c) for c in optional_cols}
    with _connect(db_path) as conn:
        return _insert_with_defaults(
            conn, "pve_snapshots", required, optional,
        )


def insert_device_probe(db_path: Path, sweep_id: int, probe: dict) -> int:
    """Insert a single device_probes row. ``*_matches_json`` and
    ``probe_errors_json`` must be pre-serialized JSON strings (or left
    unset so the schema default ``'[]'`` / ``'{}'`` applies)."""
    required = {
        "sweep_id": int(sweep_id),
        "checked_at": probe.get("checked_at") or _now(),
        "vmid": probe.get("vmid"),
    }
    optional_cols = [
        "vm_name", "win_name", "serial", "uuid", "os_build", "dsreg_status",
        "ad_found", "ad_match_count", "ad_matches_json",
        "entra_found", "entra_match_count", "entra_matches_json",
        "intune_found", "intune_match_count", "intune_matches_json",
        "probe_errors_json",
    ]
    optional = {c: probe.get(c) for c in optional_cols}
    with _connect(db_path) as conn:
        return _insert_with_defaults(
            conn, "device_probes", required, optional,
        )


def latest_pve_snapshot(db_path: Path, vmid: int) -> Optional[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM pve_snapshots "
            "WHERE vmid = ? ORDER BY checked_at DESC LIMIT 1",
            (int(vmid),),
        ).fetchone()


def latest_device_probe(db_path: Path, vmid: int) -> Optional[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM device_probes "
            "WHERE vmid = ? ORDER BY checked_at DESC LIMIT 1",
            (int(vmid),),
        ).fetchone()


def history_for_vmid(db_path: Path, vmid: int,
                     *, limit: int = 50) -> dict:
    """Return the last ``limit`` rows of each append-only table for a
    VM, newest first. Caller merges + diffs for the timeline view."""
    with _connect(db_path) as conn:
        pve = conn.execute(
            "SELECT * FROM pve_snapshots "
            "WHERE vmid = ? ORDER BY checked_at DESC LIMIT ?",
            (int(vmid), int(limit)),
        ).fetchall()
        probes = conn.execute(
            "SELECT * FROM device_probes "
            "WHERE vmid = ? ORDER BY checked_at DESC LIMIT ?",
            (int(vmid), int(limit)),
        ).fetchall()
    return {
        "pve_snapshots": [dict(r) for r in pve],
        "device_probes": [dict(r) for r in probes],
    }


# ---------------------------------------------------------------------------
# keytab health (single-row)
# ---------------------------------------------------------------------------


def get_keytab_health(db_path: Path) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM keytab_health WHERE id = 1"
        ).fetchone()
    return dict(row) if row else None


def update_keytab_probe(db_path: Path, *,
                        keytab_path: Optional[str] = None,
                        keytab_mtime: Optional[str] = None,
                        keytab_principal: Optional[str] = None,
                        keytab_kvno_local: Optional[int] = None,
                        keytab_kvno_ad: Optional[int] = None,
                        last_probe_at: Optional[str] = None,
                        last_probe_status: Optional[str] = None,
                        last_probe_message: Optional[str] = None,
                        last_kinit_at: Optional[str] = None,
                        last_kinit_ok: Optional[bool] = None,
                        last_kinit_error: Optional[str] = None) -> None:
    """Partial update; only non-None fields are written."""
    pairs = {
        "keytab_path": keytab_path,
        "keytab_mtime": keytab_mtime,
        "keytab_principal": keytab_principal,
        "keytab_kvno_local": keytab_kvno_local,
        "keytab_kvno_ad": keytab_kvno_ad,
        "last_probe_at": last_probe_at,
        "last_probe_status": last_probe_status,
        "last_probe_message": last_probe_message,
        "last_kinit_at": last_kinit_at,
        "last_kinit_ok": None if last_kinit_ok is None else (1 if last_kinit_ok else 0),
        "last_kinit_error": last_kinit_error,
    }
    sets, params = [], []
    for k, v in pairs.items():
        if v is not None:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE keytab_health SET {', '.join(sets)} WHERE id = 1",
            params,
        )


def update_keytab_refresh(db_path: Path, *,
                          ok: bool, message: str,
                          at: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE keytab_health SET "
            " last_refresh_at = ?, last_refresh_ok = ?, "
            " last_refresh_message = ?, updated_at = ? "
            "WHERE id = 1",
            (at or _now(), 1 if ok else 0, message, _now()),
        )


def latest_per_vmid(db_path: Path) -> list[dict]:
    """For the /monitoring dashboard: latest pve_snapshot + device_probe
    per vmid. Unknown rows come back as None so VMs that have only
    PVE data (guest agent down) still appear."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT vmid, MAX(checked_at) AS last_checked "
            "FROM pve_snapshots GROUP BY vmid"
        ).fetchall()
        out = []
        for r in rows:
            vmid = int(r["vmid"])
            pve = conn.execute(
                "SELECT * FROM pve_snapshots "
                "WHERE vmid = ? ORDER BY checked_at DESC LIMIT 1",
                (vmid,),
            ).fetchone()
            probe = conn.execute(
                "SELECT * FROM device_probes "
                "WHERE vmid = ? ORDER BY checked_at DESC LIMIT 1",
                (vmid,),
            ).fetchone()
            out.append({
                "vmid": vmid,
                "last_checked": r["last_checked"],
                "pve": dict(pve) if pve else None,
                "probe": dict(probe) if probe else None,
            })
    return out
