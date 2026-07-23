"""Filesystem + HTTP-redirect helpers extracted from web.app.

Leaf module: depends only on stdlib + FastAPI. It must never import
``web.app`` (that would recreate the import cycle these extractions exist
to break). ``web.app`` re-exports every public name here so existing
``web_app._safe_path`` / ``monkeypatch.setattr(web.app, ...)`` call sites
keep resolving through the ``web.app`` namespace.

One landmine to remember: constants read *inside* a moved function
(``FILE_SHELF_MAX_BYTES``, ``FILE_SHELF_UPLOAD_CHUNK_BYTES``) now live
here, so a test that wants to shrink the upload cap must patch
``web.fileio.FILE_SHELF_MAX_BYTES``, not ``web.app.FILE_SHELF_MAX_BYTES``.
``FILE_SHELF_DIR`` deliberately stays in web.app (used broadly there);
``_write_file_shelf_upload`` receives a pre-resolved ``dest`` so it never
reads it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

from fastapi import UploadFile
from fastapi.requests import Request
from fastapi.responses import JSONResponse, RedirectResponse

FILE_SHELF_MAX_BYTES = int(os.environ.get("AUTOPILOT_FILE_SHELF_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))
FILE_SHELF_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _redirect_with_error(path: str, error: str) -> RedirectResponse:
    """303-redirect to ``path`` with ``error`` safely percent-encoded.

    Use whenever rendering an exception message or user-supplied text into
    a redirect URL - raw f-string interpolation truncates at the first space
    or '#' and lets '&' smuggle extra params.
    """
    return RedirectResponse(f"{path}?error={quote_plus(str(error))}", status_code=303)


def _redirect_with_query(path: str, **query: object) -> RedirectResponse:
    pairs = [
        f"{quote_plus(str(key))}={quote_plus(str(value))}"
        for key, value in query.items()
        if value not in (None, "")
    ]
    suffix = f"?{'&'.join(pairs)}" if pairs else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


def _safe_path(base_dir, filename):
    """Resolve a filename and verify it stays inside base_dir. Raises ValueError on traversal."""
    base = base_dir.resolve()
    resolved = (base_dir / filename).resolve()
    # is_relative_to is a true containment check; a string prefix match would let
    # a sibling like `<base>-evil` through (e.g. base /data/hashes, /data/hashes-x).
    if resolved != base and not resolved.is_relative_to(base):
        raise ValueError(f"Path traversal blocked: {filename}")
    return resolved


def _primary_ui_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=302)


def _redirect_current_query(target: str, request: Request | None = None) -> RedirectResponse:
    if request and request.url.query and "?" not in target:
        target = f"{target}?{request.url.query}"
    return _primary_ui_redirect(target)


def _request_wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or content_type.startswith("application/json")


def _safe_file_shelf_name(filename: str | None) -> str:
    raw_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    safe_name = re.sub(r"[^\w\-.]", "_", raw_name)
    safe_name = safe_name.lstrip(".")
    if not safe_name:
        return ""
    return safe_name


class _FileShelfUploadTooLarge(ValueError):
    def __init__(self, filename: str, max_bytes: int):
        super().__init__(f"File {filename} exceeds {max_bytes} bytes")
        self.filename = filename
        self.max_bytes = max_bytes


async def _write_file_shelf_upload(upload: UploadFile, dest: Path, display_name: str) -> int:
    tmp_path = dest.with_name(f".{dest.name}.{uuid4().hex}.upload")
    total = 0
    try:
        with tmp_path.open("wb") as fh:
            while True:
                chunk = await upload.read(FILE_SHELF_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > FILE_SHELF_MAX_BYTES:
                    raise _FileShelfUploadTooLarge(display_name, FILE_SHELF_MAX_BYTES)
                fh.write(chunk)
        if total == 0:
            tmp_path.unlink(missing_ok=True)
            return 0
        tmp_path.replace(dest)
        return total
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
