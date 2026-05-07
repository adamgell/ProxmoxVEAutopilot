"""PostgreSQL-backed Autopilot / Intune / Entra device cache.

Runtime device cache storage is Postgres-only. Public functions mirror the
old SQLite cache behavior without ``db_path`` parameters so app call sites keep
the same grouped row shapes while records are stored in typed Postgres tables.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_devices (
    id text PRIMARY KEY,
    serial text NOT NULL,
    group_tag text NULL,
    profile_status text NULL,
    enrollment_state text NULL,
    manufacturer text NULL,
    model text NULL,
    display_name text NULL,
    last_contact timestamptz NULL,
    azure_ad_device_id text NULL,
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autopilot_serial ON autopilot_devices(serial);

CREATE TABLE IF NOT EXISTS intune_devices (
    id text PRIMARY KEY,
    serial text NULL,
    device_name text NULL,
    os text NULL,
    os_version text NULL,
    user_principal_name text NULL,
    compliance_state text NULL,
    management_state text NULL,
    last_sync timestamptz NULL,
    enrolled_date timestamptz NULL,
    azure_ad_device_id text NULL,
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intune_serial ON intune_devices(serial);
CREATE INDEX IF NOT EXISTS idx_intune_azure_id ON intune_devices(azure_ad_device_id);

CREATE TABLE IF NOT EXISTS entra_devices (
    id text PRIMARY KEY,
    device_id text NULL,
    serial text NULL,
    ztdid text NULL,
    display_name text NULL,
    operating_system text NULL,
    operating_system_version text NULL,
    trust_type text NULL,
    approximate_last_sign_in timestamptz NULL,
    account_enabled boolean NULL,
    raw_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entra_serial ON entra_devices(serial);
CREATE INDEX IF NOT EXISTS idx_entra_device_id ON entra_devices(device_id);

CREATE TABLE IF NOT EXISTS deletions (
    id bigserial PRIMARY KEY,
    deleted_at timestamptz NOT NULL,
    source text NOT NULL,
    object_id text NOT NULL,
    serial text NULL,
    display_name text NULL,
    status text NOT NULL,
    message text NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _nullable_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def _json_value(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value.obj
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in (
        "last_contact",
        "last_sync",
        "enrolled_date",
        "approximate_last_sign_in",
        "synced_at",
        "deleted_at",
    ):
        if key in out:
            out[key] = _iso(out.get(key))
    if "raw_json" in out:
        out["raw_json"] = _json_value(out.get("raw_json"))
    return out


def _open_or_use(conn: Connection | None) -> tuple[Connection, bool]:
    if conn is not None:
        return conn, False
    return db_pg.connect(), True


def init(conn: Connection | None = None) -> None:
    db, own = _open_or_use(conn)
    try:
        db.execute(SCHEMA)
        db.commit()
    finally:
        if own:
            db.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS deletions CASCADE")
    conn.execute("DROP TABLE IF EXISTS entra_devices CASCADE")
    conn.execute("DROP TABLE IF EXISTS intune_devices CASCADE")
    conn.execute("DROP TABLE IF EXISTS autopilot_devices CASCADE")
    conn.commit()


def _extract_physical_id(raw: dict, prefix: str) -> str | None:
    for pid in raw.get("physicalIds", []) or []:
        if isinstance(pid, str) and pid.startswith(prefix):
            return pid.split(":", 1)[1]
    return None


def upsert_autopilot(devices: Iterable[dict]) -> int:
    now = _now()
    rows = []
    for d in devices:
        rows.append((
            d.get("id", ""),
            d.get("serialNumber", ""),
            _nullable_text(d.get("groupTag")),
            _nullable_text(d.get("deploymentProfileAssignmentStatus")),
            _nullable_text(d.get("enrollmentState")),
            _nullable_text(d.get("manufacturer")),
            _nullable_text(d.get("model")),
            _nullable_text(d.get("displayName")),
            _timestamp(d.get("lastContactedDateTime")),
            _nullable_text(
                d.get("azureAdDeviceId") or d.get("azureActiveDirectoryDeviceId")
            ),
            Jsonb(d),
            now,
        ))
    with db_pg.connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM autopilot_devices")
            if rows:
                conn.cursor().executemany(
                    """
                    INSERT INTO autopilot_devices
                        (id, serial, group_tag, profile_status, enrollment_state,
                         manufacturer, model, display_name, last_contact,
                         azure_ad_device_id, raw_json, synced_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
    return len(rows)


def upsert_intune(devices: Iterable[dict]) -> int:
    now = _now()
    rows = []
    for d in devices:
        rows.append((
            d.get("id", ""),
            _nullable_text(d.get("serialNumber")),
            _nullable_text(d.get("deviceName")),
            _nullable_text(d.get("operatingSystem")),
            _nullable_text(d.get("osVersion")),
            _nullable_text(d.get("userPrincipalName")),
            _nullable_text(d.get("complianceState")),
            _nullable_text(d.get("managementState")),
            _timestamp(d.get("lastSyncDateTime")),
            _timestamp(d.get("enrolledDateTime")),
            _nullable_text(d.get("azureADDeviceId")),
            Jsonb(d),
            now,
        ))
    with db_pg.connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM intune_devices")
            if rows:
                conn.cursor().executemany(
                    """
                    INSERT INTO intune_devices
                        (id, serial, device_name, os, os_version, user_principal_name,
                         compliance_state, management_state, last_sync, enrolled_date,
                         azure_ad_device_id, raw_json, synced_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
    return len(rows)


def upsert_entra(devices: Iterable[dict]) -> int:
    now = _now()
    rows = []
    for d in devices:
        rows.append((
            d.get("id", ""),
            _nullable_text(d.get("deviceId")),
            _extract_physical_id(d, "[SerialNumber]:"),
            _extract_physical_id(d, "[ZTDID]:"),
            _nullable_text(d.get("displayName")),
            _nullable_text(d.get("operatingSystem")),
            _nullable_text(d.get("operatingSystemVersion")),
            _nullable_text(d.get("trustType")),
            _timestamp(d.get("approximateLastSignInDateTime")),
            d.get("accountEnabled"),
            Jsonb(d),
            now,
        ))
    with db_pg.connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM entra_devices")
            if rows:
                conn.cursor().executemany(
                    """
                    INSERT INTO entra_devices
                        (id, device_id, serial, ztdid, display_name, operating_system,
                         operating_system_version, trust_type, approximate_last_sign_in,
                         account_enabled, raw_json, synced_at)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
    return len(rows)


def _is_windows_intune(row: dict) -> bool:
    return (row.get("os") or "").strip().lower().startswith("windows")


def _is_windows_entra(row: dict) -> bool:
    os_name = (row.get("operating_system") or "").strip().lower()
    return os_name.startswith("windows") or os_name == "unknown" or os_name == ""


def _read_cache() -> tuple[list[dict], list[dict], list[dict], dict]:
    with db_pg.connection() as conn:
        ap = [
            _row_to_dict(r)
            for r in conn.execute("SELECT * FROM autopilot_devices").fetchall()
        ]
        it = [
            _row_to_dict(r)
            for r in conn.execute("SELECT * FROM intune_devices").fetchall()
        ]
        en = [
            _row_to_dict(r)
            for r in conn.execute("SELECT * FROM entra_devices").fetchall()
        ]
    meta = {
        "synced_at": max([r["synced_at"] for r in ap + it + en] or [""]),
        "counts": {"autopilot": len(ap), "intune": len(it), "entra": len(en)},
    }
    return ap, it, en, meta


def grouped_by_serial(*, windows_only: bool = False) -> dict[str, dict]:
    groups, _extra = list_grouped(windows_only=windows_only)
    return {g["serial"]: g for g in groups}


def list_grouped(*, windows_only: bool = True) -> tuple[list[dict], dict]:
    """Return serial-grouped records plus metadata/unmatched Entra rows."""
    ap, it, en, meta = _read_cache()
    intune_by_azure_id = {
        r["azure_ad_device_id"]: r for r in it if r.get("azure_ad_device_id")
    }

    for row in en:
        if not row.get("serial") and row.get("device_id") in intune_by_azure_id:
            row["serial"] = intune_by_azure_id[row["device_id"]]["serial"]

    if windows_only:
        it = [r for r in it if _is_windows_intune(r)]
        en = [r for r in en if _is_windows_entra(r)]
        meta["counts_filtered"] = {
            "autopilot": len(ap),
            "intune": len(it),
            "entra": len(en),
        }
        meta["filter"] = "windows_only"

    serials: dict[str, dict] = {}

    def bucket(serial: str | None) -> dict:
        key = serial or ""
        if key not in serials:
            serials[key] = {
                "serial": key,
                "autopilot": None,
                "intune": None,
                "entra": [],
            }
        return serials[key]

    for row in ap:
        bucket(row["serial"])["autopilot"] = row
    for row in it:
        bucket(row.get("serial"))["intune"] = row

    unmatched: list[dict] = []
    for row in en:
        if row.get("serial"):
            bucket(row["serial"])["entra"].append(row)
        else:
            unmatched.append(row)

    groups = sorted(
        (group for group in serials.values() if group["serial"]),
        key=lambda group: group["serial"],
    )
    return groups, {"meta": meta, "unmatched": unmatched}


def list_unmatched_entra() -> list[dict]:
    _groups, extra = list_grouped(windows_only=False)
    return extra["unmatched"]


def record_deletion(
    source: str,
    object_id: str,
    serial: str = "",
    display_name: str = "",
    status: str = "ok",
    message: str = "",
) -> dict:
    with db_pg.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO deletions
                (deleted_at, source, object_id, serial, display_name, status, message)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (_now(), source, object_id, serial, display_name, status, message),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)


def list_deletions(limit: int = 100) -> list[dict]:
    with db_pg.connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM deletions
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def recent_deletions(limit: int = 50) -> list[dict]:
    return list_deletions(limit=limit)
