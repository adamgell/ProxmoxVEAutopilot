# Install Tracking Soft Delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add soft-delete with audit fields (`deleted_at`, `deleted_by`, `delete_reason`) to `install_tracking_runs` and `install_tracking_items`, plus `DELETE` endpoints that require a reason. The existing read paths filter out soft-deleted rows unless `include_deleted=true`.

**Architecture:** Idempotent ALTER migration in `web/install_tracking_pg.py` (matches the existing `_migrate_run_scope` pattern). Storage functions gain `delete_run` and `delete_item`. Existing list/get functions accept `include_deleted` keyword. `app.py` gets two new DELETE endpoints. No React port in this surface; the UI lands with the Answer ISOs React port in Surface 4.

**Tech Stack:** psycopg connections, FastAPI, Pydantic.

**Spec:** [docs/specs/2026-05-28-crud-gap-fill-design.md](../specs/2026-05-28-crud-gap-fill-design.md) Surface 2.

---

## File Structure

| Action | Path | Responsibility |
| --- | --- | --- |
| Modify | `autopilot-proxmox/web/install_tracking_pg.py` | Add `deleted_at`/`deleted_by`/`delete_reason` columns + partial indexes + `delete_run`/`delete_item` + `include_deleted` parameter on reads |
| Modify | `autopilot-proxmox/web/app.py` | Add `DELETE /api/install-tracking/runs/{run_id}` and `DELETE /api/install-tracking/runs/{run_id}/items/{item_id}` endpoints; thread `include_deleted` through `GET /api/install-tracking/runs` |
| Create | `autopilot-proxmox/tests/test_install_tracking_delete.py` | pytest coverage for storage + endpoints |
| Modify | `autopilot-proxmox/frontend/src/generated/` | OpenAPI client regen |

---

## Task 1: Schema migration + storage functions

**Files:**
- Modify: `autopilot-proxmox/web/install_tracking_pg.py`

- [ ] **Step 1: Add columns and indexes via idempotent ALTER**

Open `web/install_tracking_pg.py`. Find `_migrate_run_scope(conn)` around line 348. Add a new helper function `_migrate_soft_delete(conn)` and call it from `init` after `_migrate_run_scope`.

Insert after `_migrate_run_scope` ends (around line 410, before `def ensure_default_run`):

```python
def _migrate_soft_delete(conn: Connection) -> None:
    for table in ("install_tracking_runs", "install_tracking_items"):
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL,
            ADD COLUMN IF NOT EXISTS deleted_by text NULL,
            ADD COLUMN IF NOT EXISTS delete_reason text NULL
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_install_tracking_runs_live
        ON install_tracking_runs(updated_at DESC)
        WHERE deleted_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_install_tracking_items_live
        ON install_tracking_items(run_id, status, sort_order)
        WHERE deleted_at IS NULL
        """
    )
```

Then in `init` (around line 327), insert the call:

```python
def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        _migrate_run_scope(conn)
        _migrate_soft_delete(conn)  # NEW LINE
        conn.execute(POST_MIGRATE_SCHEMA)
        ensure_default_run(conn, commit=False)
        seed_defaults(conn, DEFAULT_RUN_ID, commit=False)
        _refresh_run_summary(conn, DEFAULT_RUN_ID, commit=False, touch=False)
        conn.commit()
    finally:
        if own:
            conn.close()
```

- [ ] **Step 2: Add `delete_run` and `delete_item` storage functions**

Insert into `install_tracking_pg.py` after `update_item` (around line 738):

```python
def delete_run(
    conn: Connection,
    run_id: str,
    *,
    reason: str,
    deleted_by: str | None = None,
    commit: bool = True,
) -> dict | None:
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError("delete reason is required")
    reason_clean = reason_clean[:500]
    now = _now()
    by = (deleted_by or "").strip() or None
    row = conn.execute(
        """
        UPDATE install_tracking_runs
        SET deleted_at = %s,
            deleted_by = %s,
            delete_reason = %s,
            updated_at = %s
        WHERE run_id = %s AND deleted_at IS NULL
        RETURNING *
        """,
        (now, by, reason_clean, now, run_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        """
        UPDATE install_tracking_items
        SET deleted_at = %s,
            deleted_by = %s,
            delete_reason = %s,
            updated_at = %s
        WHERE run_id = %s AND deleted_at IS NULL
        """,
        (now, by, reason_clean, now, run_id),
    )
    if commit:
        conn.commit()
    return _run_dict(dict(row))


def delete_item(
    conn: Connection,
    run_id: str,
    item_id: str,
    *,
    reason: str,
    deleted_by: str | None = None,
    commit: bool = True,
) -> dict | None:
    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError("delete reason is required")
    reason_clean = reason_clean[:500]
    now = _now()
    by = (deleted_by or "").strip() or None
    row = conn.execute(
        """
        UPDATE install_tracking_items
        SET deleted_at = %s,
            deleted_by = %s,
            delete_reason = %s,
            updated_at = %s
        WHERE run_id = %s AND item_id = %s AND deleted_at IS NULL
        RETURNING *
        """,
        (now, by, reason_clean, now, run_id, item_id),
    ).fetchone()
    if commit:
        conn.commit()
    return _row_dict(dict(row)) if row else None
```

- [ ] **Step 3: Thread `include_deleted` through reads**

Modify these functions to accept an optional `include_deleted: bool = False` parameter and add the filter to the WHERE clause:

- `list_runs(conn, *, include_deleted=False)` (around line 524): add `WHERE deleted_at IS NULL` when `not include_deleted`.
- `get_run(conn, run_id, *, include_deleted=False)` (around line 536): same.
- `list_items(conn, *, include_deleted=False)` (around line 593): same.
- `list_run_items(conn, run_id, *, include_deleted=False)` (around line 597): same.
- `get_item(conn, run_id, item_id, *, include_deleted=False)` (around line 610): same.

Read each function before modifying and add the filter only to the WHERE clause; do not change the SELECT or RETURNING shape.

Also include `deleted_at`, `deleted_by`, and `delete_reason` in `_row_dict` and `_run_dict` output by passing them through `_iso` for the timestamp.

Update `_row_dict` (around line 297):

```python
def _row_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["created_at"] = _iso(out.get("created_at"))
    out["updated_at"] = _iso(out.get("updated_at"))
    out["deleted_at"] = _iso(out.get("deleted_at"))
    out["evidence"] = out.pop("evidence_json") or {}
    return out
```

Update `_run_dict` similarly (around line 307): add `out["deleted_at"] = _iso(out.get("deleted_at"))`.

- [ ] **Step 4: Commit storage layer**

```bash
git add autopilot-proxmox/web/install_tracking_pg.py
git commit -m "$(cat <<'EOF'
Add soft delete to install_tracking storage

Idempotent ALTER migration adds deleted_at, deleted_by, delete_reason
to both install_tracking_runs and install_tracking_items, plus partial
indexes on (deleted_at IS NULL) so the live-row default stays cheap.

New delete_run/delete_item functions cascade run delete to items and
require a non-empty reason. Existing list and get helpers accept an
include_deleted flag and default to live-only.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: API endpoints

**Files:**
- Modify: `autopilot-proxmox/web/app.py`
- Test: `autopilot-proxmox/tests/test_install_tracking_delete.py`

- [ ] **Step 1: Write the failing endpoint tests**

Create `autopilot-proxmox/tests/test_install_tracking_delete.py`:

```python
"""Tests for /api/install-tracking soft-delete endpoints."""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    not os.environ.get("AUTOPILOT_TEST_DATABASE_URL"),
    reason="AUTOPILOT_TEST_DATABASE_URL not set",
)


@pytest.fixture
def conn():
    url = os.environ["AUTOPILOT_TEST_DATABASE_URL"]
    with psycopg.connect(url, row_factory=psycopg.rows.dict_row) as c:
        from web import install_tracking_pg

        install_tracking_pg.reset_for_tests(c)
        install_tracking_pg.init(c)
        yield c


def test_delete_run_requires_reason(conn):
    from web import app as app_module, install_tracking_pg

    install_tracking_pg.create_run(conn, run_id="r-1", name="r-1")
    client = TestClient(app_module.app)
    response = client.delete(
        "/api/install-tracking/runs/r-1",
        headers={"Accept": "application/json"},
        json={"reason": ""},
    )
    assert response.status_code == 422


def test_delete_run_marks_deleted_and_cascades(conn):
    from web import app as app_module, install_tracking_pg

    install_tracking_pg.create_run(conn, run_id="r-2", name="r-2")
    install_tracking_pg.upsert_item(
        conn,
        run_id="r-2",
        item_id="i-1",
        category="autopilot",
        label="Hash present",
        status="pending",
    )
    client = TestClient(app_module.app)
    response = client.delete(
        "/api/install-tracking/runs/r-2",
        json={"reason": "duplicate run"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "r-2"
    assert body["deleted_at"]
    assert body["delete_reason"] == "duplicate run"

    item = install_tracking_pg.get_item(conn, "r-2", "i-1", include_deleted=True)
    assert item is not None
    assert item["deleted_at"]


def test_list_runs_hides_deleted_by_default(conn):
    from web import app as app_module, install_tracking_pg

    install_tracking_pg.create_run(conn, run_id="r-3", name="r-3")
    install_tracking_pg.delete_run(conn, "r-3", reason="dedupe")
    client = TestClient(app_module.app)
    response = client.get("/api/install-tracking/runs")
    assert response.status_code == 200
    body = response.json()
    ids = [r["run_id"] for r in body.get("runs", [])]
    assert "r-3" not in ids


def test_list_runs_include_deleted_returns_soft_deleted_rows(conn):
    from web import app as app_module, install_tracking_pg

    install_tracking_pg.create_run(conn, run_id="r-4", name="r-4")
    install_tracking_pg.delete_run(conn, "r-4", reason="dedupe")
    client = TestClient(app_module.app)
    response = client.get("/api/install-tracking/runs?include_deleted=true")
    assert response.status_code == 200
    body = response.json()
    ids = [r["run_id"] for r in body.get("runs", [])]
    assert "r-4" in ids
    target = next(r for r in body["runs"] if r["run_id"] == "r-4")
    assert target["deleted_at"]
    assert target["delete_reason"] == "dedupe"


def test_delete_item_marks_deleted(conn):
    from web import app as app_module, install_tracking_pg

    install_tracking_pg.create_run(conn, run_id="r-5", name="r-5")
    install_tracking_pg.upsert_item(
        conn,
        run_id="r-5",
        item_id="i-x",
        category="autopilot",
        label="Hash present",
        status="pending",
    )
    client = TestClient(app_module.app)
    response = client.delete(
        "/api/install-tracking/runs/r-5/items/i-x",
        json={"reason": "obsolete check"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["item_id"] == "i-x"
    assert body["deleted_at"]


def test_delete_item_missing_returns_404(conn):
    from web import app as app_module, install_tracking_pg

    install_tracking_pg.create_run(conn, run_id="r-6", name="r-6")
    client = TestClient(app_module.app)
    response = client.delete(
        "/api/install-tracking/runs/r-6/items/ghost",
        json={"reason": "x"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python3 -m pytest tests/test_install_tracking_delete.py -v
```

Expected: All FAIL with 404 or 405 because the endpoints don't exist. If `AUTOPILOT_TEST_DATABASE_URL` is unset, tests skip entirely and the suite shows the file as deselected; mention this in the commit message.

- [ ] **Step 3: Add endpoints to `web/app.py`**

Find the existing `/api/install-tracking/runs` block (search for `@app.post("/api/install-tracking/runs")`). Immediately after that block, append:

```python
class _InstallTrackingDeleteBody(BaseModel):
    reason: str


def _session_user_email(request: Request) -> str | None:
    session = getattr(request.state, "session", None) or {}
    email = session.get("user_email") if isinstance(session, dict) else None
    return email if isinstance(email, str) and email.strip() else None


@app.delete("/api/install-tracking/runs/{run_id}")
async def delete_install_tracking_run(
    request: Request,
    run_id: str,
    body: _InstallTrackingDeleteBody,
):
    reason = (body.reason or "").strip()
    if not reason:
        return JSONResponse({"detail": "reason is required"}, status_code=422)
    with db_pg.connect() as conn:
        try:
            row = install_tracking_pg.delete_run(
                conn,
                run_id,
                reason=reason,
                deleted_by=_session_user_email(request),
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
    if row is None:
        return JSONResponse({"detail": "run not found"}, status_code=404)
    return row


@app.delete("/api/install-tracking/runs/{run_id}/items/{item_id}")
async def delete_install_tracking_item(
    request: Request,
    run_id: str,
    item_id: str,
    body: _InstallTrackingDeleteBody,
):
    reason = (body.reason or "").strip()
    if not reason:
        return JSONResponse({"detail": "reason is required"}, status_code=422)
    with db_pg.connect() as conn:
        try:
            row = install_tracking_pg.delete_item(
                conn,
                run_id,
                item_id,
                reason=reason,
                deleted_by=_session_user_email(request),
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
    if row is None:
        return JSONResponse({"detail": "item not found"}, status_code=404)
    return row
```

If `BaseModel`, `install_tracking_pg`, `db_pg`, and `JSONResponse` are not already imported at the top of `app.py`, confirm they are (they should be — search for existing imports).

- [ ] **Step 4: Update `GET /api/install-tracking/runs` to honor `include_deleted`**

Find the existing `@app.get("/api/install-tracking/runs")` handler. Read the current signature, then add an `include_deleted: bool = False` query parameter and forward it to `install_tracking_pg.list_runs`. Show the existing signature and the change inline before modifying.

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python3 -m pytest tests/test_install_tracking_delete.py -v
```

Expected: All PASS (if a test database is configured). If not configured, tests skip — note this and verify the storage-layer tests in `tests/test_install_tracking_pg.py` (if it exists) still pass for the unmodified functions.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_install_tracking_delete.py
git commit -m "$(cat <<'EOF'
Add /api/install-tracking soft-delete endpoints

DELETE /api/install-tracking/runs/{run_id} and
DELETE /api/install-tracking/runs/{run_id}/items/{item_id} both require
a non-empty reason (422 otherwise) and record the session user email
in deleted_by. GET /api/install-tracking/runs now accepts
?include_deleted=true to expose soft-deleted rows for audit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Regenerate OpenAPI client

- [ ] **Step 1: Regenerate**

```bash
cd autopilot-proxmox/frontend && PYTHON=python3 npm run generate:openapi
```

- [ ] **Step 2: Typecheck**

```bash
cd autopilot-proxmox/frontend && npx tsc --noEmit
```

Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/frontend/src/generated
git commit -m "$(cat <<'EOF'
Regenerate OpenAPI client for install-tracking soft delete

Adds DELETE /api/install-tracking/runs/{run_id} and DELETE
/api/install-tracking/runs/{run_id}/items/{item_id}, plus the
include_deleted parameter on GET /api/install-tracking/runs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Checklist

- All ALTER statements use `IF NOT EXISTS` so the migration is idempotent.
- Partial indexes match the live-row query path.
- `delete_run` cascades the soft-delete to items in the same run.
- `delete_item` does not cascade upward (a deleted item does not delete its run).
- Reads default `include_deleted=False`; the endpoint and tests both confirm this.
- Reason is required at the API boundary (422) and at the storage boundary (ValueError).

## Success Criteria

- `install_tracking_runs` and `install_tracking_items` carry the three audit columns and matching partial indexes.
- `DELETE /api/install-tracking/runs/{run_id}` and `DELETE /api/install-tracking/runs/{run_id}/items/{item_id}` exist, require a reason, return the deleted row on success and `404` on missing.
- `GET /api/install-tracking/runs?include_deleted=true` exposes soft-deleted rows with `deleted_at` / `deleted_by` / `delete_reason` populated.
- All new pytest cases pass (or skip cleanly when no test DB is configured).
- OpenAPI client regenerated; typecheck clean.
