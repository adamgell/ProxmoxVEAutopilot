"""SQLite-backed inventory of Autopilot / Intune / Entra device records.

Each source is stored in its own table keyed by the Graph object ID. Serial
number is the cross-source correlation key for Autopilot and Intune; Entra
device objects often lack a serial, so they're additionally joined to Intune
via the Azure AD device ID (`deviceId` == Intune `azureADDeviceId`).

The UI groups everything by serial. Entra records that can't be correlated
back to a serial appear in an "unmatched" bucket.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_devices (
    id TEXT PRIMARY KEY,
    serial TEXT NOT NULL,
    group_tag TEXT,
    profile_status TEXT,
    enrollment_state TEXT,
    manufacturer TEXT,
    model TEXT,
    display_name TEXT,
    last_contact TEXT,
    azure_ad_device_id TEXT,
    raw_json TEXT,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intune_devices (
    id TEXT PRIMARY KEY,
    serial TEXT,
    device_name TEXT,
    os TEXT,
    os_version TEXT,
    user_principal_name TEXT,
    compliance_state TEXT,
    management_state TEXT,
    last_sync TEXT,
    enrolled_date TEXT,
    azure_ad_device_id TEXT,
    raw_json TEXT,
    synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entra_devices (
    id TEXT PRIMARY KEY,
    device_id TEXT,
    serial TEXT,
    ztdid TEXT,
    display_name TEXT,
    operating_system TEXT,
    operating_system_version TEXT,
    trust_type TEXT,
    approximate_last_sign_in TEXT,
    account_enabled INTEGER,
    raw_json TEXT,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_autopilot_serial ON autopilot_devices(serial);
CREATE INDEX IF NOT EXISTS idx_intune_serial    ON intune_devices(serial);
CREATE INDEX IF NOT EXISTS idx_intune_azure_id  ON intune_devices(azure_ad_device_id);
CREATE INDEX IF NOT EXISTS idx_entra_device_id  ON entra_devices(device_id);

CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_at TEXT NOT NULL,
    source TEXT NOT NULL,
    object_id TEXT NOT NULL,
    serial TEXT,
    display_name TEXT,
    status TEXT NOT NULL,
    message TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Additive migrations for DBs created before these columns existed.
        for stmt in (
            "ALTER TABLE autopilot_devices ADD COLUMN azure_ad_device_id TEXT",
            "ALTER TABLE entra_devices ADD COLUMN ztdid TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already present


def _extract_physical_id(raw: dict, prefix: str) -> str:
    # Entra encodes a few bits of Autopilot/Intune linkage inside `physicalIds`
    # as "[<tag>]:<value>" strings — e.g. [SerialNumber]:ABC, [ZTDID]:<guid>.
    for pid in raw.get("physicalIds", []) or []:
        if isinstance(pid, str) and pid.startswith(prefix):
            return pid.split(":", 1)[1]
    return ""


def upsert_autopilot(db_path: Path, devices: Iterable[dict]) -> int:
    now = _now()
    rows = []
    for d in devices:
        rows.append((
            d.get("id", ""),
            d.get("serialNumber", ""),
            d.get("groupTag", ""),
            d.get("deploymentProfileAssignmentStatus", ""),
            d.get("enrollmentState", ""),
            d.get("manufacturer", ""),
            d.get("model", ""),
            d.get("displayName", ""),
            (d.get("lastContactedDateTime") or "")[:19],
            d.get("azureAdDeviceId", "") or d.get("azureActiveDirectoryDeviceId", ""),
            json.dumps(d),
            now,
        ))
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM autopilot_devices")
        conn.executemany(
            "INSERT INTO autopilot_devices "
            "(id, serial, group_tag, profile_status, enrollment_state, "
            " manufacturer, model, display_name, last_contact, "
            " azure_ad_device_id, raw_json, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def upsert_intune(db_path: Path, devices: Iterable[dict]) -> int:
    now = _now()
    rows = []
    for d in devices:
        rows.append((
            d.get("id", ""),
            d.get("serialNumber", ""),
            d.get("deviceName", ""),
            d.get("operatingSystem", ""),
            d.get("osVersion", ""),
            d.get("userPrincipalName", ""),
            d.get("complianceState", ""),
            d.get("managementState", ""),
            (d.get("lastSyncDateTime") or "")[:19],
            (d.get("enrolledDateTime") or "")[:19],
            d.get("azureADDeviceId", ""),
            json.dumps(d),
            now,
        ))
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM intune_devices")
        conn.executemany(
            "INSERT INTO intune_devices "
            "(id, serial, device_name, os, os_version, user_principal_name, "
            " compliance_state, management_state, last_sync, enrolled_date, "
            " azure_ad_device_id, raw_json, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def upsert_entra(db_path: Path, devices: Iterable[dict]) -> int:
    now = _now()
    rows = []
    for d in devices:
        rows.append((
            d.get("id", ""),
            d.get("deviceId", ""),
            _extract_physical_id(d, "[SerialNumber]:"),
            _extract_physical_id(d, "[ZTDID]:"),
            d.get("displayName", ""),
            d.get("operatingSystem", ""),
            d.get("operatingSystemVersion", ""),
            d.get("trustType", ""),
            (d.get("approximateLastSignInDateTime") or "")[:19],
            1 if d.get("accountEnabled") else 0,
            json.dumps(d),
            now,
        ))
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM entra_devices")
        conn.executemany(
            "INSERT INTO entra_devices "
            "(id, device_id, serial, ztdid, display_name, operating_system, "
            " operating_system_version, trust_type, approximate_last_sign_in, "
            " account_enabled, raw_json, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def _is_windows_intune(row: dict) -> bool:
    return (row.get("os") or "").strip().lower().startswith("windows")


def _is_windows_entra(row: dict) -> bool:
    # Entra's operatingSystem spans {"Windows", "IPad", "IPhone", "MacMDM",
    # "AndroidForWork", "AndroidEnterprise", "Unknown", ""}. Match Windows
    # prefix; also keep Unknown records since re-enrolled/stale Windows
    # device objects often report that.
    os = (row.get("operating_system") or "").strip().lower()
    return os.startswith("windows") or os == "unknown" or os == ""


def list_grouped(db_path: Path, *, windows_only: bool = True) -> tuple[list[dict], dict]:
    """Return devices grouped by serial plus unmatched Entra records.

    Each group has: serial, autopilot (row or None), intune (row or None),
    entra (list — a serial can correspond to multiple Entra objects after
    re-enrollment). Unmatched is a list of Entra rows with no serial and no
    deviceId match into Intune.

    `windows_only=True` drops non-Windows Intune and Entra rows at display
    time so the Cloud UI doesn't show iOS/Android/macOS records. Autopilot
    is always Windows-only so it isn't filtered.
    """
    with _connect(db_path) as conn:
        ap  = [dict(r) for r in conn.execute("SELECT * FROM autopilot_devices")]
        it  = [dict(r) for r in conn.execute("SELECT * FROM intune_devices")]
        en  = [dict(r) for r in conn.execute("SELECT * FROM entra_devices")]
        total_counts = {"autopilot": len(ap), "intune": len(it), "entra": len(en)}
        meta = {
            "synced_at": max(
                [r["synced_at"] for r in ap + it + en] or [""]
            ),
            "counts": total_counts,
        }

    intune_by_azure_id = {r["azure_ad_device_id"]: r for r in it if r.get("azure_ad_device_id")}

    # Backfill Entra serial via Intune's azureADDeviceId linkage. Do this
    # BEFORE filtering so Windows Entra records still get their serial even
    # when the correlating Intune record would otherwise be filtered out.
    for r in en:
        if not r.get("serial") and r.get("device_id") in intune_by_azure_id:
            r["serial"] = intune_by_azure_id[r["device_id"]]["serial"]

    if windows_only:
        it = [r for r in it if _is_windows_intune(r)]
        en = [r for r in en if _is_windows_entra(r)]
        meta["counts_filtered"] = {
            "autopilot": len(ap), "intune": len(it), "entra": len(en)
        }
        meta["filter"] = "windows_only"

    serials: dict[str, dict] = {}
    def bucket(serial: str) -> dict:
        key = serial or ""
        if key not in serials:
            serials[key] = {"serial": key, "autopilot": None, "intune": None, "entra": []}
        return serials[key]

    for r in ap:
        bucket(r["serial"])["autopilot"] = r
    for r in it:
        bucket(r["serial"] or "")["intune"] = r
    unmatched: list[dict] = []
    for r in en:
        if r.get("serial"):
            bucket(r["serial"])["entra"].append(r)
        else:
            unmatched.append(r)

    groups = sorted(
        (g for g in serials.values() if g["serial"]),
        key=lambda g: g["serial"],
    )
    return groups, {"meta": meta, "unmatched": unmatched}


def record_deletion(db_path: Path, *, source: str, object_id: str,
                    serial: str = "", display_name: str = "",
                    status: str = "ok", message: str = "") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO deletions "
            "(deleted_at, source, object_id, serial, display_name, status, message) "
            "VALUES (?,?,?,?,?,?,?)",
            (_now(), source, object_id, serial, display_name, status, message),
        )


def recent_deletions(db_path: Path, limit: int = 50) -> list[dict]:
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM deletions ORDER BY id DESC LIMIT ?", (limit,)
        )]
