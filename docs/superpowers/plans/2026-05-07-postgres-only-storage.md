# Postgres-Only Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all SQLite-backed runtime state with mandatory PostgreSQL state and clean-reset existing SQLite operational history.

**Architecture:** Add a shared Postgres app database module, then port each repository surface from SQLite to focused Postgres modules. Keep each port self-contained and test-compatible before deleting SQLite modules and constants.

**Tech Stack:** Python 3.10, FastAPI, psycopg 3, PostgreSQL 16, pytest, Docker-backed Postgres fixtures, PowerShell/Pester for WinPE and OSD client coverage.

---

## File Map

- Create `autopilot-proxmox/web/db_pg.py`: required DSN resolution, connection helper, schema bootstrap orchestration, test reset helper.
- Create `autopilot-proxmox/web/jobs_pg.py`: Postgres job queue repository.
- Create `autopilot-proxmox/web/service_health_pg.py`: Postgres service heartbeat repository.
- Create `autopilot-proxmox/web/device_history_pg.py`: Postgres monitoring repository.
- Create `autopilot-proxmox/web/devices_pg.py`: Postgres Graph device cache repository.
- Modify `autopilot-proxmox/web/ts_engine_pg.py`: use shared Postgres bootstrap helpers and expose V2 helpers needed by WinPE package staging.
- Modify `autopilot-proxmox/web/app.py`: require Postgres DSN, initialize Postgres repositories, replace SQLite module imports/call sites.
- Modify `autopilot-proxmox/web/builder.py`: use `jobs_pg` and Postgres service health.
- Modify `autopilot-proxmox/web/monitor_main.py`: use Postgres monitoring/job/service-health repositories.
- Modify `autopilot-proxmox/web/winpe_endpoints.py`: replace legacy SQLite run state with V2 Postgres run state or remove legacy-only endpoints after V2 equivalents exist.
- Modify `tools/winpe-build/Invoke-AutopilotWinPE.ps1`: stage V2 OSD config from server-authored V2 package response.
- Modify tests under `autopilot-proxmox/tests/`: replace SQLite fixtures with Postgres fixtures and add static no-SQLite runtime guard.
- Delete after callers are gone: `web/jobs_db.py`, `web/device_history_db.py`, `web/devices_db.py`, `web/service_health.py`, `web/sequences_db.py`.

---

## Task 1: Shared Postgres App Database Foundation

**Files:**
- Create: `autopilot-proxmox/web/db_pg.py`
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/web/ts_engine_pg.py`
- Test: `autopilot-proxmox/tests/test_db_pg.py`
- Test: `autopilot-proxmox/tests/test_ts_engine_startup.py`

- [ ] **Step 1: Write failing DSN requirement tests**

Create `autopilot-proxmox/tests/test_db_pg.py`:

```python
from __future__ import annotations

import pytest


def test_database_url_requires_env(monkeypatch):
    from web import db_pg

    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres database URL is required"):
        db_pg.database_url()


def test_database_url_prefers_autopilot_database_url(monkeypatch):
    from web import db_pg

    monkeypatch.setenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", "postgresql://old")
    monkeypatch.setenv("AUTOPILOT_DATABASE_URL", "postgresql://new")

    assert db_pg.database_url() == "postgresql://new"
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_db_pg.py -q
```

Expected: failure because `web.db_pg` does not exist.

- [ ] **Step 3: Create the minimal shared Postgres module**

Create `autopilot-proxmox/web/db_pg.py`:

```python
"""Shared PostgreSQL application database helpers."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row


def database_url() -> str:
    dsn = (
        os.environ.get("AUTOPILOT_DATABASE_URL")
        or os.environ.get("AUTOPILOT_TS_ENGINE_DATABASE_URL")
        or ""
    ).strip()
    if not dsn:
        raise RuntimeError(
            "Postgres database URL is required; set AUTOPILOT_DATABASE_URL "
            "or AUTOPILOT_TS_ENGINE_DATABASE_URL"
        )
    return dsn


def connect(dsn: str | None = None) -> Connection:
    return psycopg.connect(dsn or database_url(), row_factory=dict_row)


@contextmanager
def connection(dsn: str | None = None) -> Iterator[Connection]:
    with connect(dsn) as conn:
        yield conn
```

- [ ] **Step 4: Run DSN tests and verify they pass**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_db_pg.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Move `ts_engine_pg.connect` to shared helper**

Modify the bottom of `autopilot-proxmox/web/ts_engine_pg.py` so callers use the shared connection implementation:

```python
def connect(dsn: str):
    """Return a dict-row psycopg connection for engine callers."""
    from web import db_pg

    return db_pg.connect(dsn)
```

- [ ] **Step 6: Make app startup require Postgres**

In `autopilot-proxmox/web/app.py`, replace the optional startup behavior with a required initializer:

```python
def _database_url() -> str:
    from web import db_pg

    return db_pg.database_url()


def _init_app_database() -> None:
    from web import db_pg, ts_engine_pg

    with db_pg.connection(_database_url()) as conn:
        ts_engine_pg.init(conn)
```

Then update the startup handler that currently calls `_init_ts_engine_database_if_configured()` so it calls:

```python
_init_app_database()
```

- [ ] **Step 7: Update startup tests**

Modify `autopilot-proxmox/tests/test_ts_engine_startup.py` to assert missing DSN raises and configured DSN initializes:

```python
def test_app_database_startup_requires_database_url(monkeypatch):
    from web import app as web_app

    monkeypatch.delenv("AUTOPILOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_TS_ENGINE_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres database URL is required"):
        web_app._database_url()
```

- [ ] **Step 8: Run foundation tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_db_pg.py tests/test_ts_engine_startup.py tests/test_ts_engine_pg.py -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit foundation**

Run:

```bash
git add autopilot-proxmox/web/db_pg.py autopilot-proxmox/web/app.py autopilot-proxmox/web/ts_engine_pg.py autopilot-proxmox/tests/test_db_pg.py autopilot-proxmox/tests/test_ts_engine_startup.py
git commit -m "feat(db): require shared postgres app database"
```

---

## Task 2: Port Job Queue To Postgres

**Files:**
- Create: `autopilot-proxmox/web/jobs_pg.py`
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/web/builder.py`
- Test: `autopilot-proxmox/tests/test_jobs_pg.py`
- Test: `autopilot-proxmox/tests/test_builder.py`

- [ ] **Step 1: Write failing Postgres job queue test**

Create `autopilot-proxmox/tests/test_jobs_pg.py` with an ephemeral Postgres fixture copied from `tests/test_ts_engine_pg.py`, then add:

```python
def test_claim_respects_per_type_cap(pg_conn):
    from web import jobs_pg

    jobs_pg.init(pg_conn)
    jobs_pg.enqueue(
        job_id="j1",
        job_type="build_template",
        playbook="build.yml",
        cmd=["ansible-playbook", "build.yml"],
        args={"vmid": 101},
    )
    jobs_pg.enqueue(
        job_id="j2",
        job_type="build_template",
        playbook="build.yml",
        cmd=["ansible-playbook", "build.yml"],
        args={"vmid": 102},
    )

    first = jobs_pg.claim_next_job(worker_id="builder-1")
    second = jobs_pg.claim_next_job(worker_id="builder-2")

    assert first["id"] == "j1"
    assert second is None
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_jobs_pg.py::test_claim_respects_per_type_cap -q
```

Expected: failure because `web.jobs_pg` does not exist.

- [ ] **Step 3: Create `jobs_pg.py` schema and init**

Create `autopilot-proxmox/web/jobs_pg.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    job_type text NOT NULL,
    playbook text NOT NULL,
    cmd_json jsonb NOT NULL,
    args_json jsonb NOT NULL,
    status text NOT NULL,
    worker_id text NULL,
    kill_requested boolean NOT NULL DEFAULT false,
    exit_code integer NULL,
    created_at timestamptz NOT NULL,
    claimed_at timestamptz NULL,
    last_heartbeat timestamptz NULL,
    ended_at timestamptz NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_type_limits (
    job_type text PRIMARY KEY,
    max_concurrent integer NOT NULL
);
"""

DEFAULT_LIMITS = [
    ("build_template", 1),
    ("provision_clone", 3),
    ("hash_capture", 5),
    ("upload_after_capture", 5),
    ("retry_inject_hash", 3),
]
DEFAULT_CAP = 1


def _now():
    return datetime.now(timezone.utc)


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        for job_type, cap in DEFAULT_LIMITS:
            conn.execute(
                """
                INSERT INTO job_type_limits (job_type, max_concurrent)
                VALUES (%s, %s)
                ON CONFLICT (job_type) DO NOTHING
                """,
                (job_type, cap),
            )
        conn.commit()
    finally:
        if own:
            conn.close()
```

- [ ] **Step 4: Add `enqueue`, `get_job`, and row normalization**

Append to `jobs_pg.py`:

```python
def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["cmd"] = out.pop("cmd_json")
    out["args"] = out.pop("args_json")
    out["started"] = out.get("created_at").isoformat() if out.get("created_at") else None
    out["ended"] = out.get("ended_at").isoformat() if out.get("ended_at") else None
    for key in ("created_at", "claimed_at", "last_heartbeat", "ended_at"):
        if out.get(key):
            out[key] = out[key].isoformat()
    return out


def enqueue(*, job_id: str, job_type: str, playbook: str, cmd: list, args: dict) -> dict:
    now = _now()
    with db_pg.connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs
                (id, job_type, playbook, cmd_json, args_json, status, created_at)
            VALUES (%s, %s, %s, %s, %s, 'pending', %s)
            """,
            (job_id, job_type, playbook, Jsonb(cmd), Jsonb(args), now),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        conn.commit()
        return _row_to_dict(row)


def get_job(job_id: str) -> Optional[dict]:
    with db_pg.connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        return _row_to_dict(row)
```

- [ ] **Step 5: Add atomic Postgres claim implementation**

Append:

```python
def claim_next_job(*, worker_id: str) -> Optional[dict]:
    now = _now()
    with db_pg.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                f"""
                SELECT j.id
                FROM jobs j
                LEFT JOIN job_type_limits l ON l.job_type = j.job_type
                WHERE j.status = 'pending'
                  AND (
                      SELECT COUNT(*)
                      FROM jobs running
                      WHERE running.job_type = j.job_type
                        AND running.status = 'running'
                  ) < COALESCE(l.max_concurrent, {DEFAULT_CAP})
                ORDER BY j.created_at ASC, j.id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    worker_id = %s,
                    claimed_at = %s,
                    last_heartbeat = %s
                WHERE id = %s AND status = 'pending'
                """,
                (worker_id, now, now, row["id"]),
            )
        claimed = conn.execute("SELECT * FROM jobs WHERE id = %s", (row["id"],)).fetchone()
        return _row_to_dict(claimed)
```

- [ ] **Step 6: Port remaining jobs API**

Add the remaining public API from `jobs_db.py` to `jobs_pg.py`. Use these exact behaviors:

- `list_jobs(limit=200)`: select newest jobs by `created_at DESC, id DESC`, return `_row_to_dict()` rows.
- `finalize_job(job_id, exit_code=0)`: set `status = 'complete'` for exit `0`, `status = 'failed'` otherwise, set `exit_code`, `ended_at = now`.
- `request_kill(job_id)`: set `kill_requested = true`.
- `touch_heartbeat(job_id, worker_id)`: update `last_heartbeat = now` only when `id` and `worker_id` match.
- `reap_stale_running_jobs(older_than_seconds=900)`: mark stale `running` jobs `orphaned`, set `ended_at = now`, return row count.
- `list_job_type_limits()`: return `job_type` and `max_concurrent` ordered by `job_type`.
- `update_job_type_limit(job_type, max_concurrent)`: upsert a positive integer cap and return the row.
- `complete_interrupted_provision_winpe_jobs_for_run(run_id)`: find jobs with `job_type = 'provision_clone'`, `status IN ('failed', 'orphaned')`, and `args_json->>'run_id' = run_id`; mark them `complete` and return row count.

Use `%s` placeholders, `Jsonb` for JSON columns, explicit `conn.commit()` after writes, and `_row_to_dict()` for rows returned to the UI.

- [ ] **Step 7: Run job tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_jobs_pg.py -q
```

Expected: all `jobs_pg` tests pass.

- [ ] **Step 8: Replace app and builder imports**

In `autopilot-proxmox/web/app.py` and `autopilot-proxmox/web/builder.py`, replace:

```python
from web import jobs_db
```

with:

```python
from web import jobs_pg as jobs_db
```

Then remove `JOBS_DB` path arguments from calls. Convert:

```python
jobs_db.enqueue(JOBS_DB, job_id=job_id, job_type=job_type, playbook=playbook, cmd=cmd, args=args)
```

to:

```python
jobs_db.enqueue(job_id=job_id, job_type=job_type, playbook=playbook, cmd=cmd, args=args)
```

- [ ] **Step 9: Run builder and job endpoint tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_jobs.py tests/test_builder.py tests/test_jobs_pg.py -q
```

Expected: selected tests pass after test fixtures configure Postgres DSN.

- [ ] **Step 10: Commit jobs port**

Run:

```bash
git add autopilot-proxmox/web/jobs_pg.py autopilot-proxmox/web/app.py autopilot-proxmox/web/builder.py autopilot-proxmox/tests/test_jobs_pg.py autopilot-proxmox/tests/test_jobs.py autopilot-proxmox/tests/test_builder.py
git commit -m "feat(db): move job queue to postgres"
```

---

## Task 3: Port Service Health To Postgres

**Files:**
- Create: `autopilot-proxmox/web/service_health_pg.py`
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/web/monitor_main.py`
- Modify: `autopilot-proxmox/web/builder.py`
- Test: `autopilot-proxmox/tests/test_service_health_pg.py`
- Test: `autopilot-proxmox/tests/test_service_health.py`

- [ ] **Step 1: Write failing service health test**

Create `autopilot-proxmox/tests/test_service_health_pg.py`:

```python
def test_heartbeat_upserts_and_classifies(pg_conn):
    from web import service_health_pg

    service_health_pg.init(pg_conn)
    service_health_pg.heartbeat(
        service_id="web-1",
        service_type="web",
        version_sha="abc123",
        detail="ready",
    )
    rows = service_health_pg.list_services()

    assert rows[0]["service_id"] == "web-1"
    assert rows[0]["service_type"] == "web"
    assert rows[0]["version_sha"] == "abc123"
    assert rows[0]["status"] == "ok"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_service_health_pg.py -q
```

Expected: failure because `service_health_pg` does not exist.

- [ ] **Step 3: Create Postgres service health module**

Create `autopilot-proxmox/web/service_health_pg.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg import Connection

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS service_health (
    service_id text PRIMARY KEY,
    service_type text NOT NULL,
    version_sha text NOT NULL,
    started_at timestamptz NOT NULL,
    last_heartbeat timestamptz NOT NULL,
    detail text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS service_health_by_type ON service_health(service_type);
"""

OK_THRESHOLD = 20
DEGRADED_THRESHOLD = 50


def _now():
    return datetime.now(timezone.utc)


def init(conn: Connection | None = None) -> None:
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def heartbeat(*, service_id: str, service_type: str, version_sha: str, detail: str = "") -> None:
    now = _now()
    with db_pg.connection() as conn:
        conn.execute(
            """
            INSERT INTO service_health
                (service_id, service_type, version_sha, started_at, last_heartbeat, detail)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (service_id) DO UPDATE SET
                service_type = EXCLUDED.service_type,
                version_sha = EXCLUDED.version_sha,
                last_heartbeat = EXCLUDED.last_heartbeat,
                detail = EXCLUDED.detail
            """,
            (service_id, service_type, version_sha, now, now, detail),
        )
        conn.commit()
```

- [ ] **Step 4: Add list and prune functions**

Append:

```python
def _classify(last_heartbeat: datetime, now: datetime) -> str:
    age = (now - last_heartbeat).total_seconds()
    if age <= OK_THRESHOLD:
        return "ok"
    if age <= DEGRADED_THRESHOLD:
        return "degraded"
    return "dead"


def list_services() -> list[dict]:
    now = _now()
    with db_pg.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM service_health ORDER BY service_type, service_id"
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["status"] = _classify(item["last_heartbeat"], now)
        item["age_seconds"] = int((now - item["last_heartbeat"]).total_seconds())
        for key in ("started_at", "last_heartbeat"):
            item[key] = item[key].isoformat()
        out.append(item)
    return out


def prune_dead_workers(*, max_age_seconds: int = 600) -> int:
    cutoff = _now() - timedelta(seconds=max_age_seconds)
    with db_pg.connection() as conn:
        cur = conn.execute(
            """
            DELETE FROM service_health
            WHERE service_type = 'builder'
              AND last_heartbeat < %s
            """,
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
```

- [ ] **Step 5: Replace service health imports**

Replace runtime imports:

```python
from web import service_health
```

with:

```python
from web import service_health_pg as service_health
```

Remove `db_path` arguments from call sites.

- [ ] **Step 6: Run service health tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_service_health_pg.py tests/test_service_health.py tests/test_monitoring_page.py -q
```

Expected: selected tests pass.

- [ ] **Step 7: Commit service health port**

Run:

```bash
git add autopilot-proxmox/web/service_health_pg.py autopilot-proxmox/web/app.py autopilot-proxmox/web/builder.py autopilot-proxmox/web/monitor_main.py autopilot-proxmox/tests/test_service_health_pg.py autopilot-proxmox/tests/test_service_health.py autopilot-proxmox/tests/test_monitoring_page.py
git commit -m "feat(db): move service health to postgres"
```

---

## Task 4: Port Monitoring History To Postgres

**Files:**
- Create: `autopilot-proxmox/web/device_history_pg.py`
- Modify: `autopilot-proxmox/web/device_monitor.py`
- Modify: `autopilot-proxmox/web/monitor_main.py`
- Modify: `autopilot-proxmox/web/app.py`
- Test: `autopilot-proxmox/tests/test_device_history_pg.py`
- Test: `autopilot-proxmox/tests/test_monitoring_page.py`
- Test: `autopilot-proxmox/tests/test_monitoring_api.py`

- [ ] **Step 1: Write failing monitoring repository test**

Create `autopilot-proxmox/tests/test_device_history_pg.py`:

```python
def test_latest_per_vmid_joins_snapshot_and_probe(pg_conn):
    from web import device_history_pg

    device_history_pg.init(pg_conn)
    sweep_id = device_history_pg.start_sweep()
    device_history_pg.insert_pve_snapshot(sweep_id, {
        "vmid": 108,
        "present": True,
        "node": "pve2",
        "name": "Gell-60F03E42",
        "status": "running",
        "tags": ["autopilot"],
        "config_digest": "abc",
    })
    device_history_pg.insert_device_probe(sweep_id, {
        "vmid": 108,
        "vm_name": "Gell-60F03E42",
        "win_name": "Gell-60F03E42",
        "serial": "Gell-60F03E42",
        "entra_found": True,
        "entra_match_count": 1,
        "entra_matches": [{"trustType": "AzureAD"}],
    })
    device_history_pg.finish_sweep(sweep_id, vm_count=1)

    rows = device_history_pg.latest_per_vmid()

    assert rows[0]["vmid"] == 108
    assert rows[0]["pve"]["name"] == "Gell-60F03E42"
    assert rows[0]["probe"]["entra_found"] is True
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_device_history_pg.py::test_latest_per_vmid_joins_snapshot_and_probe -q
```

Expected: failure because `device_history_pg` does not exist.

- [ ] **Step 3: Create Postgres monitoring schema**

Create `autopilot-proxmox/web/device_history_pg.py` with the SQLite table shape translated to Postgres:

```python
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
    dsreg_status text NULL,
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
"""
```

- [ ] **Step 4: Add monitoring settings and OU tables**

Add to the same `SCHEMA` string:

```python
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
```

- [ ] **Step 5: Implement public monitoring functions**

Port the public behavior from `device_history_db.py` to `device_history_pg.py`, removing `db_path` parameters:

- Initialize schema and seed the default OU only when `monitoring_search_ous` is empty.
- Return `MonitoringSettings` and `SearchOu` dataclasses with the same field names as the current module.
- Enforce `interval_seconds >= 60`.
- Validate DNs with the existing `_DN_RE` regular expression.
- Preserve the `CannotDeleteLastOu` and `InvalidDn` exception classes.
- Store snapshot/probe list fields in JSONB instead of encoded text.
- Implement `latest_pve_snapshot(vmid)` and `latest_device_probe(vmid)` using `ORDER BY checked_at DESC, id DESC LIMIT 1`.
- Implement `latest_per_vmid()` by selecting the latest PVE snapshot per VMID and joining the latest probe for the same VMID into the existing `{"vmid": 108, "pve": {}, "probe": {}}` return shape.

Use `Jsonb` for JSON fields, `bool` for boolean columns, and `.isoformat()` in returned dictionaries where current UI code expects strings.

- [ ] **Step 6: Replace monitoring imports**

Replace runtime imports:

```python
from web import device_history_db
```

with:

```python
from web import device_history_pg as device_history_db
```

Remove `MONITOR_DB` and `monitor_db_path` arguments from runtime calls.

- [ ] **Step 7: Run monitoring tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_device_history_pg.py tests/test_monitoring_page.py tests/test_monitoring_api.py tests/test_monitoring_view.py -q
```

Expected: selected tests pass.

- [ ] **Step 8: Commit monitoring port**

Run:

```bash
git add autopilot-proxmox/web/device_history_pg.py autopilot-proxmox/web/device_monitor.py autopilot-proxmox/web/monitor_main.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_device_history_pg.py autopilot-proxmox/tests/test_monitoring_page.py autopilot-proxmox/tests/test_monitoring_api.py
git commit -m "feat(db): move monitoring history to postgres"
```

---

## Task 5: Port Device Cache To Postgres

**Files:**
- Create: `autopilot-proxmox/web/devices_pg.py`
- Modify: `autopilot-proxmox/web/app.py`
- Test: `autopilot-proxmox/tests/test_devices_pg.py`
- Test: `autopilot-proxmox/tests/test_device_detail_page.py`
- Test: `autopilot-proxmox/tests/test_device_regression.py`

- [ ] **Step 1: Write failing device cache test**

Create `autopilot-proxmox/tests/test_devices_pg.py`:

```python
def test_upsert_and_group_devices_by_serial(pg_conn):
    from web import devices_pg

    devices_pg.init(pg_conn)
    devices_pg.upsert_autopilot([
        {"id": "ap-1", "serialNumber": "ABC123", "groupTag": "lab"}
    ])
    devices_pg.upsert_intune([
        {"id": "mdm-1", "serialNumber": "ABC123", "deviceName": "Gell-ABC123"}
    ])
    devices_pg.upsert_entra([
        {"id": "aad-1", "deviceId": "dev-1", "displayName": "Gell-ABC123"}
    ])

    grouped = devices_pg.grouped_by_serial()

    assert grouped["ABC123"]["autopilot"]["id"] == "ap-1"
    assert grouped["ABC123"]["intune"]["id"] == "mdm-1"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_devices_pg.py::test_upsert_and_group_devices_by_serial -q
```

Expected: failure because `devices_pg` does not exist.

- [ ] **Step 3: Create Postgres device cache schema**

Create `autopilot-proxmox/web/devices_pg.py` with tables matching current device cache semantics:

```python
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
```

- [ ] **Step 4: Port public functions**

Port the public behavior from `devices_db.py` to `devices_pg.py` without `db_path` parameters:

- `upsert_autopilot(devices)`: replace the current Autopilot cache with the supplied records and return inserted count.
- `upsert_intune(devices)`: replace the current Intune cache with the supplied records and return inserted count.
- `upsert_entra(devices)`: replace the current Entra cache with supplied records, extracting `[SerialNumber]` and `[ZTDID]` from `physicalIds`, and return inserted count.
- `grouped_by_serial()`: preserve the current grouped return shape used by `/devices`.
- `list_unmatched_entra()`: return Entra rows that cannot be correlated by serial or Intune Azure AD device ID.
- `record_deletion(source, object_id, serial, display_name, status, message)`: insert one deletion audit row and return it.
- `list_deletions(limit=100)`: return newest deletion audit rows.

- [ ] **Step 5: Replace device cache imports**

Replace:

```python
from web import devices_db
```

with:

```python
from web import devices_pg as devices_db
```

Remove path arguments from calls.

- [ ] **Step 6: Run device tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_devices_pg.py tests/test_device_detail_page.py tests/test_device_regression.py tests/test_monitoring_view.py -q
```

Expected: selected tests pass.

- [ ] **Step 7: Commit device cache port**

Run:

```bash
git add autopilot-proxmox/web/devices_pg.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_devices_pg.py autopilot-proxmox/tests/test_device_detail_page.py autopilot-proxmox/tests/test_device_regression.py
git commit -m "feat(db): move device cache to postgres"
```

---

## Task 6: V2 WinPE Package Config And UUID Run State

**Files:**
- Modify: `autopilot-proxmox/web/osd_v2_endpoints.py`
- Modify: `autopilot-proxmox/web/ts_engine_pg.py`
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Test: `autopilot-proxmox/tests/test_osd_v2_endpoints.py`
- Test: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write failing V2 package endpoint test**

Add to `autopilot-proxmox/tests/test_osd_v2_endpoints.py`:

```python
def test_v2_agent_package_returns_server_authored_config(osd_v2_client, pg_conn):
    run_id = _create_run(pg_conn, winpe_only=False)
    response = osd_v2_client.get(f"/osd/v2/agent/package/{run_id}?phase=full_os")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 2
    assert body["engine"] == "v2"
    assert body["api_version"] == 2
    assert body["run_id"] == run_id
    assert body["config"]["engine"] == "v2"
    assert body["config"]["api_version"] == 2
    assert body["config"]["run_id"] == run_id
    assert body["config"]["phase"] == "full_os"
    assert body["config"]["agent_id"].startswith("osd-fullos-")
    assert body["config"]["bearer_token"]
    assert any(file["path"].endswith("OsdClient.ps1") for file in body["files"])
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_osd_v2_endpoints.py::test_v2_agent_package_returns_server_authored_config -q
```

Expected: 404 for missing endpoint.

- [ ] **Step 3: Add V2 package endpoint**

In `autopilot-proxmox/web/osd_v2_endpoints.py`, add:

```python
@router.get("/agent/package/{run_id}")
def get_v2_agent_package(run_id: str, phase: str = "full_os"):
    with _conn() as conn:
        try:
            ts_engine_pg.get_run(conn, run_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="run not found")
    files_dir = _files_dir()
    files = _osd_client_files(files_dir)
    agent_id = f"osd-{phase.replace('_', '')}-{run_id[:8]}"
    token = _sign(run_id)
    config = {
        "engine": "v2",
        "api_version": 2,
        "flask_base_url": "",
        "run_id": run_id,
        "agent_id": agent_id,
        "phase": phase,
        "bearer_token": token,
    }
    return {
        "schema_version": 2,
        "engine": "v2",
        "api_version": 2,
        "run_id": run_id,
        "phase": phase,
        "agent_id": agent_id,
        "bearer_token": token,
        "config_path": r"V:\ProgramData\ProxmoxVEAutopilot\OSD\osd-config.json",
        "config": config,
        "files": files,
    }
```

If `_files_dir`, `_content_b64`, or OSD package file helpers currently live only in `winpe_endpoints.py`, move them to a small shared module `web/osd_package.py` and import from both endpoint modules.

- [ ] **Step 4: Update WinPE staging test**

In `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`, add a V2 response case where `run_id` is a UUID string and `config` is present:

```powershell
It 'writes server-authored v2 osd-config.json' {
    $uuid = '11111111-2222-3333-4444-555555555555'
    $package = [pscustomobject]@{
        schema_version = 2
        engine = 'v2'
        api_version = 2
        run_id = $uuid
        bearer_token = 'v2-token'
        config = [pscustomobject]@{
            engine = 'v2'
            api_version = 2
            flask_base_url = 'http://autopilot.local'
            run_id = $uuid
            phase = 'full_os'
            agent_id = 'osd-fullos-11111111'
            bearer_token = 'v2-token'
        }
        files = @(
            [pscustomobject]@{
                path = 'V:\ProgramData\ProxmoxVEAutopilot\OSD\OsdClient.ps1'
                content_b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('Write-Host ok'))
            }
        )
    }
    # Existing test harness should return $package from the REST invoker.
    # Assert osd-config.json contains engine v2 and UUID run_id.
}
```

- [ ] **Step 5: Update WinPE stage client action**

Modify `tools/winpe-build/Invoke-AutopilotWinPE.ps1`:

```powershell
if ($package.PSObject.Properties.Match('schema_version').Count -gt 0 -and
    [int] $package.schema_version -eq 2) {
    if ([string] $package.engine -ne 'v2') {
        throw "stage_osd_client: v2 package engine mismatch actual=$($package.engine)"
    }
    if ($package.PSObject.Properties.Match('config').Count -eq 0 -or $null -eq $package.config) {
        throw "stage_osd_client: v2 package missing config"
    }
    $config = $package.config
    $config.flask_base_url = $BaseUrl
    if ($FallbackBaseUrl) {
        $config | Add-Member -NotePropertyName flask_base_url_fallback -NotePropertyValue $FallbackBaseUrl -Force
    }
} else {
    $config = @{
        flask_base_url = $BaseUrl
        run_id = $RunId
        bearer_token = [string] $package.bearer_token
    }
    if ($FallbackBaseUrl) { $config.flask_base_url_fallback = $FallbackBaseUrl }
}
```

- [ ] **Step 6: Run V2 package tests**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_osd_v2_endpoints.py -q
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/tools/winpe-build
pwsh -NoProfile -Command "$c = New-PesterConfiguration; $c.Run.Path = 'tests/Invoke-AutopilotWinPE.Tests.ps1'; $c.Run.Exit = $true; Invoke-Pester -Configuration $c"
```

Expected: selected tests pass.

- [ ] **Step 7: Commit V2 package staging**

Run:

```bash
git add autopilot-proxmox/web/osd_v2_endpoints.py autopilot-proxmox/web/ts_engine_pg.py tools/winpe-build/Invoke-AutopilotWinPE.ps1 autopilot-proxmox/tests/test_osd_v2_endpoints.py tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(osd): stage v2 client config from postgres run"
```

---

## Task 7: Remove SQLite Runtime Imports And Files

**Files:**
- Delete: `autopilot-proxmox/web/jobs_db.py`
- Delete: `autopilot-proxmox/web/device_history_db.py`
- Delete: `autopilot-proxmox/web/devices_db.py`
- Delete: `autopilot-proxmox/web/service_health.py`
- Delete: `autopilot-proxmox/web/sequences_db.py`
- Modify: `autopilot-proxmox/web/app.py`
- Test: `autopilot-proxmox/tests/test_no_sqlite_runtime.py`

- [ ] **Step 1: Write failing no-SQLite runtime guard**

Create `autopilot-proxmox/tests/test_no_sqlite_runtime.py`:

```python
from __future__ import annotations

from pathlib import Path


def test_runtime_web_modules_do_not_import_sqlite3():
    web_dir = Path(__file__).resolve().parents[1] / "web"
    offenders = []
    for path in web_dir.glob("*.py"):
        if path.name.endswith("_db.py"):
            offenders.append(path.name)
            continue
        text = path.read_text(encoding="utf-8")
        if "import sqlite3" in text or "from sqlite3" in text:
            offenders.append(path.name)

    assert offenders == []
```

- [ ] **Step 2: Run the guard and verify it fails**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_no_sqlite_runtime.py -q
```

Expected: failure listing existing SQLite modules.

- [ ] **Step 3: Delete SQLite modules**

Run:

```bash
git rm autopilot-proxmox/web/jobs_db.py
git rm autopilot-proxmox/web/device_history_db.py
git rm autopilot-proxmox/web/devices_db.py
git rm autopilot-proxmox/web/service_health.py
git rm autopilot-proxmox/web/sequences_db.py
```

- [ ] **Step 4: Remove SQLite constants and path setup**

In `autopilot-proxmox/web/app.py`, remove constants and initialization for:

```python
JOBS_DB
MONITOR_DB
DEVICES_DB
SEQUENCES_DB
```

Replace any remaining app state access with Postgres modules imported as:

```python
from web import jobs_pg, device_history_pg, devices_pg, service_health_pg, ts_engine_pg
```

- [ ] **Step 5: Run static guard**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests/test_no_sqlite_runtime.py -q
```

Expected: `1 passed`.

- [ ] **Step 6: Run full local suite**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests -q
```

Expected: full suite passes, with integration tests skipped as before unless live credentials are configured.

- [ ] **Step 7: Commit SQLite removal**

Run:

```bash
git add autopilot-proxmox/tests/test_no_sqlite_runtime.py autopilot-proxmox/web/app.py
git commit -m "refactor(db): remove sqlite runtime state"
```

---

## Task 8: Production Clean Reset And Smoke

**Files:**
- Modify: `autopilot-proxmox/docker-compose.yml` if env var rename is introduced.
- Modify: `autopilot-proxmox/tests/test_ts_engine_startup.py`

- [ ] **Step 1: Verify local compose declares Postgres DSN**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
docker compose config | rg "AUTOPILOT_(TS_ENGINE_)?DATABASE_URL|autopilot-postgres"
```

Expected: output shows Postgres service and a configured app database URL.

- [ ] **Step 2: Run final static checks**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot
git diff --check
cd autopilot-proxmox
.venv/bin/python -m pytest tests/test_no_sqlite_runtime.py tests/test_db_pg.py tests/test_ts_engine_startup.py -q
```

Expected: all pass.

- [ ] **Step 3: Run full suite**

Run:

```bash
cd /Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox
.venv/bin/python -m pytest tests -q
```

Expected: full suite passes.

- [ ] **Step 4: Create PR and merge after review**

Run:

```bash
git push -u origin <branch>
gh pr create --base main --head <branch> --title "Move runtime state to Postgres" --body "Postgres-only clean reset cutover. Removes SQLite runtime state and ports jobs, monitoring, devices, service health, and OSD/WinPE run state to Postgres."
```

After review and merge, watch the Docker build:

```bash
gh run list --branch main --limit 3
gh run watch <run-id> --exit-status
```

- [ ] **Step 5: Deploy production clean reset**

Run:

```bash
ssh root@192.168.2.4 'set -e
cd /opt/ProxmoxVEAutopilot/autopilot-proxmox
curl -fsS http://127.0.0.1:5000/api/version || true
git fetch origin main
git pull --ff-only origin main
docker compose pull
docker compose up -d
curl -fsS http://127.0.0.1:5000/healthz
curl -fsS http://127.0.0.1:5000/api/version
docker compose ps'
```

Expected: web, builder, monitor, and Postgres containers are healthy; `/api/version` reports the merged SHA.

---

## Self-Review

- Spec coverage: This plan covers mandatory Postgres startup, jobs, service health, monitoring, device cache, V2 WinPE/OSD package config, SQLite removal, tests, and clean-reset deployment.
- Placeholder scan: clear.
- Type consistency: New Postgres modules consistently remove `db_path` parameters, use `Jsonb` for JSONB columns, use UUID/string run IDs for V2, and use shared `db_pg.connection()`.
- Scope check: The plan is large but decomposed into independently commit-able surfaces. Each task can ship only after its tests pass.
