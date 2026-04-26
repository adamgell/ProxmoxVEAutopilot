# Plan 2 — Orchestrator API for WinPE-driven OSD

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three HTTP endpoints to the existing FastAPI app — `GET /winpe/manifest/<smbios-uuid>`, `GET /winpe/content/<sha256>`, `POST /winpe/checkin` — backed by Plan 1's content-addressed artifact store, so PE-side bootstraps can identify by SMBIOS UUID, fetch a per-VM task manifest, stream content blobs by sha, and report progress.

**Architecture:** Three new modules under `autopilot-proxmox/web/`: `winpe_targets_db.py` (per-VM record: which install.wim + render params), `winpe_checkin_db.py` (PE→orchestrator progress events), `winpe_manifest_renderer.py` (assembles the JSON manifest, calls into the existing artifact store, caches per-VM rendered blobs). The three routes live in `winpe_routes.py` as a `fastapi.APIRouter`, included into `web/app.py` with `app.include_router(...)`. No auth (LAN-trusted per spec); content is sha-verified end-to-end so tampering surfaces.

**Tech Stack:** FastAPI (existing), SQLite (existing pattern), pytest + `fastapi.testclient.TestClient` (existing pattern), Plan 1's `web.artifact_store.ArtifactStore` and `web.artifact_sidecar` (consumed as-is).

**Spec reference:** `docs/specs/2026-04-25-winpe-osd-pipeline-design.md` — Section 6 (manifest schema + checkin payload), Section 7 (orchestrator API), Section 8 (artifact storage + `winpe_targets` schema).

**Note on Flask vs FastAPI:** Spec Section 3 ("Architecture diagram") and Section 7 say "Flask app." The actual codebase is **FastAPI**. Plan 2 uses FastAPI patterns (APIRouter, TestClient, dependency injection) — no behavior change vs the spec, just the right web framework. Spec doc can be updated in a follow-up.

**Out of scope for Plan 2** (deferred to Plan 3): the manifest renderer in this plan returns a **stub manifest** with hardcoded step types (partition → apply-wim → reboot). Plan 3 replaces the renderer with one that compiles task sequences from `sequence_compiler.py` and renders per-VM `unattend.xml` via `unattend_renderer.py`. This plan validates the wire format and the round-trip — fancy step orchestration comes later.

---

## File structure

| File | Purpose |
|---|---|
| `autopilot-proxmox/web/winpe_targets_db.py` | sqlite-backed per-VM target records (vm_uuid → install_wim_sha + params). New module. |
| `autopilot-proxmox/web/winpe_checkin_db.py` | sqlite-backed PE→orchestrator checkin events (vm_uuid + step_id + status + log_tail). New module. |
| `autopilot-proxmox/web/winpe_manifest_renderer.py` | Assembles manifest JSON for a vm_uuid; calls `ArtifactStore.lookup` and `ArtifactStore.cache_blob`. New module. |
| `autopilot-proxmox/web/winpe_routes.py` | `APIRouter` with the 3 routes. New module. |
| `autopilot-proxmox/web/artifact_store.py` | **Modify**: add `cache_blob(content: bytes, *, kind, extension) -> ArtifactRecord` for orchestrator-rendered per-VM blobs. |
| `autopilot-proxmox/web/app.py` | **Modify**: `from web.winpe_routes import router as winpe_router` + `app.include_router(winpe_router)` near the existing `app = FastAPI(...)`. |
| `autopilot-proxmox/tests/test_winpe_targets_db.py` | Unit tests for the targets DB. |
| `autopilot-proxmox/tests/test_winpe_checkin_db.py` | Unit tests for the checkin DB. |
| `autopilot-proxmox/tests/test_winpe_manifest_renderer.py` | Unit tests for the renderer. |
| `autopilot-proxmox/tests/test_winpe_routes.py` | Integration tests for the 3 routes via `TestClient`. |

---

## Task 1: APIRouter skeleton + app wiring

**Files:**
- Create: `autopilot-proxmox/web/winpe_routes.py`
- Modify: `autopilot-proxmox/web/app.py`
- Create: `autopilot-proxmox/tests/test_winpe_routes.py`

- [ ] **Step 1: Write the failing test**

Create `autopilot-proxmox/tests/test_winpe_routes.py`:

```python
"""Integration tests for /winpe/* routes."""
from fastapi.testclient import TestClient


def test_winpe_router_is_mounted():
    """Smoke test: importing app.py mounts the winpe router and serves a 404 (not 500) on a stub path."""
    from web.app import app
    client = TestClient(app)
    # The router exists; an unknown sha returns 404 (route exists but content not found),
    # not 500 (route would 500 if not mounted at all).
    resp = client.get("/winpe/content/0000000000000000000000000000000000000000000000000000000000000000")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-orchestrator-api/autopilot-proxmox
pytest tests/test_winpe_routes.py::test_winpe_router_is_mounted -v
```

Expected: ImportError or 404→500/path-not-mapped (route doesn't exist yet).

- [ ] **Step 3: Create the router skeleton**

Create `autopilot-proxmox/web/winpe_routes.py`:

```python
"""FastAPI router for the WinPE-driven OSD orchestrator API.

Three endpoints (spec Section 7):

    GET  /winpe/manifest/<smbios-uuid>   → per-VM manifest JSON
    GET  /winpe/content/<sha256>         → content-addressed blob
    POST /winpe/checkin                  → PE→orchestrator progress event

Auth: none (LAN-trusted). Content is sha-verified by PE-side after fetch.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from web.artifact_store import ArtifactStore


router = APIRouter(prefix="/winpe", tags=["winpe"])


def _artifact_root() -> Path:
    """Default artifact-store root: <repo-root>/var/artifacts.

    autopilot-proxmox is run from itself as the working directory in tests
    and Docker; var/artifacts lives one level up at the repo root.
    """
    return Path.cwd().parent / "var" / "artifacts"


@router.get("/content/{sha256}")
def get_content(sha256: str) -> FileResponse:
    """Stream a registered artifact (or cached per-VM blob) by sha256.

    Looks up the sha in the artifact-store index; if absent, 404. If present
    but the underlying file is gone, 410 Gone (corrupt store; operator must
    re-register the artifact).
    """
    store = ArtifactStore(_artifact_root())
    record = store.lookup(sha256)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown sha256: {sha256}")
    abs_path = store.root / record.relative_path
    if not abs_path.exists():
        raise HTTPException(status_code=410, detail=f"sha256 indexed but file missing: {record.relative_path}")
    return FileResponse(
        path=str(abs_path),
        media_type="application/octet-stream",
        filename=abs_path.name,
    )
```

- [ ] **Step 4: Wire the router into app.py**

Edit `autopilot-proxmox/web/app.py`. Find the line that creates the FastAPI app (`app = FastAPI(...)`) and add an `include_router` call immediately after it.

Locate the existing `app = FastAPI(...)` line and append after it:

```python
# WinPE-driven OSD orchestrator API (spec: docs/specs/2026-04-25-winpe-osd-pipeline-design.md §7)
from web.winpe_routes import router as winpe_router  # noqa: E402
app.include_router(winpe_router)
```

Use a `noqa: E402` because the import has to land after the `app = FastAPI(...)` constructor for the router decorators to bind correctly to *this* app instance.

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_winpe_routes.py::test_winpe_router_is_mounted -v
```

Expected: 1 passed. The route is mounted; an unknown sha returns 404 (the `lookup()` returned None and `raise HTTPException(404)` fired).

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/winpe_routes.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_winpe_routes.py
git commit -m "feat(winpe): add /winpe/* APIRouter skeleton + content lookup"
```

---

## Task 2: winpe_targets DB

**Files:**
- Create: `autopilot-proxmox/web/winpe_targets_db.py`
- Create: `autopilot-proxmox/tests/test_winpe_targets_db.py`

Per-VM record. Schema is spec Section 8. The DB lives in the artifact store's sqlite (`var/artifacts/index.db`) so we don't fragment storage.

- [ ] **Step 1: Write the failing tests**

Create `autopilot-proxmox/tests/test_winpe_targets_db.py`:

```python
import pytest

from web.winpe_targets_db import (
    WinpeTarget,
    WinpeTargetsDb,
    UnknownVmError,
)


def _db(tmp_path):
    return WinpeTargetsDb(tmp_path / "index.db")


def test_init_creates_table(tmp_path):
    db = _db(tmp_path)
    assert db.list_uuids() == []


def test_register_and_lookup(tmp_path):
    db = _db(tmp_path)
    db.register(
        vm_uuid="11111111-2222-3333-4444-555555555555",
        install_wim_sha="a" * 64,
        template_id="win11-arm64-baseline",
        params={"computer_name": "AUTOPILOT-X1", "oem_profile": "Lenovo-ThinkPad"},
    )
    target = db.lookup("11111111-2222-3333-4444-555555555555")
    assert target is not None
    assert target.install_wim_sha == "a" * 64
    assert target.template_id == "win11-arm64-baseline"
    assert target.params == {"computer_name": "AUTOPILOT-X1", "oem_profile": "Lenovo-ThinkPad"}


def test_lookup_unknown_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.lookup("00000000-0000-0000-0000-000000000000") is None


def test_register_is_upsert_on_uuid(tmp_path):
    db = _db(tmp_path)
    db.register(vm_uuid="u1", install_wim_sha="a" * 64, template_id="t1", params={"k": 1})
    db.register(vm_uuid="u1", install_wim_sha="b" * 64, template_id="t2", params={"k": 2})
    target = db.lookup("u1")
    assert target.install_wim_sha == "b" * 64
    assert target.template_id == "t2"
    assert target.params == {"k": 2}
    assert len(db.list_uuids()) == 1


def test_touch_last_manifest_at(tmp_path):
    db = _db(tmp_path)
    db.register(vm_uuid="u1", install_wim_sha="a" * 64, template_id="t1", params={})
    before = db.lookup("u1").last_manifest_at
    assert before is None
    db.touch_last_manifest_at("u1")
    after = db.lookup("u1").last_manifest_at
    assert after is not None and after.endswith("Z")


def test_touch_unknown_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(UnknownVmError):
        db.touch_last_manifest_at("u-does-not-exist")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_winpe_targets_db.py -v
```

Expected: ImportError ("cannot import name 'WinpeTarget' from 'web.winpe_targets_db'").

- [ ] **Step 3: Implement `winpe_targets_db.py`**

Create `autopilot-proxmox/web/winpe_targets_db.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_winpe_targets_db.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_targets_db.py autopilot-proxmox/tests/test_winpe_targets_db.py
git commit -m "feat(winpe): per-VM target records (winpe_targets table)"
```

---

## Task 3: winpe_checkin DB

**Files:**
- Create: `autopilot-proxmox/web/winpe_checkin_db.py`
- Create: `autopilot-proxmox/tests/test_winpe_checkin_db.py`

PE-side bootstrap POSTs one event per step. Persisted for the Jobs page (Plan 4) to render per-step progress.

- [ ] **Step 1: Write the failing tests**

Create `autopilot-proxmox/tests/test_winpe_checkin_db.py`:

```python
from web.winpe_checkin_db import (
    Checkin,
    WinpeCheckinDb,
)


def _db(tmp_path):
    return WinpeCheckinDb(tmp_path / "checkins.db")


def test_init_creates_table(tmp_path):
    db = _db(tmp_path)
    assert db.list_for_vm("u1") == []


def test_record_and_list(tmp_path):
    db = _db(tmp_path)
    db.record(Checkin(
        vm_uuid="u1",
        step_id="partition",
        status="ok",
        timestamp="2026-04-25T22:00:00Z",
        duration_sec=4.2,
        log_tail="formatted disk 0; created GPT layout\n",
        error_message=None,
        extra={"esp": "S:", "windows": "W:"},
    ))
    db.record(Checkin(
        vm_uuid="u1",
        step_id="apply-wim",
        status="ok",
        timestamp="2026-04-25T22:01:30Z",
        duration_sec=84.1,
        log_tail="applied install.wim to W:\\\n",
        error_message=None,
        extra={},
    ))
    rows = db.list_for_vm("u1")
    assert len(rows) == 2
    assert rows[0].step_id == "partition"
    assert rows[1].step_id == "apply-wim"
    assert rows[1].duration_sec == 84.1


def test_list_filters_by_vm_uuid(tmp_path):
    db = _db(tmp_path)
    db.record(Checkin(vm_uuid="u1", step_id="p", status="ok",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=1.0,
                      log_tail="", error_message=None, extra={}))
    db.record(Checkin(vm_uuid="u2", step_id="p", status="ok",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=1.0,
                      log_tail="", error_message=None, extra={}))
    assert len(db.list_for_vm("u1")) == 1
    assert len(db.list_for_vm("u2")) == 1
    assert len(db.list_for_vm("u3")) == 0


def test_record_idempotent_on_uuid_step_timestamp(tmp_path):
    """Same (vm_uuid, step_id, timestamp) tuple — duplicate POSTs from a retrying PE
    must not insert duplicate rows. We use INSERT OR REPLACE keyed on the triple."""
    db = _db(tmp_path)
    c = Checkin(vm_uuid="u1", step_id="p", status="starting",
                timestamp="2026-04-25T22:00:00Z", duration_sec=0.0,
                log_tail="", error_message=None, extra={})
    db.record(c)
    db.record(c)  # duplicate — same triple
    assert len(db.list_for_vm("u1")) == 1


def test_status_update_on_same_triple(tmp_path):
    """If PE first records 'starting' then later records 'ok' with the same timestamp
    (this is unusual; normally the timestamp would advance), we accept the latter as
    an idempotent rewrite. In practice timestamps will differ and we'll have two rows."""
    db = _db(tmp_path)
    db.record(Checkin(vm_uuid="u1", step_id="p", status="starting",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=0.0,
                      log_tail="", error_message=None, extra={}))
    db.record(Checkin(vm_uuid="u1", step_id="p", status="ok",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=2.5,
                      log_tail="done", error_message=None, extra={}))
    rows = db.list_for_vm("u1")
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].duration_sec == 2.5


def test_error_message_persists(tmp_path):
    db = _db(tmp_path)
    db.record(Checkin(vm_uuid="u1", step_id="apply", status="error",
                      timestamp="2026-04-25T22:00:00Z", duration_sec=1.0,
                      log_tail="...sha256 mismatch on fetched content",
                      error_message="sha256 mismatch: expected aaa, got bbb",
                      extra={}))
    row = db.list_for_vm("u1")[0]
    assert row.status == "error"
    assert row.error_message == "sha256 mismatch: expected aaa, got bbb"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_winpe_checkin_db.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `winpe_checkin_db.py`**

Create `autopilot-proxmox/web/winpe_checkin_db.py`:

```python
"""PE-side step-by-step progress events (spec §6 checkin payload).

PE bootstrap POSTs one Checkin per manifest step. Idempotent on
(vm_uuid, step_id, timestamp) so retrying POSTs from a flaky PE network
don't duplicate. The Jobs page (Plan 4) will render rows from here as
per-step progress for a deployment job.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Checkin:
    vm_uuid: str
    step_id: str
    status: str           # 'starting' | 'ok' | 'error'
    timestamp: str        # ISO 8601 'Z'
    duration_sec: float
    log_tail: str
    error_message: str | None
    extra: dict = field(default_factory=dict)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS winpe_checkins (
    vm_uuid       TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    status        TEXT NOT NULL,
    duration_sec  REAL NOT NULL,
    log_tail      TEXT NOT NULL,
    error_message TEXT,
    extra_json    TEXT NOT NULL,
    PRIMARY KEY (vm_uuid, step_id, timestamp)
);

CREATE INDEX IF NOT EXISTS winpe_checkins_by_vm
    ON winpe_checkins(vm_uuid, timestamp);
"""


class WinpeCheckinDb:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    def record(self, checkin: Checkin) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO winpe_checkins "
                "(vm_uuid, step_id, timestamp, status, duration_sec, "
                " log_tail, error_message, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkin.vm_uuid,
                    checkin.step_id,
                    checkin.timestamp,
                    checkin.status,
                    checkin.duration_sec,
                    checkin.log_tail,
                    checkin.error_message,
                    json.dumps(checkin.extra),
                ),
            )

    def list_for_vm(self, vm_uuid: str) -> list[Checkin]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT vm_uuid, step_id, status, timestamp, duration_sec, "
                "       log_tail, error_message, extra_json "
                "FROM winpe_checkins WHERE vm_uuid = ? ORDER BY timestamp",
                (vm_uuid,),
            ).fetchall()
        return [
            Checkin(
                vm_uuid=r[0],
                step_id=r[1],
                status=r[2],
                timestamp=r[3],
                duration_sec=r[4],
                log_tail=r[5],
                error_message=r[6],
                extra=json.loads(r[7]),
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_winpe_checkin_db.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_checkin_db.py autopilot-proxmox/tests/test_winpe_checkin_db.py
git commit -m "feat(winpe): PE checkin event persistence"
```

---

## Task 4: ArtifactStore.cache_blob extension

**Files:**
- Modify: `autopilot-proxmox/web/artifact_store.py`
- Modify: `autopilot-proxmox/tests/test_artifact_store.py`

The manifest renderer needs to cache per-VM rendered blobs (the per-VM `unattend.xml` and the per-VM stage-files zip) into `var/artifacts/cache/<sha>.<ext>`. PE then fetches them by sha via `/winpe/content`. We extend `ArtifactStore` with a `cache_blob` method that:

- Takes raw bytes + `kind` + `extension`.
- Computes sha256.
- Writes to `cache/<sha>.<ext>` (idempotent).
- Indexes into the same `artifacts` table with `relative_path = "cache/<sha>.<ext>"`.

This way, the existing `/winpe/content/<sha>` route serves cache blobs and registered build artifacts identically.

- [ ] **Step 1: Append the failing test**

Open `autopilot-proxmox/tests/test_artifact_store.py` and append:

```python
def test_cache_blob_hashes_writes_indexes(tmp_path):
    store = ArtifactStore(tmp_path)
    content = b"<?xml version='1.0'?><unattend>per-VM xml</unattend>"
    record = store.cache_blob(
        content,
        kind=ArtifactKind.UNATTEND_XML,
        extension="xml",
    )
    assert record.kind is ArtifactKind.UNATTEND_XML
    assert record.relative_path == f"cache/{record.sha256}.xml"
    assert (tmp_path / record.relative_path).read_bytes() == content
    # Round-trips through lookup like any registered artifact:
    looked_up = store.lookup(record.sha256)
    assert looked_up is not None
    assert looked_up.relative_path == record.relative_path


def test_cache_blob_idempotent_on_identical_content(tmp_path):
    store = ArtifactStore(tmp_path)
    content = b"identical-bytes"
    r1 = store.cache_blob(content, kind=ArtifactKind.STAGE_ZIP, extension="zip")
    r2 = store.cache_blob(content, kind=ArtifactKind.STAGE_ZIP, extension="zip")
    assert r1.sha256 == r2.sha256
    assert len(store.list_artifacts()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_artifact_store.py::test_cache_blob_hashes_writes_indexes -v
```

Expected: AttributeError ('ArtifactStore' object has no attribute 'cache_blob').

- [ ] **Step 3: Add `cache_blob` to ArtifactStore**

Open `autopilot-proxmox/web/artifact_store.py`. Add this method on the `ArtifactStore` class, right after the existing `register` method:

```python
    def cache_blob(self, content: bytes, *, kind: ArtifactKind, extension: str) -> ArtifactRecord:
        """Stash an orchestrator-rendered per-VM blob in cache/, indexed alongside
        registered build artifacts so /winpe/content/<sha> serves both uniformly.

        Idempotent on content sha — a second call with identical bytes returns
        the existing record without rewriting the file.
        """
        import hashlib
        sha = hashlib.sha256(content).hexdigest()

        existing = self.lookup(sha)
        if existing is not None:
            # File on disk may also exist — preserve it; otherwise re-write.
            expected_path = self.root / existing.relative_path
            if not expected_path.exists():
                expected_path.parent.mkdir(parents=True, exist_ok=True)
                expected_path.write_bytes(content)
            return existing

        rel = f"cache/{sha}.{extension.lstrip('.')}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

        registered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO artifacts (sha256, kind, size, relative_path, metadata_json, registered_at, last_served_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (sha, kind.value, len(content), rel, json.dumps({"source": "cache_blob"}), registered_at),
            )
        return ArtifactRecord(
            sha256=sha,
            kind=kind,
            size=len(content),
            relative_path=rel,
            metadata={"source": "cache_blob"},
            registered_at=registered_at,
            last_served_at=None,
        )
```

- [ ] **Step 4: Run all artifact_store tests**

```bash
pytest tests/test_artifact_store.py -v
```

Expected: all existing tests still pass + 2 new ones pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/artifact_store.py autopilot-proxmox/tests/test_artifact_store.py
git commit -m "feat(artifacts): ArtifactStore.cache_blob for per-VM rendered blobs"
```

---

## Task 5: winpe_manifest_renderer (stub for v1)

**Files:**
- Create: `autopilot-proxmox/web/winpe_manifest_renderer.py`
- Create: `autopilot-proxmox/tests/test_winpe_manifest_renderer.py`

For Plan 2, the renderer returns a **minimal manifest** that drives the basic flow: partition → apply-wim → write-unattend → bcdboot → reboot. The unattend is rendered as a hardcoded stub XML that substitutes only the computer name. Plan 3 replaces this with real `unattend_renderer` + `sequence_compiler` integration.

The renderer is a pure function: given a `WinpeTarget` and an `ArtifactStore`, return `(manifest_dict, list_of_cache_records_created)`.

- [ ] **Step 1: Write the failing tests**

Create `autopilot-proxmox/tests/test_winpe_manifest_renderer.py`:

```python
import hashlib

import pytest

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore
from web.winpe_manifest_renderer import render_manifest, RendererError
from web.winpe_targets_db import WinpeTarget


def _seed_install_wim(store: ArtifactStore, tmp_path) -> str:
    """Register a fake install.wim into the store, return its sha."""
    src = tmp_path / "install.wim"
    content = b"fake install.wim content"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store.register(
        src,
        Sidecar(kind=ArtifactKind.INSTALL_WIM, sha256=sha, size=len(content), metadata={}),
        extension="wim",
    )
    return sha


def _make_target(install_wim_sha: str, **overrides) -> WinpeTarget:
    return WinpeTarget(
        vm_uuid=overrides.get("vm_uuid", "11111111-2222-3333-4444-555555555555"),
        install_wim_sha=install_wim_sha,
        template_id=overrides.get("template_id", "win11-arm64-baseline"),
        params=overrides.get("params", {"computer_name": "AUTOPILOT-X1"}),
        created_at="2026-04-25T00:00:00Z",
        last_manifest_at=None,
    )


def test_renders_minimal_manifest(tmp_path):
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha)

    manifest = render_manifest(target, store)

    assert manifest["version"] == 1
    assert manifest["vmUuid"] == target.vm_uuid
    assert manifest["onError"] == "halt"
    step_types = [s["type"] for s in manifest["steps"]]
    assert step_types == [
        "partition", "apply-wim", "write-unattend",
        "set-registry", "bcdboot", "reboot",
    ]


def test_apply_wim_step_references_target_install_wim_sha(tmp_path):
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha)

    manifest = render_manifest(target, store)
    apply = next(s for s in manifest["steps"] if s["type"] == "apply-wim")
    assert apply["content"]["sha256"] == install_sha
    assert apply["content"]["size"] > 0


def test_unattend_step_caches_rendered_xml(tmp_path):
    """write-unattend points at a sha that, when fetched from the store, is the rendered XML."""
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha, params={"computer_name": "MY-COMPUTER-42"})

    manifest = render_manifest(target, store)
    unattend = next(s for s in manifest["steps"] if s["type"] == "write-unattend")
    sha = unattend["content"]["sha256"]
    record = store.lookup(sha)
    assert record is not None
    assert record.kind is ArtifactKind.UNATTEND_XML
    rendered = (store.root / record.relative_path).read_bytes().decode("utf-8")
    assert "MY-COMPUTER-42" in rendered


def test_unattend_caching_is_deterministic(tmp_path):
    """Identical params → identical rendered XML → identical sha (one cache row, not two)."""
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha)
    m1 = render_manifest(target, store)
    m2 = render_manifest(target, store)
    sha1 = next(s["content"]["sha256"] for s in m1["steps"] if s["type"] == "write-unattend")
    sha2 = next(s["content"]["sha256"] for s in m2["steps"] if s["type"] == "write-unattend")
    assert sha1 == sha2
    # Only 2 rows: install.wim + unattend.xml.
    assert len(store.list_artifacts()) == 2


def test_unknown_install_wim_raises(tmp_path):
    """Target references an install.wim sha that isn't in the store — renderer fails fast."""
    store = ArtifactStore(tmp_path)
    target = _make_target("0" * 64)  # bogus sha
    with pytest.raises(RendererError, match="install.wim"):
        render_manifest(target, store)


def test_set_registry_step_carries_computer_name(tmp_path):
    """The minimal manifest writes ComputerName via offline registry too (belt-and-braces with the unattend)."""
    store = ArtifactStore(tmp_path)
    install_sha = _seed_install_wim(store, tmp_path)
    target = _make_target(install_sha, params={"computer_name": "FOO-BAR"})

    manifest = render_manifest(target, store)
    reg = next(s for s in manifest["steps"] if s["type"] == "set-registry")
    cn_entry = next(k for k in reg["keys"] if k["name"] == "ComputerName")
    assert cn_entry["value"] == "FOO-BAR"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_winpe_manifest_renderer.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `winpe_manifest_renderer.py`**

Create `autopilot-proxmox/web/winpe_manifest_renderer.py`:

```python
"""Manifest assembly for /winpe/manifest/<vm-uuid> (spec §6).

Plan 2 ships a stub renderer: the manifest has fixed step types in a fixed
order, and only the computer name is per-VM-substituted. Plan 3 replaces
this with sequence_compiler + unattend_renderer integration that produces
real per-VM artifacts.

Pure function: given a WinpeTarget and an ArtifactStore, return the
manifest dict. Rendered per-VM blobs (unattend.xml) get cached into the
store via cache_blob; the manifest references them by sha. PE fetches
each blob via /winpe/content/<sha>.
"""

from __future__ import annotations

from web.artifact_sidecar import ArtifactKind
from web.artifact_store import ArtifactStore
from web.winpe_targets_db import WinpeTarget


class RendererError(RuntimeError):
    pass


_UNATTEND_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <ComputerName>__COMPUTER_NAME__</ComputerName>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral"
               versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
    </component>
  </settings>
</unattend>
"""


def _render_unattend(params: dict) -> bytes:
    computer_name = params.get("computer_name", "AUTOPILOT-VM")
    return _UNATTEND_TEMPLATE.replace("__COMPUTER_NAME__", computer_name).encode("utf-8")


def render_manifest(target: WinpeTarget, store: ArtifactStore) -> dict:
    """Assemble a minimal manifest for the target. Caches rendered per-VM blobs."""
    install_record = store.lookup(target.install_wim_sha)
    if install_record is None:
        raise RendererError(
            f"target's install.wim sha {target.install_wim_sha} is not registered "
            f"in the artifact store"
        )

    unattend_xml = _render_unattend(target.params)
    unattend_record = store.cache_blob(
        unattend_xml,
        kind=ArtifactKind.UNATTEND_XML,
        extension="xml",
    )

    computer_name = target.params.get("computer_name", "AUTOPILOT-VM")

    return {
        "version": 1,
        "vmUuid": target.vm_uuid,
        "onError": "halt",
        "steps": [
            {"id": "p1", "type": "partition", "layout": "uefi-standard"},
            {
                "id": "a1",
                "type": "apply-wim",
                "content": {"sha256": install_record.sha256, "size": install_record.size},
            },
            {
                "id": "u1",
                "type": "write-unattend",
                "content": {"sha256": unattend_record.sha256, "size": unattend_record.size},
                "target": "W:\\Windows\\Panther\\unattend.xml",
            },
            {
                "id": "r1",
                "type": "set-registry",
                "hive": "SYSTEM",
                "target": "W:",
                "keys": [
                    {
                        "path": "Setup",
                        "name": "ComputerName",
                        "type": "REG_SZ",
                        "value": computer_name,
                    },
                ],
            },
            {"id": "b1", "type": "bcdboot", "windows": "W:", "esp": "S:"},
            {"id": "rb", "type": "reboot"},
        ],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_winpe_manifest_renderer.py -v
```

Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_manifest_renderer.py autopilot-proxmox/tests/test_winpe_manifest_renderer.py
git commit -m "feat(winpe): stub manifest renderer (partition→apply-wim→reboot)"
```

---

## Task 6: GET /winpe/manifest/<smbios-uuid> route

**Files:**
- Modify: `autopilot-proxmox/web/winpe_routes.py`
- Modify: `autopilot-proxmox/tests/test_winpe_routes.py`

Wire the renderer into the FastAPI router. 404 if VM unknown; 503 if install.wim referenced by the target isn't registered (RendererError).

- [ ] **Step 1: Append failing tests**

Append to `autopilot-proxmox/tests/test_winpe_routes.py`:

```python
import hashlib

import pytest
from fastapi.testclient import TestClient

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore


@pytest.fixture
def isolated_artifact_root(tmp_path, monkeypatch):
    """Redirect web.winpe_routes._artifact_root to a per-test tmp_path."""
    from web import winpe_routes
    monkeypatch.setattr(winpe_routes, "_artifact_root", lambda: tmp_path)
    return tmp_path


def _seed_install_wim(root, content: bytes = b"fake install wim") -> str:
    src = root / "src.wim"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store = ArtifactStore(root)
    store.register(src, Sidecar(kind=ArtifactKind.INSTALL_WIM, sha256=sha, size=len(content), metadata={}), extension="wim")
    src.unlink()
    return sha


def test_manifest_returns_404_for_unknown_uuid(isolated_artifact_root):
    from web.app import app
    client = TestClient(app)
    resp = client.get("/winpe/manifest/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert "unknown" in resp.json()["detail"].lower()


def test_manifest_renders_for_registered_target(isolated_artifact_root):
    from web.app import app
    from web.winpe_targets_db import WinpeTargetsDb

    install_sha = _seed_install_wim(isolated_artifact_root)
    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(
        vm_uuid="aaaa-bbbb",
        install_wim_sha=install_sha,
        template_id="win11-arm64-baseline",
        params={"computer_name": "TEST-MANIFEST-01"},
    )

    client = TestClient(app)
    resp = client.get("/winpe/manifest/aaaa-bbbb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["vmUuid"] == "aaaa-bbbb"
    apply_step = next(s for s in body["steps"] if s["type"] == "apply-wim")
    assert apply_step["content"]["sha256"] == install_sha
    # Rendered unattend was cached and is now servable via /winpe/content
    unattend_step = next(s for s in body["steps"] if s["type"] == "write-unattend")
    unattend_sha = unattend_step["content"]["sha256"]
    follow_up = client.get(f"/winpe/content/{unattend_sha}")
    assert follow_up.status_code == 200
    assert b"TEST-MANIFEST-01" in follow_up.content


def test_manifest_503_when_target_install_wim_missing(isolated_artifact_root):
    from web.app import app
    from web.winpe_targets_db import WinpeTargetsDb

    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(
        vm_uuid="vvv",
        install_wim_sha="0" * 64,  # not registered
        template_id="t",
        params={},
    )
    client = TestClient(app)
    resp = client.get("/winpe/manifest/vvv")
    assert resp.status_code == 503
    assert "install.wim" in resp.json()["detail"].lower()


def test_manifest_request_touches_last_manifest_at(isolated_artifact_root):
    from web.app import app
    from web.winpe_targets_db import WinpeTargetsDb

    install_sha = _seed_install_wim(isolated_artifact_root)
    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(vm_uuid="touchme", install_wim_sha=install_sha, template_id="t", params={})

    assert db.lookup("touchme").last_manifest_at is None
    client = TestClient(app)
    client.get("/winpe/manifest/touchme")
    assert db.lookup("touchme").last_manifest_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_winpe_routes.py -v -k manifest
```

Expected: 4 fail with 404 from a non-mounted route.

- [ ] **Step 3: Implement the manifest route**

Modify `autopilot-proxmox/web/winpe_routes.py`. Add the import for the renderer + targets DB at the top of the file:

```python
from web.winpe_manifest_renderer import render_manifest, RendererError
from web.winpe_targets_db import UnknownVmError, WinpeTargetsDb
```

Then add this route in the file, before the existing `get_content` route:

```python
@router.get("/manifest/{vm_uuid}")
def get_manifest(vm_uuid: str) -> JSONResponse:
    """Render the per-VM task manifest. 404 if unknown; 503 if install.wim missing."""
    root = _artifact_root()
    db = WinpeTargetsDb(root / "index.db")
    target = db.lookup(vm_uuid)
    if target is None:
        raise HTTPException(status_code=404, detail=f"unknown vm_uuid: {vm_uuid}")

    store = ArtifactStore(root)
    try:
        manifest = render_manifest(target, store)
    except RendererError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        db.touch_last_manifest_at(vm_uuid)
    except UnknownVmError:
        # Race: target was deleted between lookup and touch. Manifest is still valid.
        pass

    return JSONResponse(content=manifest)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_winpe_routes.py -v -k manifest
```

Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_routes.py autopilot-proxmox/tests/test_winpe_routes.py
git commit -m "feat(winpe): GET /winpe/manifest/<uuid> route"
```

---

## Task 7: GET /winpe/content/<sha256> — extra cases

**Files:**
- Modify: `autopilot-proxmox/tests/test_winpe_routes.py`

The route already works (Task 1 baseline). Add tests for the streaming-binary case and the 410-Gone case.

- [ ] **Step 1: Append failing tests**

Append to `autopilot-proxmox/tests/test_winpe_routes.py`:

```python
def test_content_streams_install_wim_bytes(isolated_artifact_root):
    from web.app import app
    install_sha = _seed_install_wim(isolated_artifact_root, content=b"\x01\x02\x03 binary blob")
    client = TestClient(app)
    resp = client.get(f"/winpe/content/{install_sha}")
    assert resp.status_code == 200
    assert resp.content == b"\x01\x02\x03 binary blob"
    assert resp.headers["content-type"] == "application/octet-stream"


def test_content_410_when_indexed_but_file_missing(isolated_artifact_root):
    from web.app import app
    install_sha = _seed_install_wim(isolated_artifact_root)
    # Delete the underlying file but leave the index row.
    (isolated_artifact_root / "store" / f"{install_sha}.wim").unlink()
    client = TestClient(app)
    resp = client.get(f"/winpe/content/{install_sha}")
    assert resp.status_code == 410
    assert "missing" in resp.json()["detail"].lower()


def test_content_404_for_unknown_sha(isolated_artifact_root):
    from web.app import app
    client = TestClient(app)
    resp = client.get("/winpe/content/" + "f" * 64)
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest tests/test_winpe_routes.py -v -k content
```

Expected: 3 pass (route already implemented in Task 1; this just covers more cases).

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/tests/test_winpe_routes.py
git commit -m "test(winpe): cover content streaming + 410-gone"
```

---

## Task 8: POST /winpe/checkin route

**Files:**
- Modify: `autopilot-proxmox/web/winpe_routes.py`
- Modify: `autopilot-proxmox/tests/test_winpe_routes.py`

PE bootstrap POSTs JSON checkin events. We validate the shape with a Pydantic model, persist via `WinpeCheckinDb`, return 204 No Content.

- [ ] **Step 1: Append failing tests**

Append to `autopilot-proxmox/tests/test_winpe_routes.py`:

```python
def test_checkin_persists_and_returns_204(isolated_artifact_root):
    from web.app import app
    from web.winpe_checkin_db import WinpeCheckinDb

    client = TestClient(app)
    payload = {
        "vmUuid": "aaaa-bbbb",
        "stepId": "apply",
        "status": "ok",
        "timestamp": "2026-04-25T22:00:00Z",
        "durationSec": 84.5,
        "logTail": "applied install.wim → W:\\",
        "errorMessage": None,
        "extra": {"esp": "S:", "windows": "W:"},
    }
    resp = client.post("/winpe/checkin", json=payload)
    assert resp.status_code == 204
    assert resp.content == b""

    db = WinpeCheckinDb(isolated_artifact_root / "checkins.db")
    rows = db.list_for_vm("aaaa-bbbb")
    assert len(rows) == 1
    assert rows[0].step_id == "apply"
    assert rows[0].duration_sec == 84.5
    assert rows[0].extra == {"esp": "S:", "windows": "W:"}


def test_checkin_validates_required_fields(isolated_artifact_root):
    from web.app import app
    client = TestClient(app)
    # Missing vmUuid
    resp = client.post("/winpe/checkin", json={"stepId": "x", "status": "ok"})
    assert resp.status_code == 422  # FastAPI's Pydantic-validation default


def test_checkin_idempotent_on_retry(isolated_artifact_root):
    """PE retries POST after a transient network error — duplicate writes don't accumulate."""
    from web.app import app
    from web.winpe_checkin_db import WinpeCheckinDb

    client = TestClient(app)
    payload = {
        "vmUuid": "u-retry", "stepId": "p", "status": "ok",
        "timestamp": "2026-04-25T22:00:00Z", "durationSec": 1.0,
        "logTail": "", "errorMessage": None, "extra": {},
    }
    client.post("/winpe/checkin", json=payload)
    client.post("/winpe/checkin", json=payload)
    db = WinpeCheckinDb(isolated_artifact_root / "checkins.db")
    assert len(db.list_for_vm("u-retry")) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_winpe_routes.py -v -k checkin
```

Expected: all fail (route not implemented).

- [ ] **Step 3: Implement the checkin route**

Modify `autopilot-proxmox/web/winpe_routes.py`. Add at the top of the file:

```python
from pydantic import BaseModel, Field

from web.winpe_checkin_db import Checkin, WinpeCheckinDb


class _CheckinIn(BaseModel):
    vmUuid: str
    stepId: str
    status: str = Field(pattern=r"^(starting|ok|error)$")
    timestamp: str
    durationSec: float = 0.0
    logTail: str = ""
    errorMessage: str | None = None
    extra: dict = Field(default_factory=dict)
```

Then add this route at the bottom of the file:

```python
@router.post("/checkin", status_code=204)
def post_checkin(payload: _CheckinIn) -> None:
    root = _artifact_root()
    root.mkdir(parents=True, exist_ok=True)
    db = WinpeCheckinDb(root / "checkins.db")
    db.record(Checkin(
        vm_uuid=payload.vmUuid,
        step_id=payload.stepId,
        status=payload.status,
        timestamp=payload.timestamp,
        duration_sec=payload.durationSec,
        log_tail=payload.logTail,
        error_message=payload.errorMessage,
        extra=payload.extra,
    ))
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_winpe_routes.py -v -k checkin
```

Expected: 3 pass.

- [ ] **Step 5: Run all winpe_routes tests**

```bash
pytest tests/test_winpe_routes.py -v
```

Expected: 11 tests pass (1 skeleton + 4 manifest + 3 content + 3 checkin).

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/winpe_routes.py autopilot-proxmox/tests/test_winpe_routes.py
git commit -m "feat(winpe): POST /winpe/checkin route"
```

---

## Task 9: End-to-end integration test

**Files:**
- Create: `autopilot-proxmox/tests/test_winpe_e2e.py`

One test exercising the full PE-side round-trip: register a target → fetch manifest → fetch each content blob from the manifest → POST a checkin per step → verify checkins persisted in the right order.

This proves the wire format end-to-end without requiring an actual booted PE.

- [ ] **Step 1: Write the test**

Create `autopilot-proxmox/tests/test_winpe_e2e.py`:

```python
"""End-to-end integration: simulate a PE-side bootstrap walking the manifest."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from web.artifact_sidecar import ArtifactKind, Sidecar
from web.artifact_store import ArtifactStore
from web.winpe_checkin_db import WinpeCheckinDb
from web.winpe_targets_db import WinpeTargetsDb


@pytest.fixture
def isolated_artifact_root(tmp_path, monkeypatch):
    from web import winpe_routes
    monkeypatch.setattr(winpe_routes, "_artifact_root", lambda: tmp_path)
    return tmp_path


def test_pe_bootstrap_round_trip(isolated_artifact_root):
    """Stand in for the real PE Bootstrap.ps1: pull manifest, fetch each content blob, post checkins."""
    from web.app import app

    # Setup: register an install.wim and a winpe_target referencing it.
    install_content = b"INSTALL_WIM_PAYLOAD"
    install_sha = hashlib.sha256(install_content).hexdigest()
    src = isolated_artifact_root / "install.wim"
    src.write_bytes(install_content)
    store = ArtifactStore(isolated_artifact_root)
    store.register(
        src,
        Sidecar(kind=ArtifactKind.INSTALL_WIM, sha256=install_sha,
                size=len(install_content), metadata={"edition": "Win11 Enterprise ARM64"}),
        extension="wim",
    )
    src.unlink()

    db = WinpeTargetsDb(isolated_artifact_root / "index.db")
    db.register(
        vm_uuid="e2e-uuid",
        install_wim_sha=install_sha,
        template_id="win11-arm64-baseline",
        params={"computer_name": "E2E-VM-99"},
    )

    client = TestClient(app)

    # Step 1: PE fetches manifest.
    manifest = client.get("/winpe/manifest/e2e-uuid").json()
    assert manifest["vmUuid"] == "e2e-uuid"

    # Step 2: PE walks each step's content reference (where present).
    fetched = {}
    for step in manifest["steps"]:
        if "content" in step:
            sha = step["content"]["sha256"]
            blob = client.get(f"/winpe/content/{sha}")
            assert blob.status_code == 200, f"step {step['id']} content fetch failed"
            fetched[step["id"]] = blob.content

    # Verify the install.wim and unattend.xml round-trip cleanly.
    assert fetched["a1"] == install_content
    assert b"E2E-VM-99" in fetched["u1"]

    # Step 3: PE POSTs a checkin for each step.
    timestamp_base = "2026-04-25T22:00:"
    for i, step in enumerate(manifest["steps"]):
        ts = f"{timestamp_base}{i:02d}Z"
        client.post("/winpe/checkin", json={
            "vmUuid": "e2e-uuid",
            "stepId": step["id"],
            "status": "ok",
            "timestamp": ts,
            "durationSec": 1.0 + i,
            "logTail": f"step {step['id']} done",
            "errorMessage": None,
            "extra": {},
        })

    # Step 4: Verify all checkins persisted in order.
    checkin_db = WinpeCheckinDb(isolated_artifact_root / "checkins.db")
    rows = checkin_db.list_for_vm("e2e-uuid")
    assert len(rows) == len(manifest["steps"])
    assert [r.step_id for r in rows] == [s["id"] for s in manifest["steps"]]
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/test_winpe_e2e.py -v
```

Expected: 1 test passes (round-trip end-to-end).

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/tests/test_winpe_e2e.py
git commit -m "test(winpe): end-to-end PE bootstrap round-trip"
```

---

## Task 10: Final verification

**Files:** none new — verification only.

- [ ] **Step 1: Run the full new-Python suite**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-orchestrator-api/autopilot-proxmox
pytest tests/test_winpe_targets_db.py tests/test_winpe_checkin_db.py tests/test_winpe_manifest_renderer.py tests/test_winpe_routes.py tests/test_winpe_e2e.py tests/test_artifact_store.py tests/test_artifact_sidecar.py tests/test_artifact_register.py -v
```

Expected: all pass. Sum: 6 + 6 + 6 + 11 + 1 + 9 + 8 + 3 = 50 tests.

- [ ] **Step 2: Confirm no regression in pre-existing tests**

```bash
pytest --collect-only -q 2>&1 | tail -3
```

Expected: collection completes (existing 14 errors from missing FastAPI deps in unbootstrapped worktree are *pre-existing* and unrelated; do not new ones).

- [ ] **Step 3: Verify file inventory**

```bash
ls autopilot-proxmox/web/winpe_targets_db.py \
   autopilot-proxmox/web/winpe_checkin_db.py \
   autopilot-proxmox/web/winpe_manifest_renderer.py \
   autopilot-proxmox/web/winpe_routes.py \
   autopilot-proxmox/tests/test_winpe_targets_db.py \
   autopilot-proxmox/tests/test_winpe_checkin_db.py \
   autopilot-proxmox/tests/test_winpe_manifest_renderer.py \
   autopilot-proxmox/tests/test_winpe_routes.py \
   autopilot-proxmox/tests/test_winpe_e2e.py
```

Expected: all 9 files present.

- [ ] **Step 4: Confirm router is registered**

```bash
grep -n 'winpe_router' autopilot-proxmox/web/app.py
```

Expected: at least one match showing the `app.include_router(winpe_router)` line.

- [ ] **Step 5: Sanity check the routes are reachable in a live app instance**

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/winpe-orchestrator-api/autopilot-proxmox
python3 -c "from web.app import app; routes = [r.path for r in app.routes if r.path.startswith('/winpe')]; print(routes)"
```

Expected output: `['/winpe/manifest/{vm_uuid}', '/winpe/content/{sha256}', '/winpe/checkin']`

**Plan 2 is complete when**: all 50 tests pass, app loads without import errors, and the three `/winpe/*` routes appear in the registered route list.

---

## Self-review checklist

- [x] **Spec coverage**: every requirement in `2026-04-25-winpe-osd-pipeline-design.md` Section 6 (manifest schema + checkin payload), Section 7 (3 endpoints, 404/503 semantics, content addressing), and Section 8 (winpe_targets table) maps to a task. Per-VM `unattend.xml` rendering is explicitly stubbed for v1 (Plan 3 ships full integration); the wire format and round-trip work today.
- [x] **No placeholders**: every step has concrete code or a concrete shell command. No "TBD", "TODO", "implement later".
- [x] **Type/name consistency**: `WinpeTarget`, `WinpeTargetsDb`, `Checkin`, `WinpeCheckinDb`, `RendererError`, `render_manifest`, `_artifact_root`, `_CheckinIn`, `cache_blob` — all referenced consistently across tasks.
- [x] **Dependencies**: every task only consumes types/functions defined in earlier tasks (or in Plan 1's already-shipped modules: `ArtifactStore`, `ArtifactKind`, `Sidecar`).
- [x] **Test code is real**: every TDD task shows full test bodies, not "write tests for the above."
- [x] **Commit messages**: each task ends with a Conventional Commits style message.
