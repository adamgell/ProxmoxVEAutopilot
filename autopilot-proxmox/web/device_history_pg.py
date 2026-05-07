"""PostgreSQL store for device state monitoring.

Runtime monitoring history is Postgres-only. The public functions mirror
``device_history_db`` without ``db_path`` parameters so callers keep the
same row shapes while JSON fields are stored as JSONB and timestamps as
``timestamptz``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS monitoring_sweeps (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NULL,
    vm_count integer NOT NULL DEFAULT 0,
    errors_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS pve_snapshots (
    id bigserial PRIMARY KEY,
    sweep_id bigint NOT NULL REFERENCES monitoring_sweeps(id) ON DELETE CASCADE,
    checked_at timestamptz NOT NULL,
    vmid integer NOT NULL,
    present boolean NOT NULL DEFAULT true,
    node text NULL,
    name text NULL,
    status text NULL,
    tags_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    lock_mode text NULL,
    cores integer NULL,
    sockets integer NULL,
    memory_mb integer NULL,
    balloon_mb integer NULL,
    machine text NULL,
    bios text NULL,
    smbios1 text NULL,
    args text NULL,
    vmgenid text NULL,
    disks_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    net_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    config_digest text NOT NULL,
    probe_error text NULL
);
CREATE INDEX IF NOT EXISTS idx_pve_vmid_time ON pve_snapshots(vmid, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_pve_sweep_vmid_time ON pve_snapshots(sweep_id, vmid, checked_at DESC);

CREATE TABLE IF NOT EXISTS device_probes (
    id bigserial PRIMARY KEY,
    sweep_id bigint NOT NULL REFERENCES monitoring_sweeps(id) ON DELETE CASCADE,
    checked_at timestamptz NOT NULL,
    vmid integer NOT NULL,
    vm_name text NULL,
    win_name text NULL,
    serial text NULL,
    uuid text NULL,
    os_build text NULL,
    dsreg_status jsonb NOT NULL DEFAULT '{}'::jsonb,
    ad_found boolean NOT NULL DEFAULT false,
    ad_match_count integer NOT NULL DEFAULT 0,
    ad_matches_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    entra_found boolean NOT NULL DEFAULT false,
    entra_match_count integer NOT NULL DEFAULT 0,
    entra_matches_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    intune_found boolean NOT NULL DEFAULT false,
    intune_match_count integer NOT NULL DEFAULT 0,
    intune_matches_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    probe_errors_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_probe_vmid_time ON device_probes(vmid, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_probe_sweep_vmid_time ON device_probes(sweep_id, vmid, checked_at DESC);

CREATE TABLE IF NOT EXISTS monitoring_settings (
    id integer PRIMARY KEY CHECK (id = 1),
    enabled boolean NOT NULL DEFAULT true,
    interval_seconds integer NOT NULL DEFAULT 900,
    ad_credential_id integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS monitoring_search_ous (
    id bigserial PRIMARY KEY,
    dn text NOT NULL UNIQUE,
    label text NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS keytab_health (
    id integer PRIMARY KEY CHECK (id = 1),
    keytab_path text NULL,
    keytab_mtime timestamptz NULL,
    keytab_principal text NULL,
    keytab_kvno_local integer NULL,
    keytab_kvno_ad integer NULL,
    last_probe_at timestamptz NULL,
    last_probe_status text NULL,
    last_probe_message text NULL,
    last_kinit_at timestamptz NULL,
    last_kinit_ok boolean NULL,
    last_kinit_error text NULL,
    last_refresh_at timestamptz NULL,
    last_refresh_ok boolean NULL,
    last_refresh_message text NULL,
    updated_at timestamptz NOT NULL
);
"""


_DN_RE = re.compile(
    r"^(?:OU|CN|DC)=[^,;=\x00]+(?:,(?:OU|CN|DC)=[^,;=\x00]+)*$",
    re.IGNORECASE,
)


class CannotDeleteLastOu(Exception):
    """Deleting or disabling this OU would leave zero enabled rows."""


class InvalidDn(ValueError):
    """DN did not match the expected OU=...,DC=... syntax."""


@dataclass
class Settings:
    enabled: bool
    interval_seconds: int
    ad_credential_id: int
    updated_at: str


@dataclass
class SearchOu:
    id: int
    dn: str
    label: str
    enabled: bool
    sort_order: int
    created_at: str
    updated_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, Jsonb):
        return value.obj
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list:
    parsed = _json_value(value, [])
    return parsed if isinstance(parsed, list) else []


def _json_obj(value: Any) -> dict:
    parsed = _json_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _tags_from_snapshot(snap: dict) -> list[str]:
    if "tags_json" in snap:
        return [str(t) for t in _json_list(snap.get("tags_json")) if str(t)]
    if "tags" in snap:
        return [str(t) for t in _json_list(snap.get("tags")) if str(t)]
    raw = snap.get("tags_csv")
    if raw is None:
        return []
    return [t.strip() for t in re.split(r"[,;]", str(raw)) if t.strip()]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _init_on(conn: Connection, *, seed_default_ou: bool = True) -> None:
    conn.execute(SCHEMA)
    now = _now()
    conn.execute(
        """
        INSERT INTO monitoring_settings
            (id, enabled, interval_seconds, ad_credential_id, updated_at)
        VALUES (1, true, 900, 0, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (now,),
    )
    conn.execute(
        """
        INSERT INTO keytab_health (id, updated_at)
        VALUES (1, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (now,),
    )
    if seed_default_ou:
        existing = conn.execute(
            "SELECT COUNT(*) AS count FROM monitoring_search_ous"
        ).fetchone()["count"]
        if int(existing or 0) == 0:
            conn.execute(
                """
                INSERT INTO monitoring_search_ous
                    (dn, label, enabled, sort_order, created_at, updated_at)
                VALUES (%s, %s, true, 0, %s, %s)
                """,
                (
                    "OU=WorkspaceLabs,DC=home,DC=gell,DC=one",
                    "WorkspaceLabs",
                    now,
                    now,
                ),
            )


def init(conn: Connection | None = None, *, seed_default_ou: bool = True) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        _init_on(conn, seed_default_ou=seed_default_ou)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS device_probes CASCADE")
    conn.execute("DROP TABLE IF EXISTS pve_snapshots CASCADE")
    conn.execute("DROP TABLE IF EXISTS monitoring_sweeps CASCADE")
    conn.execute("DROP TABLE IF EXISTS monitoring_search_ous CASCADE")
    conn.execute("DROP TABLE IF EXISTS monitoring_settings CASCADE")
    conn.execute("DROP TABLE IF EXISTS keytab_health CASCADE")
    conn.commit()


def get_settings() -> Settings:
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            SELECT enabled, interval_seconds, ad_credential_id, updated_at
            FROM monitoring_settings WHERE id = 1
            """
        ).fetchone()
    return Settings(
        enabled=bool(row["enabled"]),
        interval_seconds=int(row["interval_seconds"]),
        ad_credential_id=int(row["ad_credential_id"]),
        updated_at=_iso(row["updated_at"]) or "",
    )


def update_settings(
    *,
    enabled: Optional[bool] = None,
    interval_seconds: Optional[int] = None,
    ad_credential_id: Optional[int] = None,
) -> None:
    sets, params = [], []
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(bool(enabled))
    if interval_seconds is not None:
        if interval_seconds < 60:
            raise ValueError(
                f"interval_seconds must be >= 60, got {interval_seconds}"
            )
        sets.append("interval_seconds = %s")
        params.append(int(interval_seconds))
    if ad_credential_id is not None:
        sets.append("ad_credential_id = %s")
        params.append(int(ad_credential_id))
    if not sets:
        return
    sets.append("updated_at = %s")
    params.append(_now())
    with db_pg.connection() as conn:
        conn.execute(
            f"UPDATE monitoring_settings SET {', '.join(sets)} WHERE id = 1",
            params,
        )
        conn.commit()


def _validate_dn(dn: str) -> str:
    cleaned = (dn or "").strip()
    if not _DN_RE.match(cleaned):
        raise InvalidDn(
            f"not a valid DN: {dn!r} - "
            "expected OU=...,DC=... with only OU/CN/DC components"
        )
    return cleaned


def _row_to_search_ou(row: dict) -> SearchOu:
    return SearchOu(
        id=int(row["id"]),
        dn=row["dn"],
        label=row["label"] or "",
        enabled=bool(row["enabled"]),
        sort_order=int(row["sort_order"]),
        created_at=_iso(row["created_at"]) or "",
        updated_at=_iso(row["updated_at"]) or "",
    )


def list_search_ous() -> list[SearchOu]:
    with db_pg.connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM monitoring_search_ous
            ORDER BY sort_order, id
            """
        ).fetchall()
    return [_row_to_search_ou(r) for r in rows]


def list_enabled_search_ous() -> list[SearchOu]:
    return [o for o in list_search_ous() if o.enabled]


def add_search_ou(
    *,
    dn: str,
    label: str = "",
    enabled: bool = True,
    sort_order: Optional[int] = None,
) -> int:
    clean_dn = _validate_dn(dn)
    now = _now()
    with db_pg.connection() as conn:
        if sort_order is None:
            sort_order = conn.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order
                FROM monitoring_search_ous
                """
            ).fetchone()["next_order"]
        row = conn.execute(
            """
            INSERT INTO monitoring_search_ous
                (dn, label, enabled, sort_order, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (clean_dn, label or "", bool(enabled), int(sort_order), now, now),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def update_search_ou(
    ou_id: int,
    *,
    dn: Optional[str] = None,
    label: Optional[str] = None,
    enabled: Optional[bool] = None,
    sort_order: Optional[int] = None,
) -> None:
    sets, params = [], []
    if dn is not None:
        sets.append("dn = %s")
        params.append(_validate_dn(dn))
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if sort_order is not None:
        sets.append("sort_order = %s")
        params.append(int(sort_order))
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(bool(enabled))
    if not sets:
        return
    sets.append("updated_at = %s")
    params.append(_now())
    with db_pg.connection() as conn:
        with conn.transaction():
            conn.execute(
                f"UPDATE monitoring_search_ous SET {', '.join(sets)} WHERE id = %s",
                [*params, int(ou_id)],
            )
            enabled_count = conn.execute(
                "SELECT COUNT(*) AS count FROM monitoring_search_ous WHERE enabled"
            ).fetchone()["count"]
            if int(enabled_count or 0) == 0:
                raise CannotDeleteLastOu(
                    "at least one search OU must stay enabled; "
                    "enable another before disabling this one"
                )
        conn.commit()


def delete_search_ou(ou_id: int) -> None:
    with db_pg.connection() as conn:
        with conn.transaction():
            total = conn.execute(
                "SELECT COUNT(*) AS count FROM monitoring_search_ous"
            ).fetchone()["count"]
            if int(total or 0) <= 1:
                raise CannotDeleteLastOu(
                    "at least one search OU must exist; "
                    "add another before deleting this one"
                )
            target = conn.execute(
                "SELECT enabled FROM monitoring_search_ous WHERE id = %s",
                (int(ou_id),),
            ).fetchone()
            if target is None:
                return
            enabled_count = conn.execute(
                "SELECT COUNT(*) AS count FROM monitoring_search_ous WHERE enabled"
            ).fetchone()["count"]
            if bool(target["enabled"]) and int(enabled_count or 0) <= 1:
                raise CannotDeleteLastOu(
                    "at least one search OU must stay enabled; "
                    "enable another before deleting this one"
                )
            conn.execute(
                "DELETE FROM monitoring_search_ous WHERE id = %s",
                (int(ou_id),),
            )
        conn.commit()


def start_sweep() -> int:
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO monitoring_sweeps (started_at)
            VALUES (%s)
            RETURNING id
            """,
            (_now(),),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def finish_sweep(
    sweep_id: int,
    *,
    vm_count: int,
    errors: Optional[dict] = None,
) -> None:
    with db_pg.connection() as conn:
        conn.execute(
            """
            UPDATE monitoring_sweeps
            SET ended_at = %s, vm_count = %s, errors_json = %s
            WHERE id = %s
            """,
            (_now(), int(vm_count), Jsonb(errors or {}), int(sweep_id)),
        )
        conn.commit()


def insert_pve_snapshot(sweep_id: int, snap: dict) -> int:
    tags = _tags_from_snapshot(snap)
    disks = _json_list(snap.get("disks_json"))
    nets = _json_list(snap.get("net_json"))
    row = {
        "sweep_id": int(sweep_id),
        "checked_at": snap.get("checked_at") or _now(),
        "vmid": int(snap.get("vmid")),
        "present": _bool(snap.get("present", True)),
        "node": snap.get("node"),
        "name": snap.get("name"),
        "status": snap.get("status"),
        "tags_json": Jsonb(tags),
        "lock_mode": snap.get("lock_mode"),
        "cores": snap.get("cores"),
        "sockets": snap.get("sockets"),
        "memory_mb": snap.get("memory_mb"),
        "balloon_mb": snap.get("balloon_mb"),
        "machine": snap.get("machine"),
        "bios": snap.get("bios"),
        "smbios1": snap.get("smbios1"),
        "args": snap.get("args"),
        "vmgenid": snap.get("vmgenid"),
        "disks_json": Jsonb(disks),
        "net_json": Jsonb(nets),
        "config_digest": snap.get("config_digest") or "",
        "probe_error": snap.get("probe_error"),
    }
    cols = list(row)
    with db_pg.connection() as conn:
        inserted = conn.execute(
            f"""
            INSERT INTO pve_snapshots ({', '.join(cols)})
            VALUES ({', '.join(['%s'] * len(cols))})
            RETURNING id
            """,
            [row[c] for c in cols],
        ).fetchone()
        conn.commit()
        return int(inserted["id"])


def insert_device_probe(sweep_id: int, probe: dict) -> int:
    row = {
        "sweep_id": int(sweep_id),
        "checked_at": probe.get("checked_at") or _now(),
        "vmid": int(probe.get("vmid")),
        "vm_name": probe.get("vm_name"),
        "win_name": probe.get("win_name"),
        "serial": probe.get("serial"),
        "uuid": probe.get("uuid"),
        "os_build": probe.get("os_build"),
        "dsreg_status": Jsonb(_json_obj(probe.get("dsreg_status"))),
        "ad_found": _bool(probe.get("ad_found", False)),
        "ad_match_count": int(probe.get("ad_match_count") or 0),
        "ad_matches_json": Jsonb(
            _json_list(probe.get("ad_matches_json", probe.get("ad_matches")))
        ),
        "entra_found": _bool(probe.get("entra_found", False)),
        "entra_match_count": int(probe.get("entra_match_count") or 0),
        "entra_matches_json": Jsonb(
            _json_list(probe.get("entra_matches_json", probe.get("entra_matches")))
        ),
        "intune_found": _bool(probe.get("intune_found", False)),
        "intune_match_count": int(probe.get("intune_match_count") or 0),
        "intune_matches_json": Jsonb(
            _json_list(probe.get("intune_matches_json", probe.get("intune_matches")))
        ),
        "probe_errors_json": Jsonb(_json_obj(probe.get("probe_errors_json"))),
    }
    cols = list(row)
    with db_pg.connection() as conn:
        inserted = conn.execute(
            f"""
            INSERT INTO device_probes ({', '.join(cols)})
            VALUES ({', '.join(['%s'] * len(cols))})
            RETURNING id
            """,
            [row[c] for c in cols],
        ).fetchone()
        conn.commit()
        return int(inserted["id"])


def _pve_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("checked_at",):
        out[key] = _iso(out.get(key))
    tags = _json_list(out.get("tags_json"))
    out["tags_json"] = tags
    out["tags_csv"] = ",".join(str(t) for t in tags)
    out["disks_json"] = _json_list(out.get("disks_json"))
    out["net_json"] = _json_list(out.get("net_json"))
    return out


def _probe_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("checked_at",):
        out[key] = _iso(out.get(key))
    out["dsreg_status"] = _json_obj(out.get("dsreg_status"))
    out["ad_matches_json"] = _json_list(out.get("ad_matches_json"))
    out["entra_matches_json"] = _json_list(out.get("entra_matches_json"))
    out["intune_matches_json"] = _json_list(out.get("intune_matches_json"))
    out["probe_errors_json"] = _json_obj(out.get("probe_errors_json"))
    return out


def latest_pve_snapshot(vmid: int) -> dict | None:
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM pve_snapshots
            WHERE vmid = %s
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (int(vmid),),
        ).fetchone()
    return _pve_dict(row)


def latest_device_probe(vmid: int) -> dict | None:
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM device_probes
            WHERE vmid = %s
            ORDER BY checked_at DESC, id DESC
            LIMIT 1
            """,
            (int(vmid),),
        ).fetchone()
    return _probe_dict(row)


def history_for_vmid(
    vmid: int,
    *,
    limit: int = 50,
    completed_only: bool = False,
) -> dict:
    with db_pg.connection() as conn:
        if completed_only:
            pve = conn.execute(
                """
                SELECT p.*
                FROM pve_snapshots p
                JOIN monitoring_sweeps s ON s.id = p.sweep_id
                WHERE p.vmid = %s
                  AND s.ended_at IS NOT NULL
                ORDER BY p.checked_at DESC, p.id DESC
                LIMIT %s
                """,
                (int(vmid), int(limit)),
            ).fetchall()
            probes = conn.execute(
                """
                SELECT pr.*
                FROM device_probes pr
                JOIN monitoring_sweeps s ON s.id = pr.sweep_id
                WHERE pr.vmid = %s
                  AND s.ended_at IS NOT NULL
                ORDER BY pr.checked_at DESC, pr.id DESC
                LIMIT %s
                """,
                (int(vmid), int(limit)),
            ).fetchall()
        else:
            pve = conn.execute(
                """
                SELECT * FROM pve_snapshots
                WHERE vmid = %s
                ORDER BY checked_at DESC, id DESC
                LIMIT %s
                """,
                (int(vmid), int(limit)),
            ).fetchall()
            probes = conn.execute(
                """
                SELECT * FROM device_probes
                WHERE vmid = %s
                ORDER BY checked_at DESC, id DESC
                LIMIT %s
                """,
                (int(vmid), int(limit)),
            ).fetchall()
    return {
        "pve_snapshots": [_pve_dict(r) for r in pve],
        "device_probes": [_probe_dict(r) for r in probes],
    }


def latest_sweep_status() -> dict | None:
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            SELECT id, started_at, ended_at, vm_count, errors_json
            FROM monitoring_sweeps
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    ended_at = _iso(row.get("ended_at")) or ""
    return {
        "id": int(row["id"]),
        "started_at": _iso(row.get("started_at")) or "",
        "ended_at": ended_at,
        "vm_count": int(row.get("vm_count") or 0),
        "errors_json": _json_obj(row.get("errors_json")),
        "running": not bool(ended_at),
    }


def recent_sweeps(*, limit: int = 20) -> list[dict]:
    with db_pg.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, ended_at, vm_count, errors_json
            FROM monitoring_sweeps
            ORDER BY id DESC
            LIMIT %s
            """,
            (int(limit),),
        ).fetchall()
    out = []
    for row in rows:
        ended_at = _iso(row.get("ended_at")) or ""
        out.append({
            "id": int(row["id"]),
            "started_at": _iso(row.get("started_at")) or "",
            "ended_at": ended_at,
            "vm_count": int(row.get("vm_count") or 0),
            "errors_json": _json_obj(row.get("errors_json")),
            "running": not bool(ended_at),
        })
    return out


def latest_per_vmid() -> list[dict]:
    """Latest PVE row plus same-sweep probe for the newest completed sweep."""
    with db_pg.connection() as conn:
        latest_sweep = conn.execute(
            """
            SELECT id FROM monitoring_sweeps
            WHERE ended_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if latest_sweep is None:
            return []
        sweep_id = int(latest_sweep["id"])
        rows = conn.execute(
            """
            WITH latest_pve AS (
                SELECT DISTINCT ON (vmid) *
                FROM pve_snapshots
                WHERE sweep_id = %s
                ORDER BY vmid, checked_at DESC, id DESC
            ),
            latest_probe AS (
                SELECT DISTINCT ON (vmid) *
                FROM device_probes
                WHERE sweep_id = %s
                ORDER BY vmid, checked_at DESC, id DESC
            )
            SELECT
                p.vmid,
                p.checked_at AS last_checked,
                to_jsonb(p) AS pve,
                to_jsonb(pr) AS probe
            FROM latest_pve p
            LEFT JOIN latest_probe pr ON pr.vmid = p.vmid
            ORDER BY p.vmid
            """,
            (sweep_id, sweep_id),
        ).fetchall()
    out = []
    for row in rows:
        out.append({
            "vmid": int(row["vmid"]),
            "last_checked": _iso(row["last_checked"]) or "",
            "pve": _pve_dict(row["pve"]),
            "probe": _probe_dict(row["probe"]) if row["probe"] else None,
        })
    return out


def latest_completed_pair_for_vmid(vmid: int) -> dict | None:
    """Latest completed-sweep PVE row and same-sweep probe for one VMID."""
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            WITH latest_sweep AS (
                SELECT s.id
                FROM monitoring_sweeps s
                JOIN pve_snapshots p ON p.sweep_id = s.id
                WHERE s.ended_at IS NOT NULL
                  AND p.vmid = %s
                ORDER BY s.id DESC
                LIMIT 1
            ),
            latest_pve AS (
                SELECT p.*
                FROM pve_snapshots p
                JOIN latest_sweep ls ON ls.id = p.sweep_id
                WHERE p.vmid = %s
                ORDER BY p.checked_at DESC, p.id DESC
                LIMIT 1
            ),
            latest_probe AS (
                SELECT pr.*
                FROM device_probes pr
                JOIN latest_sweep ls ON ls.id = pr.sweep_id
                WHERE pr.vmid = %s
                ORDER BY pr.checked_at DESC, pr.id DESC
                LIMIT 1
            )
            SELECT
                p.vmid,
                p.checked_at AS last_checked,
                to_jsonb(p) AS pve,
                to_jsonb(pr) AS probe
            FROM latest_pve p
            LEFT JOIN latest_probe pr ON pr.vmid = p.vmid
            """,
            (int(vmid), int(vmid), int(vmid)),
        ).fetchone()
    if not row:
        return None
    return {
        "vmid": int(row["vmid"]),
        "last_checked": _iso(row["last_checked"]) or "",
        "pve": _pve_dict(row["pve"]),
        "probe": _probe_dict(row["probe"]) if row["probe"] else None,
    }


def ad_first_seen_map() -> dict[int, str]:
    with db_pg.connection() as conn:
        rows = conn.execute(
            """
            SELECT vmid, MIN(checked_at) AS first_seen
            FROM device_probes
            WHERE ad_found
            GROUP BY vmid
            """
        ).fetchall()
    return {int(r["vmid"]): _iso(r["first_seen"]) or "" for r in rows}


def fleet_summary() -> dict:
    default = {"total": 0}
    with db_pg.connection() as conn:
        sweep = conn.execute(
            """
            SELECT id FROM monitoring_sweeps
            WHERE ended_at IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if not sweep:
            return default
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN ad_found THEN 1 ELSE 0 END) AS ad,
                   SUM(CASE WHEN entra_found THEN 1 ELSE 0 END) AS entra,
                   SUM(CASE WHEN intune_found THEN 1 ELSE 0 END) AS intune
            FROM device_probes
            WHERE sweep_id = %s
            """,
            (int(sweep["id"]),),
        ).fetchone()
    total = int(row["total"] or 0)
    if total == 0:
        return default
    return {
        "total": total,
        "ad_joined_pct": round(100 * int(row["ad"] or 0) / total),
        "autopilot_pct": round(100 * int(row["entra"] or 0) / total),
        "intune_pct": round(100 * int(row["intune"] or 0) / total),
    }


def get_keytab_health() -> dict | None:
    with db_pg.connection() as conn:
        row = conn.execute("SELECT * FROM keytab_health WHERE id = 1").fetchone()
    if not row:
        return None
    out = dict(row)
    for key in (
        "keytab_mtime",
        "last_probe_at",
        "last_kinit_at",
        "last_refresh_at",
        "updated_at",
    ):
        out[key] = _iso(out.get(key))
    return out


def update_keytab_probe(
    *,
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
    last_kinit_error: Optional[str] = None,
) -> None:
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
        "last_kinit_ok": last_kinit_ok,
        "last_kinit_error": last_kinit_error,
    }
    sets, params = [], []
    for key, value in pairs.items():
        if value is not None:
            sets.append(f"{key} = %s")
            params.append(value)
    if not sets:
        return
    sets.append("updated_at = %s")
    params.append(_now())
    with db_pg.connection() as conn:
        conn.execute(
            f"UPDATE keytab_health SET {', '.join(sets)} WHERE id = 1",
            params,
        )
        conn.commit()


def update_keytab_refresh(
    *,
    ok: bool,
    message: str,
    at: Optional[str] = None,
) -> None:
    with db_pg.connection() as conn:
        conn.execute(
            """
            UPDATE keytab_health
            SET last_refresh_at = %s,
                last_refresh_ok = %s,
                last_refresh_message = %s,
                updated_at = %s
            WHERE id = 1
            """,
            (at or _now(), bool(ok), message, _now()),
        )
        conn.commit()
