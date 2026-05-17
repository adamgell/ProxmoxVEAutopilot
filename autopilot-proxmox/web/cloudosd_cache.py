"""CloudOSD OS image and quality update cache repository."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import cloudosd_pg, db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS cloudosd_cache_entries (
    id uuid PRIMARY KEY,
    entry_type text NOT NULL,
    status text NOT NULL,
    osdcloud_module_version text NULL,
    windows_version text NOT NULL,
    release_id text NULL,
    build text NULL,
    architecture text NOT NULL,
    language text NULL,
    activation text NULL,
    edition text NULL,
    title text NULL,
    kb text NULL,
    catalog_guid text NULL,
    catalog_file text NULL,
    file_name text NOT NULL,
    source_url text NOT NULL,
    local_path text NULL,
    size_bytes bigint NULL,
    expected_size_bytes bigint NULL,
    sha1 text NULL,
    sha256 text NULL,
    expected_sha1 text NULL,
    expected_sha256 text NULL,
    error text NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    warmed_at timestamptz NULL,
    verified_at timestamptz NULL,
    last_served_at timestamptz NULL,
    served_count integer NOT NULL DEFAULT 0,
    last_checked_at timestamptz NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE(entry_type, windows_version, architecture, language, activation, edition, file_name)
);
CREATE INDEX IF NOT EXISTS idx_cloudosd_cache_entries_lookup
    ON cloudosd_cache_entries(entry_type, windows_version, architecture, language, activation, edition, status);
CREATE INDEX IF NOT EXISTS idx_cloudosd_cache_entries_status
    ON cloudosd_cache_entries(status, updated_at DESC);
"""


RESET_SQL = "DROP TABLE IF EXISTS cloudosd_cache_entries CASCADE"

DEFAULT_CACHE_ROOT = "/app/cache/cloudosd"
MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024
OSDCLOUD_PACKAGE_URL = "https://www.powershellgallery.com/api/v2/package/OSDCloud/{version}"
MICROSOFT_CATALOG_SEARCH_URL = "https://www.catalog.update.microsoft.com/Search.aspx?q={query}"
MICROSOFT_CATALOG_DOWNLOAD_URL = "https://www.catalog.update.microsoft.com/DownloadDialog.aspx"
WINDOWS_11_VERSIONS = [v for v in cloudosd_pg.OS_VERSION_CATALOG if v.startswith("Windows 11 ")]
ARCH_TO_OSDCLOUD = {"amd64": "x64", "arm64": "ARM64"}
EDITION_TO_ID = {
    "Home": "Core",
    "Home N": "CoreN",
    "Education": "Education",
    "Education N": "EducationN",
    "Pro": "Professional",
    "Pro N": "ProfessionalN",
    "Enterprise": "Enterprise",
    "Enterprise N": "EnterpriseN",
}


class CacheError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value.obj
    return value


def _row(row: dict | None) -> dict | None:
    if not row:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    out["metadata"] = _json_value(out.pop("metadata_json", {})) or {}
    for key in (
        "warmed_at",
        "verified_at",
        "last_served_at",
        "last_checked_at",
        "created_at",
        "updated_at",
    ):
        out[key] = _iso(out.get(key))
    out["ready"] = out.get("status") == "ready"
    return out


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(RESET_SQL)
    conn.commit()


def cache_root() -> Path:
    return Path(os.environ.get("AUTOPILOT_CLOUDOSD_CACHE_ROOT", DEFAULT_CACHE_ROOT)).expanduser()


def storage_summary(root: Path | None = None) -> dict:
    root = root or cache_root()
    exists = root.exists()
    stat = shutil.disk_usage(root if exists else root.parent if root.parent.exists() else Path("/"))
    return {
        "root": str(root),
        "exists": exists,
        "total_bytes": stat.total,
        "used_bytes": stat.used,
        "free_bytes": stat.free,
        "minimum_free_bytes": MIN_FREE_BYTES,
        "ready": exists and stat.free >= MIN_FREE_BYTES,
    }


def _ensure_cache_root(min_required_bytes: int = MIN_FREE_BYTES) -> Path:
    root = cache_root()
    if not root.exists():
        raise CacheError(f"CloudOSD cache root is missing: {root}")
    free = shutil.disk_usage(root).free
    if free < min_required_bytes:
        raise CacheError(
            f"CloudOSD cache root has insufficient free space: {free} bytes available"
        )
    return root


def _download_bytes(url: str, *, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ProxmoxVEAutopilot/CloudOSDCache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _osdcloud_nupkg(version: str) -> bytes:
    return _download_bytes(OSDCLOUD_PACKAGE_URL.format(version=version), timeout=120)


def _version_release(windows_version: str) -> str:
    match = re.search(r"(\d{2}H\d)", windows_version, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _build_from_catalog_name(name: str) -> str:
    match = re.search(r"(\d{5})\.(\d+)", name)
    return match.group(0) if match else ""


def _windows_version_for_catalog(name: str) -> str | None:
    lower = name.lower()
    if "win11-21h2" in lower:
        return "Windows 11 21H2"
    if "win11-22h2" in lower:
        return "Windows 11 22H2"
    if "win11-23h2" in lower:
        return "Windows 11 23H2"
    if "win11-24h2" in lower:
        return "Windows 11 24H2"
    if "win11-25h2" in lower:
        return "Windows 11 25H2"
    return None


def _activation_from_file_name(file_name: str) -> str:
    name = file_name.upper()
    if "_VOL_" in name:
        return "Volume"
    if "_RET_" in name:
        return "Retail"
    return "Unknown"


def _upsert_entry(conn: Connection, values: dict) -> dict:
    now = _now()
    entry_id = values.get("id") or str(uuid.uuid4())
    row = conn.execute(
        """
        INSERT INTO cloudosd_cache_entries (
            id, entry_type, status, osdcloud_module_version, windows_version,
            release_id, build, architecture, language, activation, edition,
            title, kb, catalog_guid, catalog_file, file_name, source_url,
            local_path, size_bytes, expected_size_bytes, sha1, sha256,
            expected_sha1, expected_sha256, error, metadata_json,
            warmed_at, verified_at, last_checked_at, created_at, updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (entry_type, windows_version, architecture, language, activation, edition, file_name)
        DO UPDATE SET
            status = CASE
                WHEN cloudosd_cache_entries.status = 'ready' THEN cloudosd_cache_entries.status
                ELSE EXCLUDED.status
            END,
            osdcloud_module_version = EXCLUDED.osdcloud_module_version,
            release_id = EXCLUDED.release_id,
            build = EXCLUDED.build,
            title = EXCLUDED.title,
            kb = EXCLUDED.kb,
            catalog_guid = EXCLUDED.catalog_guid,
            catalog_file = EXCLUDED.catalog_file,
            source_url = EXCLUDED.source_url,
            expected_size_bytes = EXCLUDED.expected_size_bytes,
            expected_sha1 = EXCLUDED.expected_sha1,
            expected_sha256 = EXCLUDED.expected_sha256,
            metadata_json = EXCLUDED.metadata_json,
            last_checked_at = EXCLUDED.last_checked_at,
            updated_at = EXCLUDED.updated_at
        RETURNING *
        """,
        (
            entry_id,
            values["entry_type"],
            values.get("status", "missing"),
            values.get("osdcloud_module_version"),
            values["windows_version"],
            values.get("release_id"),
            values.get("build"),
            values.get("architecture", "amd64"),
            values.get("language"),
            values.get("activation"),
            values.get("edition"),
            values.get("title"),
            values.get("kb"),
            values.get("catalog_guid"),
            values.get("catalog_file"),
            values["file_name"],
            values["source_url"],
            values.get("local_path"),
            values.get("size_bytes"),
            values.get("expected_size_bytes"),
            values.get("sha1"),
            values.get("sha256"),
            values.get("expected_sha1"),
            values.get("expected_sha256"),
            values.get("error"),
            Jsonb(values.get("metadata") or {}),
            values.get("warmed_at"),
            values.get("verified_at"),
            now,
            now,
            now,
        ),
    ).fetchone()
    return _row(row)


def list_entries(
    conn: Connection,
    *,
    entry_type: str | None = None,
    windows_version: str | None = None,
    limit: int = 500,
) -> list[dict]:
    clauses = []
    params: list[Any] = []
    if entry_type:
        clauses.append("entry_type = %s")
        params.append(entry_type)
    if windows_version:
        clauses.append("windows_version = %s")
        params.append(windows_version)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM cloudosd_cache_entries
        {where}
        ORDER BY
            CASE entry_type WHEN 'feature_image' THEN 0 ELSE 1 END,
            windows_version DESC,
            architecture,
            language NULLS LAST,
            activation NULLS LAST,
            edition NULLS LAST,
            file_name
        LIMIT %s
        """,
        (*params, max(1, min(int(limit or 500), 2000))),
    ).fetchall()
    return [_row(row) for row in rows]


def get_entry(conn: Connection, entry_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM cloudosd_cache_entries WHERE id = %s",
        (entry_id,),
    ).fetchone()
    return _row(row)


def find_feature_entry(
    conn: Connection,
    *,
    windows_version: str,
    architecture: str,
    language: str,
    activation: str,
    edition: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM cloudosd_cache_entries
        WHERE entry_type = 'feature_image'
          AND windows_version = %s
          AND architecture = %s
          AND language = %s
          AND activation = %s
          AND edition = %s
        ORDER BY
          CASE status WHEN 'ready' THEN 0 WHEN 'warming' THEN 1 WHEN 'missing' THEN 2 ELSE 3 END,
          updated_at DESC
        LIMIT 1
        """,
        (windows_version, architecture, language, activation, edition),
    ).fetchone()
    return _row(row)


def _find_feature_in_nupkg(
    *,
    module_version: str,
    windows_version: str,
    architecture: str,
    language: str,
    activation: str,
    edition: str,
) -> dict | None:
    package = _osdcloud_nupkg(module_version)
    arch = ARCH_TO_OSDCLOUD.get(architecture, architecture)
    edition_id = EDITION_TO_ID.get(edition, edition)
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        handle.write(package)
        package_path = handle.name
    try:
        with zipfile.ZipFile(package_path) as archive:
            for name in archive.namelist():
                if not name.startswith("catalogs/operatingsystem/") or not name.endswith(".xml"):
                    continue
                if _windows_version_for_catalog(name) != windows_version:
                    continue
                root = ET.fromstring(archive.read(name))
                for node in root.findall(".//File"):
                    data = {child.tag: (child.text or "") for child in list(node)}
                    if data.get("Architecture") != arch:
                        continue
                    if data.get("LanguageCode") != language:
                        continue
                    if data.get("Edition") != edition_id:
                        continue
                    if _activation_from_file_name(data.get("FileName", "")) != activation:
                        continue
                    return {
                        "catalog_file": name,
                        "file_name": data["FileName"],
                        "source_url": data["FilePath"],
                        "expected_size_bytes": int(data.get("Size") or 0) or None,
                        "expected_sha256": (data.get("Sha256") or "").lower() or None,
                        "build": _build_from_catalog_name(data["FileName"]),
                        "metadata": {
                            "osdcloud_catalog": name,
                            "osdcloud_architecture": data.get("Architecture"),
                            "osdcloud_edition": data.get("Edition"),
                            "is_retail_only": data.get("IsRetailOnly"),
                        },
                    }
    finally:
        Path(package_path).unlink(missing_ok=True)
    return None


def ensure_feature_entry(
    conn: Connection,
    *,
    module_version: str,
    windows_version: str,
    architecture: str = "amd64",
    language: str = cloudosd_pg.DEFAULT_OS_LANGUAGE,
    activation: str = cloudosd_pg.DEFAULT_OS_ACTIVATION,
    edition: str = cloudosd_pg.DEFAULT_OS_EDITION,
) -> dict | None:
    existing = conn.execute(
        """
        SELECT *
        FROM cloudosd_cache_entries
        WHERE entry_type = 'feature_image'
          AND windows_version = %s
          AND architecture = %s
          AND language = %s
          AND activation = %s
          AND edition = %s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (windows_version, architecture, language, activation, edition),
    ).fetchone()
    if existing:
        return _row(existing)
    found = _find_feature_in_nupkg(
        module_version=module_version,
        windows_version=windows_version,
        architecture=architecture,
        language=language,
        activation=activation,
        edition=edition,
    )
    if not found:
        return None
    return _upsert_entry(
        conn,
        {
            "entry_type": "feature_image",
            "status": "missing",
            "osdcloud_module_version": module_version,
            "windows_version": windows_version,
            "release_id": _version_release(windows_version),
            "architecture": architecture,
            "language": language,
            "activation": activation,
            "edition": edition,
            **found,
        },
    )


def _catalog_search(query: str) -> str:
    url = MICROSOFT_CATALOG_SEARCH_URL.format(query=urllib.parse.quote(query))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", "replace")


def _parse_catalog_rows(page: str) -> list[dict]:
    rows = []
    for match in re.finditer(
        r'<tr id="(?P<guid>[0-9a-fA-F-]{36})_R\d+".*?</tr>',
        page,
        flags=re.S,
    ):
        raw = match.group(0)
        guid = match.group("guid")
        title_match = re.search(rf"{guid}_link'.*?>(.*?)</a>", raw, flags=re.S)
        size_match = re.search(rf'{guid}_originalSize">(\d+)</span>', raw)
        date_match = re.search(rf'{guid}_C4_R\d+">\s*([^<]+)\s*</td>', raw)
        classification_match = re.search(rf'{guid}_C3_R\d+">\s*([^<]+)\s*</td>', raw)
        if not title_match:
            continue
        title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
        kb_match = re.search(r"\((KB\d+)\)", title, re.I)
        rows.append({
            "guid": guid,
            "title": title,
            "kb": kb_match.group(1).upper() if kb_match else "",
            "size_bytes": int(size_match.group(1)) if size_match else None,
            "last_updated": (date_match.group(1).strip() if date_match else ""),
            "classification": (
                html.unescape(classification_match.group(1)).strip()
                if classification_match
                else ""
            ),
        })
    return rows


def _download_links_for_guid(guid: str) -> list[str]:
    update_ids = json.dumps([{"size": 0, "languages": "", "uidInfo": guid, "updateID": guid}])
    body = urllib.parse.urlencode({"updateIDs": update_ids}).encode()
    request = urllib.request.Request(
        MICROSOFT_CATALOG_DOWNLOAD_URL,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        page = response.read().decode("utf-8", "replace")
    links = [
        html.unescape(url)
        for url in re.findall(r"https?://[^\"']+", page)
        if "download.windowsupdate.com" in url or "dl.delivery.mp.microsoft.com" in url
    ]
    return list(dict.fromkeys(links))


def _sha1_from_update_file_name(file_name: str) -> str | None:
    match = re.search(r"_([0-9a-fA-F]{40})\.(?:msu|cab)$", file_name)
    return match.group(1).lower() if match else None


def refresh_quality_entry(
    conn: Connection,
    *,
    windows_version: str,
    architecture: str = "amd64",
) -> list[dict]:
    release = _version_release(windows_version)
    arch_query = "x64-based Systems" if architecture == "amd64" else f"{architecture}-based Systems"
    query = f"Cumulative Update for Windows 11 Version {release} for {arch_query}"
    rows = _parse_catalog_rows(_catalog_search(query))
    candidates = [
        row for row in rows
        if "Cumulative Update" in row["title"]
        and "Windows 11" in row["title"]
        and "Preview" not in row["title"]
        and "Dynamic" not in row["title"]
        and release in row["title"]
        and ("x64" in row["title"] if architecture == "amd64" else architecture in row["title"])
    ]
    if not candidates:
        return []
    selected = candidates[0]
    entries = []
    for link in _download_links_for_guid(selected["guid"]):
        file_name = urllib.parse.unquote(urllib.parse.urlparse(link).path.split("/")[-1])
        if not file_name.lower().endswith((".msu", ".cab")):
            continue
        if architecture == "amd64" and "x64" not in file_name.lower():
            continue
        entry = _upsert_entry(
            conn,
            {
                "entry_type": "quality_update",
                "status": "missing",
                "windows_version": windows_version,
                "release_id": release,
                "architecture": architecture,
                "language": "neutral",
                "activation": "all",
                "edition": "all",
                "title": selected["title"],
                "kb": selected["kb"],
                "catalog_guid": selected["guid"],
                "file_name": file_name,
                "source_url": link,
                "expected_size_bytes": None,
                "expected_sha1": _sha1_from_update_file_name(file_name),
                "metadata": {
                    "classification": selected.get("classification") or "",
                    "last_updated": selected.get("last_updated") or "",
                    "catalog_query": query,
                    "catalog_size_bytes": selected.get("size_bytes"),
                },
            },
        )
        entries.append(entry)
    return entries


def refresh_catalog(conn: Connection, *, module_version: str = cloudosd_pg.DEFAULT_OSDCLOUD_MODULE_VERSION) -> dict:
    feature_entries = []
    quality_entries = []
    for windows_version in WINDOWS_11_VERSIONS:
        feature = ensure_feature_entry(
            conn,
            module_version=module_version,
            windows_version=windows_version,
            architecture=cloudosd_pg.DEFAULT_ARCHITECTURE,
            language=cloudosd_pg.DEFAULT_OS_LANGUAGE,
            activation=cloudosd_pg.DEFAULT_OS_ACTIVATION,
            edition=cloudosd_pg.DEFAULT_OS_EDITION,
        )
        if feature:
            feature_entries.append(feature)
        try:
            quality_entries.extend(refresh_quality_entry(conn, windows_version=windows_version))
        except Exception as exc:
            _upsert_entry(
                conn,
                {
                    "entry_type": "quality_update",
                    "status": "failed",
                    "windows_version": windows_version,
                    "release_id": _version_release(windows_version),
                    "architecture": cloudosd_pg.DEFAULT_ARCHITECTURE,
                    "language": "neutral",
                    "activation": "all",
                    "edition": "all",
                    "file_name": f"{windows_version.replace(' ', '-')}-latest-quality.msu",
                    "source_url": "about:blank",
                    "error": str(exc),
                },
            )
    conn.commit()
    return {
        "feature_images": feature_entries,
        "quality_updates": quality_entries,
    }


def mark_status(
    conn: Connection,
    entry_id: str,
    *,
    status: str,
    error: str | None = None,
    local_path: str | None = None,
    size_bytes: int | None = None,
    sha1_value: str | None = None,
    sha256_value: str | None = None,
) -> dict | None:
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_cache_entries
        SET status = %s,
            error = %s,
            local_path = COALESCE(%s, local_path),
            size_bytes = COALESCE(%s, size_bytes),
            sha1 = COALESCE(%s, sha1),
            sha256 = COALESCE(%s, sha256),
            warmed_at = CASE WHEN %s = 'ready' THEN COALESCE(warmed_at, %s) ELSE warmed_at END,
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
            sha1_value,
            sha256_value,
            status,
            now,
            status,
            now,
            now,
            entry_id,
        ),
    ).fetchone()
    conn.commit()
    return _row(row)


def _entry_storage_path(root: Path, entry: dict) -> Path:
    safe_version = re.sub(r"[^A-Za-z0-9_.-]+", "-", entry["windows_version"]).strip("-")
    safe_type = re.sub(r"[^A-Za-z0-9_.-]+", "-", entry["entry_type"]).strip("-")
    safe_file = Path(entry["file_name"]).name
    return root / safe_type / safe_version / safe_file


def warm_entry(conn: Connection, entry_id: str) -> dict:
    entry = get_entry(conn, entry_id)
    if not entry:
        raise CacheError(f"CloudOSD cache entry not found: {entry_id}")
    expected_size = int(entry.get("expected_size_bytes") or 0)
    min_required = max(MIN_FREE_BYTES, expected_size + 512 * 1024 * 1024)
    root = _ensure_cache_root(min_required)
    mark_status(conn, entry_id, status="warming", error=None)
    destination = _entry_storage_path(root, entry)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + f".{uuid.uuid4().hex}.tmp")
    sha1_hasher = sha1()
    sha256_hasher = sha256()
    size = 0
    try:
        request = urllib.request.Request(
            entry["source_url"],
            headers={"User-Agent": "ProxmoxVEAutopilot/CloudOSDCache"},
        )
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                sha1_hasher.update(chunk)
                sha256_hasher.update(chunk)
                size += len(chunk)
        actual_sha1 = sha1_hasher.hexdigest()
        actual_sha256 = sha256_hasher.hexdigest()
        expected_sha1 = (entry.get("expected_sha1") or "").lower()
        if expected_sha1 and actual_sha1.lower() != expected_sha1:
            raise CacheError(
                f"SHA1 mismatch expected={expected_sha1} actual={actual_sha1}"
            )
        expected_sha256 = (entry.get("expected_sha256") or "").lower()
        if expected_sha256 and actual_sha256.lower() != expected_sha256:
            raise CacheError(
                f"SHA256 mismatch expected={expected_sha256} actual={actual_sha256}"
            )
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
            sha1_value=actual_sha1.lower(),
            sha256_value=actual_sha256.lower(),
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        mark_status(conn, entry_id, status="failed", error=str(exc))
        raise


def verify_entry(conn: Connection, entry_id: str) -> dict:
    entry = get_entry(conn, entry_id)
    if not entry:
        raise CacheError(f"CloudOSD cache entry not found: {entry_id}")
    path = Path(entry.get("local_path") or "")
    if not path.is_file():
        return mark_status(conn, entry_id, status="missing", error=f"cache file missing: {path}")
    sha1_hasher = sha1()
    sha256_hasher = sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha1_hasher.update(chunk)
            sha256_hasher.update(chunk)
            size += len(chunk)
    actual_sha1 = sha1_hasher.hexdigest().lower()
    actual_sha256 = sha256_hasher.hexdigest().lower()
    expected_size = int(entry.get("expected_size_bytes") or 0)
    if expected_size and size != expected_size:
        return mark_status(
            conn,
            entry_id,
            status="failed",
            error=f"size mismatch expected={expected_size} actual={size}",
            size_bytes=size,
            sha1_value=actual_sha1,
            sha256_value=actual_sha256,
        )
    expected_sha1 = (entry.get("expected_sha1") or "").lower()
    if expected_sha1 and actual_sha1 != expected_sha1:
        return mark_status(
            conn,
            entry_id,
            status="failed",
            error=f"SHA1 mismatch expected={expected_sha1} actual={actual_sha1}",
            size_bytes=size,
            sha1_value=actual_sha1,
            sha256_value=actual_sha256,
        )
    expected_sha256 = (entry.get("expected_sha256") or "").lower()
    if expected_sha256 and actual_sha256 != expected_sha256:
        return mark_status(
            conn,
            entry_id,
            status="failed",
            error=f"SHA256 mismatch expected={expected_sha256} actual={actual_sha256}",
            size_bytes=size,
            sha1_value=actual_sha1,
            sha256_value=actual_sha256,
        )
    return mark_status(
        conn,
        entry_id,
        status="ready",
        error=None,
        local_path=str(path),
        size_bytes=size,
        sha1_value=actual_sha1,
        sha256_value=actual_sha256,
    )


def delete_entry_file(conn: Connection, entry_id: str) -> dict:
    entry = get_entry(conn, entry_id)
    if not entry:
        raise CacheError(f"CloudOSD cache entry not found: {entry_id}")
    path = Path(entry.get("local_path") or "")
    if path.is_file():
        path.unlink()
    now = _now()
    row = conn.execute(
        """
        UPDATE cloudosd_cache_entries
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
        (now, entry_id),
    ).fetchone()
    conn.commit()
    return _row(row)


def mark_served(conn: Connection, entry_id: str) -> dict | None:
    row = conn.execute(
        """
        UPDATE cloudosd_cache_entries
        SET last_served_at = %s,
            served_count = served_count + 1,
            updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (_now(), _now(), entry_id),
    ).fetchone()
    conn.commit()
    return _row(row)


def matching_quality_updates(conn: Connection, *, windows_version: str, architecture: str = "amd64") -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM cloudosd_cache_entries
        WHERE entry_type = 'quality_update'
          AND windows_version = %s
          AND architecture = %s
          AND status = 'ready'
        ORDER BY kb DESC NULLS LAST, file_name
        """,
        (windows_version, architecture),
    ).fetchall()
    return [_row(row) for row in rows]


def package_cache_payload(
    conn: Connection,
    *,
    run: dict,
    artifact: dict,
    server_base_url: str,
    token: str,
) -> dict:
    feature = find_feature_entry(
        conn,
        windows_version=run["os_version"],
        architecture=run["architecture"],
        language=run["os_language"],
        activation=run["os_activation"],
        edition=run["os_edition"],
    )
    feature_payload = None
    policy = "direct_on_miss"
    if feature:
        download_url = (
            f"{server_base_url}/api/cloudosd/cache/{feature['id']}/download/"
            f"{urllib.parse.quote(feature['file_name'])}?run_id={run['run_id']}&token={urllib.parse.quote(token)}"
        )
        feature_payload = {
            "policy": policy,
            "hit": feature["status"] == "ready",
            "entry_id": feature["id"],
            "status": feature["status"],
            "file_name": feature["file_name"],
            "source_url": feature["source_url"],
            "download_url": download_url if feature["status"] == "ready" else "",
            "expected_sha256": feature.get("expected_sha256") or feature.get("sha256") or "",
            "expected_size_bytes": feature.get("expected_size_bytes"),
            "catalog_file": feature.get("catalog_file") or "",
            "windows_version": feature["windows_version"],
            "architecture": feature["architecture"],
            "language": feature.get("language") or "",
            "activation": feature.get("activation") or "",
            "edition": feature.get("edition") or "",
        }
    quality_payload = []
    for entry in matching_quality_updates(
        conn,
        windows_version=run["os_version"],
        architecture=run["architecture"],
    ):
        quality_payload.append({
            "entry_id": entry["id"],
            "status": entry["status"],
            "title": entry.get("title") or entry["file_name"],
            "kb": entry.get("kb") or "",
            "file_name": entry["file_name"],
            "sha256": entry.get("sha256") or "",
            "size_bytes": entry.get("size_bytes"),
            "url": (
                f"{server_base_url}/api/cloudosd/cache/{entry['id']}/download/"
                f"{urllib.parse.quote(entry['file_name'])}?run_id={run['run_id']}&token={urllib.parse.quote(token)}"
            ),
        })
    return {
        "policy": policy,
        "feature_image": feature_payload,
        "quality_updates": quality_payload,
    }


def payload(conn: Connection) -> dict:
    entries = list_entries(conn)
    grouped: dict[str, dict[str, list[dict]]] = {}
    for entry in entries:
        grouped.setdefault(entry["windows_version"], {"feature_images": [], "quality_updates": []})
        key = "feature_images" if entry["entry_type"] == "feature_image" else "quality_updates"
        grouped[entry["windows_version"]][key].append(entry)
    ready_count = sum(1 for entry in entries if entry["status"] == "ready")
    total_size = sum(int(entry.get("size_bytes") or 0) for entry in entries if entry["status"] == "ready")
    return {
        "schema_version": 1,
        "storage": storage_summary(),
        "entries": entries,
        "grouped": grouped,
        "summary": {
            "total": len(entries),
            "ready": ready_count,
            "warming": sum(1 for entry in entries if entry["status"] == "warming"),
            "failed": sum(1 for entry in entries if entry["status"] == "failed"),
            "total_cached_bytes": total_size,
        },
    }
