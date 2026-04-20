"""Content-addressed cache for per-VM answer ISOs.

Each provision compiles a sequence to a unique ``autounattend.xml``.
Two provisions that compile to the same bytes (same sequence, same
profile, same credentials) share one ISO on Proxmox storage instead
of each building their own. The ISO filename embeds the first 16 hex
chars of the content's SHA-256 so the presence check is a simple
content listing by volid.

Design call: the hash truncated to 16 hex chars (64 bits) is only the
*filename*. The cache row stores the full 64-char digest and compares
that before reuse, so a 64-bit-collision filename can never pull a
wrong ISO from the storage.

Public API:
* :func:`compute_hash` — SHA-256 of the unattend bytes.
* :func:`ensure_iso` — build-and-upload-if-missing, return volid.
* :func:`list_cache` — cache rows enriched with "in_use" flag.
* :func:`prune` — delete selected cache entries + the underlying ISOs.
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import requests


_FILENAME_PREFIX = "autopilot-unattend-"
_FILENAME_SUFFIX = ".iso"


def compute_hash(unattend_bytes: bytes) -> str:
    """Full SHA-256 hex digest of the unattend payload."""
    return hashlib.sha256(unattend_bytes).hexdigest()


def short_hash(full_hash: str) -> str:
    return full_hash[:16]


def _volid_for(iso_storage: str, short: str) -> str:
    return f"{iso_storage}:iso/{_FILENAME_PREFIX}{short}{_FILENAME_SUFFIX}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Lookup / bookkeeping
# ---------------------------------------------------------------------------


def _lookup(db_path: Path, full_hash: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM answer_iso_cache WHERE hash = ?",
            (full_hash,),
        ).fetchone()
    return dict(row) if row else None


def _insert(db_path: Path, *, full_hash: str, short: str, volid: str) -> None:
    with _connect(db_path) as conn:
        now = _now()
        conn.execute(
            "INSERT INTO answer_iso_cache "
            "(hash, short_hash, volid, compiled_at, last_used_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (full_hash, short, volid, now, now),
        )


def _touch(db_path: Path, full_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE answer_iso_cache SET last_used_at = ? WHERE hash = ?",
            (_now(), full_hash),
        )


def _delete_row(db_path: Path, full_hash: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM answer_iso_cache WHERE hash = ?",
                     (full_hash,))


def list_cache(db_path: Path, *, in_use_volids: set[str]) -> list[dict]:
    """Return every cached ISO with an ``in_use`` flag indicating whether
    it's attached to at least one VM (caller supplies the set of volids
    currently referenced from VM configs)."""
    with _connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM answer_iso_cache ORDER BY compiled_at DESC"
        )]
    for r in rows:
        r["in_use"] = r["volid"] in in_use_volids
    return rows


# ---------------------------------------------------------------------------
# ISO build + upload
# ---------------------------------------------------------------------------


def _build_iso(unattend_bytes: bytes, out_iso: Path) -> None:
    """mkisofs the unattend into a tiny ISO labelled OEMDRV so Windows
    Setup picks it up automatically on boot."""
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage"
        stage.mkdir()
        (stage / "autounattend.xml").write_bytes(unattend_bytes)
        subprocess.run(
            ["genisoimage", "-quiet", "-o", str(out_iso),
             "-J", "-r", "-V", "OEMDRV", str(stage)],
            check=True, capture_output=True, text=True,
        )


def _content_listing(*, proxmox_api_get, node: str, iso_storage: str,
                     volid: str) -> bool:
    """Return True if *volid* already exists on the storage (via the
    authenticated _proxmox_api caller)."""
    try:
        entries = proxmox_api_get(
            f"/nodes/{node}/storage/{iso_storage}/content?content=iso"
        ) or []
    except Exception:
        entries = []
    return any(e.get("volid") == volid for e in entries)


def _upload(*, proxmox_config: dict, node: str, iso_storage: str,
            iso_path: Path, iso_filename: str) -> None:
    """POST the built ISO to Proxmox's storage-upload endpoint."""
    host = proxmox_config.get("proxmox_host", "")
    port = proxmox_config.get("proxmox_port", 8006)
    token_id = proxmox_config.get("vault_proxmox_api_token_id", "")
    token_secret = proxmox_config.get("vault_proxmox_api_token_secret", "")
    url = (
        f"https://{host}:{port}/api2/json/nodes/{node}/storage/"
        f"{iso_storage}/upload"
    )
    with open(iso_path, "rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            data={"content": "iso"},
            files={"filename": (iso_filename, fh,
                                "application/x-iso9660-image")},
            verify=proxmox_config.get("proxmox_validate_certs", False),
            timeout=60,
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Proxmox ISO upload failed: HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )


def ensure_iso(*, db_path: Path, unattend_bytes: bytes,
               proxmox_config: dict, proxmox_api_get,
               node: Optional[str] = None,
               iso_storage: Optional[str] = None) -> str:
    """Build-and-upload-if-missing. Return the volid to attach to the VM.

    ``proxmox_api_get`` is a callable that takes a path string and
    returns parsed JSON (the web app's ``_proxmox_api`` helper). It's
    injected so this module doesn't import the FastAPI app and can be
    unit-tested in isolation.
    """
    node = node or proxmox_config.get("proxmox_node", "pve")
    iso_storage = iso_storage or proxmox_config.get("proxmox_iso_storage") \
        or "isos"

    full_hash = compute_hash(unattend_bytes)
    short = short_hash(full_hash)
    filename = f"{_FILENAME_PREFIX}{short}{_FILENAME_SUFFIX}"
    volid = _volid_for(iso_storage, short)

    row = _lookup(db_path, full_hash)
    if row is not None and row["volid"] == volid:
        # Double-check the storage still has the file — the operator
        # may have pruned outside the UI. If missing, fall through to
        # rebuild.
        if _content_listing(proxmox_api_get=proxmox_api_get,
                            node=node, iso_storage=iso_storage,
                            volid=volid):
            _touch(db_path, full_hash)
            return volid
        # Storage drifted from the cache — drop the row so INSERT below
        # doesn't hit the PK.
        _delete_row(db_path, full_hash)

    # Cache miss: build, upload, record.
    with tempfile.TemporaryDirectory() as td:
        iso_path = Path(td) / filename
        _build_iso(unattend_bytes, iso_path)
        _upload(proxmox_config=proxmox_config, node=node,
                iso_storage=iso_storage,
                iso_path=iso_path, iso_filename=filename)

    _insert(db_path, full_hash=full_hash, short=short, volid=volid)
    return volid


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------


def prune(*, db_path: Path, hashes_to_delete: list[str],
          proxmox_config: dict, proxmox_api_delete,
          node: Optional[str] = None,
          iso_storage: Optional[str] = None) -> list[str]:
    """Delete each cache row + its underlying ISO. Returns the hashes
    actually removed (i.e., rows that existed). Missing rows are silently
    skipped; storage-side errors on an individual volid are non-fatal.

    ``proxmox_api_delete`` is the web app's DELETE helper, injected so
    this module stays testable without the full FastAPI surface.
    """
    node = node or proxmox_config.get("proxmox_node", "pve")
    iso_storage = iso_storage or proxmox_config.get("proxmox_iso_storage") \
        or "isos"

    removed: list[str] = []
    for full_hash in hashes_to_delete:
        row = _lookup(db_path, full_hash)
        if not row:
            continue
        volid = row["volid"]
        # URL-safe volid: the storage content endpoint wants the volid
        # as a path segment. urllib.parse.quote handles the ':' and '/'.
        from urllib.parse import quote
        try:
            proxmox_api_delete(
                f"/nodes/{node}/storage/{iso_storage}/content/{quote(volid, safe='')}"
            )
        except Exception:
            # Storage could be offline, volume gone — row removal still
            # makes sense so operator doesn't see a stale entry forever.
            pass
        _delete_row(db_path, full_hash)
        removed.append(full_hash)
    return removed
