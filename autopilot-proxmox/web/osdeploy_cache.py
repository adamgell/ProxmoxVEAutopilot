"""PostgreSQL repository for OSDeploy server image/update cache state."""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any
import urllib.request

from psycopg import Connection
from psycopg.types.json import Jsonb


_INIT_LOCK = Lock()
_INIT_DONE = False
_INIT_LOCK_KEY = "proxmoxveautopilot:osdeploy_cache:init"

DEFAULT_CACHE_ROOT = Path("/app/cache/osdeploy")
MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024
SERVER_IMAGE_SEEDS = (
    ("Windows Server 2025", "Datacenter", "en-us"),
    ("Windows Server 2025", "Standard", "en-us"),
    ("Windows Server 2022", "Datacenter", "en-us"),
    ("Windows Server 2022", "Standard", "en-us"),
)


class CacheError(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS osdeploy_cache_entries (
    id uuid PRIMARY KEY,
    entry_type text NOT NULL,
    status text NOT NULL,
    windows_version text NOT NULL,
    architecture text NOT NULL,
    edition text NULL,
    language text NULL,
    kb text NULL,
    title text NULL,
    file_name text NOT NULL,
    source_url text NULL,
    expected_size_bytes bigint NULL,
    expected_sha256 text NULL,
    sha256 text NULL,
    size_bytes bigint NULL,
    local_path text NULL,
    last_job_id text NULL,
    verified_at timestamptz NULL,
    last_served_at timestamptz NULL,
    served_count integer NOT NULL DEFAULT 0,
    error text NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_osdeploy_cache_status
    ON osdeploy_cache_entries(status, entry_type, windows_version);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS osdeploy_cache_entries CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _stable_id(*parts: str) -> str:
    key = "|".join(str(part or "") for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"proxmoxveautopilot:osdeploy-cache:{key}"))


def _json(value: Any) -> Jsonb:
    return Jsonb(value or {})


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _entry_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    out["metadata"] = out.pop("metadata_json") or {}
    for key in ("verified_at", "last_served_at", "created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def init(conn: Connection) -> None:
    global _INIT_DONE
    with _INIT_LOCK:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_INIT_LOCK_KEY,))
        conn.execute(SCHEMA)
        conn.commit()
        _INIT_DONE = True


def reset_for_tests(conn: Connection) -> None:
    global _INIT_DONE
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()
    _INIT_DONE = False


def cache_root() -> Path:
    return Path(os.environ.get("AUTOPILOT_OSDEPLOY_CACHE_ROOT", str(DEFAULT_CACHE_ROOT))).expanduser()


def upsert_entry(conn: Connection, values: dict) -> dict:
    now = _now()
    entry_id = values.get("id") or _new_id()
    row = conn.execute(
        """
        INSERT INTO osdeploy_cache_entries (
            id, entry_type, status, windows_version, architecture, edition,
            language, kb, title, file_name, source_url, expected_size_bytes,
            expected_sha256, sha256, size_bytes, local_path, last_job_id,
            verified_at, last_served_at, served_count, error, metadata_json,
            created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (id) DO UPDATE SET
            entry_type = EXCLUDED.entry_type,
            status = EXCLUDED.status,
            windows_version = EXCLUDED.windows_version,
            architecture = EXCLUDED.architecture,
            edition = EXCLUDED.edition,
            language = EXCLUDED.language,
            kb = EXCLUDED.kb,
            title = EXCLUDED.title,
            file_name = EXCLUDED.file_name,
            source_url = EXCLUDED.source_url,
            expected_size_bytes = EXCLUDED.expected_size_bytes,
            expected_sha256 = EXCLUDED.expected_sha256,
            sha256 = EXCLUDED.sha256,
            size_bytes = EXCLUDED.size_bytes,
            local_path = EXCLUDED.local_path,
            last_job_id = EXCLUDED.last_job_id,
            verified_at = EXCLUDED.verified_at,
            last_served_at = EXCLUDED.last_served_at,
            served_count = EXCLUDED.served_count,
            error = EXCLUDED.error,
            metadata_json = EXCLUDED.metadata_json,
            updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (
            entry_id,
            values["entry_type"],
            values.get("status") or "discovered",
            values["windows_version"],
            values.get("architecture") or "amd64",
            values.get("edition"),
            values.get("language"),
            values.get("kb"),
            values.get("title"),
            values["file_name"],
            values.get("source_url"),
            values.get("expected_size_bytes"),
            values.get("expected_sha256"),
            values.get("sha256"),
            values.get("size_bytes"),
            values.get("local_path"),
            values.get("last_job_id"),
            values.get("verified_at"),
            values.get("last_served_at"),
            int(values.get("served_count") or 0),
            values.get("error"),
            _json(values.get("metadata")),
            now,
            now,
        ),
    ).fetchone()
    return _entry_row(row)


def list_entries(conn: Connection, *, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM osdeploy_cache_entries
        ORDER BY updated_at DESC, created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [_entry_row(row) for row in rows]


def get_entry(conn: Connection, entry_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM osdeploy_cache_entries WHERE id = %s",
        (entry_id,),
    ).fetchone()
    return _entry_row(row)


def refresh_catalog(conn: Connection) -> dict:
    server_images = []
    quality_updates = []
    for windows_version, edition, language in SERVER_IMAGE_SEEDS:
        safe_version = re.sub(r"[^a-z0-9]+", "-", windows_version.lower()).strip("-")
        safe_edition = re.sub(r"[^a-z0-9]+", "-", edition.lower()).strip("-")
        server_images.append(
            upsert_entry(
                conn,
                {
                    "id": _stable_id("server_image", windows_version, "amd64", edition, language),
                    "entry_type": "server_image",
                    "status": "discovered",
                    "windows_version": windows_version,
                    "architecture": "amd64",
                    "edition": edition,
                    "language": language,
                    "title": f"{windows_version} {edition} installation media",
                    "file_name": f"{safe_version}-{safe_edition}-{language}.iso",
                    "source_url": "manual://microsoft-volume-licensing-or-eval-center",
                    "metadata": {
                        "factory": "OSDeploy/OSDBuilder",
                        "content_role": "source_media",
                        "requires_admin_dism_host": True,
                    },
                },
            )
        )
        quality_updates.append(
            upsert_entry(
                conn,
                {
                    "id": _stable_id("quality_update", windows_version, "amd64", edition, language),
                    "entry_type": "quality_update",
                    "status": "discovered",
                    "windows_version": windows_version,
                    "architecture": "amd64",
                    "edition": edition,
                    "language": language,
                    "title": f"Latest cumulative update for {windows_version} {edition}",
                    "file_name": f"{safe_version}-{safe_edition}-latest-quality.msu",
                    "source_url": "manual://microsoft-update-catalog",
                    "metadata": {
                        "factory": "OSDeploy/OSDBuilder",
                        "content_role": "offline_servicing_update",
                    },
                },
            )
        )
    conn.commit()
    return {"server_images": server_images, "quality_updates": quality_updates}


def mark_status(
    conn: Connection,
    entry_id: str,
    *,
    status: str,
    error: str | None = None,
    local_path: str | None = None,
    size_bytes: int | None = None,
    sha256_value: str | None = None,
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE osdeploy_cache_entries
        SET status = %s,
            error = %s,
            local_path = COALESCE(%s, local_path),
            size_bytes = COALESCE(%s, size_bytes),
            sha256 = COALESCE(%s, sha256),
            verified_at = CASE WHEN %s = 'ready' THEN %s ELSE verified_at END,
            updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (
            status,
            error,
            local_path,
            size_bytes,
            sha256_value,
            status,
            now,
            now,
            entry_id,
        ),
    ).fetchone()
    conn.commit()
    return _entry_row(row)


def _ensure_cache_root(min_required_bytes: int = MIN_FREE_BYTES) -> Path:
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if free < min_required_bytes:
        raise CacheError(f"OSDeploy cache root has insufficient free space: {free} bytes available")
    return root


def _entry_storage_path(root: Path, entry: dict) -> Path:
    safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "-", entry["windows_version"]).strip("-")
    safe_type = re.sub(r"[^A-Za-z0-9_.-]+", "-", entry["entry_type"]).strip("-")
    safe_file = Path(entry["file_name"]).name
    return root / safe_type / safe_version / safe_file


def warm_entry(conn: Connection, entry_id: str) -> dict:
    entry = get_entry(conn, entry_id)
    if not entry:
        raise CacheError(f"OSDeploy cache entry not found: {entry_id}")
    source_url = str(entry.get("source_url") or "")
    if source_url.startswith("manual://"):
        raise CacheError(f"manual source must be staged before warming: {source_url}")
    expected_size = int(entry.get("expected_size_bytes") or 0)
    min_required = max(MIN_FREE_BYTES, expected_size + 512 * 1024 * 1024)
    root = _ensure_cache_root(min_required)
    mark_status(conn, entry_id, status="warming", error=None)
    destination = _entry_storage_path(root, entry)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex}.tmp")
    hasher = sha256()
    size = 0
    try:
        request = urllib.request.Request(
            source_url,
            headers={"User-Agent": "ProxmoxVEAutopilot/OSDeployCache"},
        )
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
        actual_sha256 = hasher.hexdigest().lower()
        expected_sha256 = str(entry.get("expected_sha256") or "").lower()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise CacheError(f"SHA256 mismatch expected={expected_sha256} actual={actual_sha256}")
        if expected_size and size != expected_size:
            raise CacheError(f"size mismatch expected={expected_size} actual={size}")
        tmp.replace(destination)
        return mark_status(
            conn,
            entry_id,
            status="ready",
            error=None,
            local_path=str(destination),
            size_bytes=size,
            sha256_value=actual_sha256,
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        mark_status(conn, entry_id, status="failed", error=str(exc))
        raise


def verify_entry(conn: Connection, entry_id: str) -> dict:
    entry = get_entry(conn, entry_id)
    if not entry:
        raise CacheError(f"OSDeploy cache entry not found: {entry_id}")
    path = Path(entry.get("local_path") or "")
    if not path.is_file():
        return mark_status(conn, entry_id, status="missing", error=f"cache file missing: {path}")
    hasher = sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
            size += len(chunk)
    actual_sha256 = hasher.hexdigest().lower()
    expected_size = int(entry.get("expected_size_bytes") or 0)
    if expected_size and size != expected_size:
        return mark_status(
            conn,
            entry_id,
            status="failed",
            error=f"size mismatch expected={expected_size} actual={size}",
            size_bytes=size,
            sha256_value=actual_sha256,
        )
    expected_sha256 = str(entry.get("expected_sha256") or "").lower()
    if expected_sha256 and actual_sha256 != expected_sha256:
        return mark_status(
            conn,
            entry_id,
            status="failed",
            error=f"SHA256 mismatch expected={expected_sha256} actual={actual_sha256}",
            size_bytes=size,
            sha256_value=actual_sha256,
        )
    return mark_status(
        conn,
        entry_id,
        status="ready",
        error=None,
        local_path=str(path),
        size_bytes=size,
        sha256_value=actual_sha256,
    )


def delete_entry_file(conn: Connection, entry_id: str) -> dict:
    entry = get_entry(conn, entry_id)
    if not entry:
        raise CacheError(f"OSDeploy cache entry not found: {entry_id}")
    path = Path(entry.get("local_path") or "")
    if path.is_file():
        path.unlink()
    row = conn.execute(
        """
        UPDATE osdeploy_cache_entries
        SET status = 'missing',
            error = NULL,
            local_path = NULL,
            size_bytes = NULL,
            sha256 = NULL,
            verified_at = NULL,
            updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (_now(), entry_id),
    ).fetchone()
    conn.commit()
    return _entry_row(row)


def mark_served(conn: Connection, entry_id: str) -> dict | None:
    row = conn.execute(
        """
        UPDATE osdeploy_cache_entries
        SET last_served_at = %s,
            served_count = served_count + 1,
            updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (_now(), _now(), entry_id),
    ).fetchone()
    conn.commit()
    return _entry_row(row)


def payload(conn: Connection) -> dict:
    entries = list_entries(conn)
    statuses = {}
    for entry in entries:
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    root = cache_root()
    usage = shutil.disk_usage(root if root.exists() else Path("/"))
    return {
        "schema_version": 1,
        "storage": {
            "root": str(root),
            "ready": root.exists(),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
        },
        "summary": {
            "total": len(entries),
            "ready": statuses.get("ready", 0),
            "warming": statuses.get("warming", 0),
            "failed": statuses.get("failed", 0),
        },
        "entries": entries,
    }
