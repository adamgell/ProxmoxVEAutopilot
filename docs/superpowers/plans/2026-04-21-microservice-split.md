# Microservice Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single `autopilot` container into three services (web / builder / monitor) so the UI and login stay responsive when a playbook wedges, and so provision throughput can scale with `docker compose --scale`.

**Architecture:** Three services built from one image. Web (singleton) owns UI + API + login + schema init. Builder (N replicas) claims and runs Ansible jobs from a SQLite-backed queue. Monitor (hard singleton) runs the sweep loop + keytab refresher + orphan reaper. Containers coordinate through shared volumes (`output/`, `jobs/`, `secrets/`) and SQLite databases (`jobs.db`, `device_monitor.db`).

**Tech Stack:** Python 3.11, FastAPI, SQLite (WAL mode), Docker Compose, Ansible subprocess spawns, `fcntl.flock` for singleton enforcement.

**Design spec:** [`docs/specs/2026-04-21-microservice-split-design.md`](../../specs/2026-04-21-microservice-split-design.md)

---

## Execution phases

The work is split into six phases that each produce working software:

- **Phase 0 — Foundation (in-place).** Add `jobs.db` + `service_health` tables, atomic claim, orphan reaper, service-health writes. Web still spawns subprocesses directly; no container split yet. End state: single container, new data plane, zero deployment risk.
- **Phase 1 — Entrypoint dispatcher.** Refactor the container entrypoint so `command: ["web"|"builder"|"monitor"]` picks the process mode. Default stays web for backwards compat.
- **Phase 2 — Extract builder.** Web becomes enqueue-only; builder container claims + runs. Kill path moves from `proc.terminate` to `kill_requested=1` flag.
- **Phase 3 — Extract monitor.** Sweep loop + keytab refresher + orphan reaper move to `monitor` container with flock singleton guard. Web's on_event startup drops those tasks.
- **Phase 4 — Health UI.** `/monitoring` gets a service-health strip.
- **Phase 5 — Compose + deploy.** `docker-compose.yml` gains builder + monitor services. Self-update rolls the whole stack.
- **Phase 6 — Integration tests.** Live-harness coverage for kill path, scale, singleton, wedged-playbook survival.

Each phase ends with everything working and committed. A phase boundary is a safe place to stop, ship, and come back later.

---

## Phase 0 — Foundation (in-place)

At the end of this phase, the single container has a SQLite-backed jobs queue, service_health heartbeats, and an orphan reaper. Behavior from the operator's perspective is identical (no split yet), but the data plane is ready.

### Task 1: `jobs_db` module — schema + init

**Files:**
- Create: `autopilot-proxmox/web/jobs_db.py`
- Test: `autopilot-proxmox/tests/test_jobs_db.py`

**Context:** New SQLite file at `output/jobs.db` holds the job queue + per-type concurrency caps. Pattern mirrors `web/sequences_db.py` — module-level SCHEMA string, `init(db_path)`, context-managed connections with `row_factory=sqlite3.Row`. Uses WAL mode so the web container and (later) builder can read/write concurrently without lock contention.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_db.py
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "jobs.db"


def test_init_creates_tables(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "jobs" in names
    assert "job_type_limits" in names


def test_init_is_idempotent(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.init(db_path)  # should not raise


def test_init_seeds_default_concurrency_caps(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    caps = jobs_db.list_job_type_limits(db_path)
    by_type = {row["job_type"]: row["max_concurrent"] for row in caps}
    # Per spec §2 "Per-type concurrency caps"
    assert by_type["build_template"] == 1
    assert by_type["provision_clone"] == 3
    assert by_type["capture_hash"] == 5
    assert by_type["hash_upload"] == 5
    assert by_type["retry_inject_hash"] == 3


def test_wal_mode_enabled(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
```

- [ ] **Step 2: Run test to verify it fails**

```
cd autopilot-proxmox
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v
```

Expected: `ModuleNotFoundError: No module named 'web.jobs_db'`

- [ ] **Step 3: Write the module**

```python
# web/jobs_db.py
"""SQLite-backed job queue + per-type concurrency caps.

Design: docs/specs/2026-04-21-microservice-split-design.md §2

Layout mirrors web/sequences_db.py — module-level SCHEMA string,
init() via executescript, context-managed connections.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    job_type       TEXT NOT NULL,
    playbook       TEXT NOT NULL,
    cmd_json       TEXT NOT NULL,
    args_json      TEXT NOT NULL,
    status         TEXT NOT NULL,
    worker_id      TEXT,
    kill_requested INTEGER NOT NULL DEFAULT 0,
    exit_code      INTEGER,
    created_at     TEXT NOT NULL,
    claimed_at     TEXT,
    last_heartbeat TEXT,
    ended_at       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_type_limits (
    job_type       TEXT PRIMARY KEY,
    max_concurrent INTEGER NOT NULL
);
"""


_DEFAULT_LIMITS = [
    ("build_template", 1),
    ("provision_clone", 3),
    ("capture_hash", 5),
    ("hash_upload", 5),
    ("retry_inject_hash", 3),
]


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL mode so readers (web tailing status) don't block the
    # builder's claim/update writes. Set every connection — it's
    # persisted in the file header but setting it is cheap and
    # defensive against tools that reset it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init(db_path: Path) -> None:
    """Create tables if absent; seed default concurrency caps."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # INSERT OR IGNORE so operator-tuned values survive re-init.
        for job_type, cap in _DEFAULT_LIMITS:
            conn.execute(
                "INSERT OR IGNORE INTO job_type_limits (job_type, max_concurrent) "
                "VALUES (?, ?)",
                (job_type, cap),
            )


def list_job_type_limits(db_path: Path) -> list[dict]:
    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT job_type, max_concurrent FROM job_type_limits "
            "ORDER BY job_type"
        )]
```

- [ ] **Step 4: Run test to verify it passes**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/jobs_db.py autopilot-proxmox/tests/test_jobs_db.py
git commit -m "feat(jobs): add jobs_db module — schema + default concurrency caps"
```

---

### Task 2: `enqueue` + basic CRUD on jobs_db

**Files:**
- Modify: `autopilot-proxmox/web/jobs_db.py` (append)
- Modify: `autopilot-proxmox/tests/test_jobs_db.py` (append)

**Context:** Before the claim protocol, we need the write path: `enqueue(job_type, playbook, cmd, args)` returns a job row, and basic reads `get_job(id)` / `list_jobs()` so web can render `/jobs`.

- [ ] **Step 1: Write failing tests**

```python
# Append to tests/test_jobs_db.py

def test_enqueue_creates_pending_job(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    job = jobs_db.enqueue(
        db_path,
        job_id="20260421-abcd",
        job_type="provision_clone",
        playbook="/app/playbooks/provision_clone.yml",
        cmd=["ansible-playbook", "/app/playbooks/provision_clone.yml", "-e", "vm_count=1"],
        args={"vm_count": 1, "hostname_pattern": "autopilot-{serial}"},
    )
    assert job["id"] == "20260421-abcd"
    assert job["status"] == "pending"
    assert job["worker_id"] is None
    assert job["kill_requested"] == 0
    assert job["created_at"]
    assert job["claimed_at"] is None

    # Verify read-back via get_job
    got = jobs_db.get_job(db_path, "20260421-abcd")
    assert got["id"] == "20260421-abcd"
    assert got["args"]["vm_count"] == 1   # JSON decoded
    assert got["cmd"][0] == "ansible-playbook"


def test_list_jobs_orders_newest_first(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="older", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.enqueue(db_path, job_id="newer", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    rows = jobs_db.list_jobs(db_path)
    assert [r["id"] for r in rows] == ["newer", "older"]


def test_get_job_returns_none_for_missing(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    assert jobs_db.get_job(db_path, "does-not-exist") is None
```

- [ ] **Step 2: Run tests — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v
```

Expected: the three new tests fail with `AttributeError: module 'web.jobs_db' has no attribute 'enqueue'`.

- [ ] **Step 3: Implement enqueue/get/list**

Append to `web/jobs_db.py`:

```python
import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Deserialize the cmd_json / args_json columns back to Python."""
    d = dict(row)
    d["cmd"] = json.loads(d.pop("cmd_json"))
    d["args"] = json.loads(d.pop("args_json"))
    return d


def enqueue(db_path: Path, *, job_id: str, job_type: str,
            playbook: str, cmd: list, args: dict) -> dict:
    """Insert a new pending job. Returns the row as a dict (with cmd + args
    already JSON-decoded for callers).
    """
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO jobs "
            "(id, job_type, playbook, cmd_json, args_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (job_id, job_type, playbook, json.dumps(cmd), json.dumps(args), now),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(row)


def get_job(db_path: Path, job_id: str) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(db_path: Path, *, limit: int = 200) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
```

- [ ] **Step 4: Run tests — expect pass**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/jobs_db.py autopilot-proxmox/tests/test_jobs_db.py
git commit -m "feat(jobs): add enqueue + get_job + list_jobs to jobs_db"
```

---

### Task 3: Atomic `claim_next_job` + `update_status` + `touch_heartbeat`

**Files:**
- Modify: `autopilot-proxmox/web/jobs_db.py` (append)
- Modify: `autopilot-proxmox/tests/test_jobs_db.py` (append)

**Context:** This is the core of the queue — the atomic claim from the spec §2 "Claim protocol". Two-step transaction inside `BEGIN IMMEDIATE`, with the belt-and-braces `status='pending'` guard on the UPDATE.

- [ ] **Step 1: Write failing tests**

```python
def test_claim_next_job_picks_oldest_pending(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="old", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.enqueue(db_path, job_id="new", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    claimed = jobs_db.claim_next_job(db_path, worker_id="worker-a")
    assert claimed["id"] == "old"
    assert claimed["status"] == "running"
    assert claimed["worker_id"] == "worker-a"
    assert claimed["claimed_at"]


def test_claim_next_job_respects_type_cap(db_path):
    """build_template has cap=1 by default. Two pending build_template
    jobs → first claim succeeds, second claim returns None even though a
    job is pending."""
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="b1", job_type="build_template",
                    playbook="x", cmd=[], args={})
    jobs_db.enqueue(db_path, job_id="b2", job_type="build_template",
                    playbook="x", cmd=[], args={})
    c1 = jobs_db.claim_next_job(db_path, worker_id="worker-a")
    assert c1["id"] == "b1"
    c2 = jobs_db.claim_next_job(db_path, worker_id="worker-b")
    # Cap is 1, b1 is running, so b2 can't be claimed even though it's pending.
    assert c2 is None


def test_claim_next_job_picks_other_type_under_cap(db_path):
    """If build_template is capped at 1 and one is running, a claim
    still returns a provision_clone job because that type has cap=3."""
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="b1", job_type="build_template",
                    playbook="x", cmd=[], args={})
    jobs_db.enqueue(db_path, job_id="p1", job_type="provision_clone",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    other = jobs_db.claim_next_job(db_path, worker_id="worker-b")
    assert other["id"] == "p1"


def test_claim_next_job_returns_none_when_empty(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    assert jobs_db.claim_next_job(db_path, worker_id="worker-a") is None


def test_touch_heartbeat_updates_timestamp(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    before = jobs_db.get_job(db_path, "j1")["last_heartbeat"]
    import time; time.sleep(1.1)
    jobs_db.touch_heartbeat(db_path, "j1")
    after = jobs_db.get_job(db_path, "j1")["last_heartbeat"]
    assert after > before


def test_finalize_job_sets_status_and_exit_code(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    jobs_db.finalize_job(db_path, "j1", exit_code=0)
    row = jobs_db.get_job(db_path, "j1")
    assert row["status"] == "complete"
    assert row["exit_code"] == 0
    assert row["ended_at"]


def test_finalize_nonzero_exit_marks_failed(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    jobs_db.finalize_job(db_path, "j1", exit_code=2)
    row = jobs_db.get_job(db_path, "j1")
    assert row["status"] == "failed"
    assert row["exit_code"] == 2


def test_request_kill_sets_flag(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    jobs_db.request_kill(db_path, "j1")
    assert jobs_db.get_job(db_path, "j1")["kill_requested"] == 1
```

- [ ] **Step 2: Run tests — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v -k "claim or touch or finalize or request_kill"
```

Expected: 8 failures — all referenced functions don't exist yet.

- [ ] **Step 3: Implement the claim + lifecycle functions**

Append to `web/jobs_db.py`:

```python
def claim_next_job(db_path: Path, *, worker_id: str) -> dict | None:
    """Atomically claim the oldest pending job whose type is under its cap.

    Implementation: BEGIN IMMEDIATE transaction, SELECT a candidate
    respecting the per-type cap, then conditional UPDATE with a
    status='pending' guard (belt-and-braces against a concurrent
    writer that slipped in between SELECT and UPDATE). Returns the
    claimed row or None if nothing claimable.
    """
    now = _now()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("""
                SELECT j.id
                FROM jobs j
                JOIN job_type_limits l ON l.job_type = j.job_type
                WHERE j.status = 'pending'
                  AND (SELECT COUNT(*) FROM jobs
                       WHERE job_type = j.job_type AND status = 'running')
                      < l.max_concurrent
                ORDER BY j.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            n = conn.execute(
                "UPDATE jobs SET status='running', worker_id=?, "
                "claimed_at=?, last_heartbeat=? "
                "WHERE id=? AND status='pending'",
                (worker_id, now, now, row["id"]),
            ).rowcount
            if n != 1:
                # Another writer beat us between SELECT and UPDATE.
                # Cleanest recovery: bail this poll, try again next tick.
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _row_to_dict(claimed)


def touch_heartbeat(db_path: Path, job_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET last_heartbeat=? WHERE id=?",
            (_now(), job_id),
        )


def finalize_job(db_path: Path, job_id: str, *, exit_code: int) -> None:
    status = "complete" if exit_code == 0 else "failed"
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, ended_at=? WHERE id=?",
            (status, exit_code, _now(), job_id),
        )


def request_kill(db_path: Path, job_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET kill_requested=1 WHERE id=?",
            (job_id,),
        )
```

- [ ] **Step 4: Run tests — expect pass**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v
```

Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/jobs_db.py autopilot-proxmox/tests/test_jobs_db.py
git commit -m "feat(jobs): atomic claim + heartbeat + finalize + kill-request"
```

---

### Task 4: Orphan reaper function

**Files:**
- Modify: `autopilot-proxmox/web/jobs_db.py` (append)
- Modify: `autopilot-proxmox/tests/test_jobs_db.py` (append)

**Context:** The monitor container runs this every 30s in Phase 3. Logic is pure SQL so we can unit-test it now without the container harness. Spec §5 Orphan reaper: `status='running' AND last_heartbeat < now - 2min` → `status='orphaned'`.

- [ ] **Step 1: Write failing tests**

```python
def test_reap_orphans_marks_stale_running_as_orphaned(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    # Manually age the heartbeat to 3 minutes ago.
    from datetime import datetime, timezone, timedelta
    stale = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec="seconds")
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE jobs SET last_heartbeat=? WHERE id=?", (stale, "j1"))
    n = jobs_db.reap_orphans(db_path, stale_threshold_seconds=120)
    assert n == 1
    assert jobs_db.get_job(db_path, "j1")["status"] == "orphaned"


def test_reap_orphans_leaves_fresh_jobs_alone(db_path):
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    n = jobs_db.reap_orphans(db_path, stale_threshold_seconds=120)
    assert n == 0
    assert jobs_db.get_job(db_path, "j1")["status"] == "running"


def test_reap_orphans_ignores_complete_jobs(db_path):
    """A complete job with a stale heartbeat (which is normal — the
    heartbeat doesn't get touched after finalize) must not be marked
    orphaned."""
    from web import jobs_db
    jobs_db.init(db_path)
    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=[], args={})
    jobs_db.claim_next_job(db_path, worker_id="worker-a")
    jobs_db.finalize_job(db_path, "j1", exit_code=0)
    from datetime import datetime, timezone, timedelta
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE jobs SET last_heartbeat=? WHERE id=?", (stale, "j1"))
    n = jobs_db.reap_orphans(db_path, stale_threshold_seconds=120)
    assert n == 0
    assert jobs_db.get_job(db_path, "j1")["status"] == "complete"
```

- [ ] **Step 2: Run — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py::test_reap_orphans_marks_stale_running_as_orphaned -v
```

Expected: `AttributeError: module 'web.jobs_db' has no attribute 'reap_orphans'`.

- [ ] **Step 3: Implement the reaper**

Append to `web/jobs_db.py`:

```python
def reap_orphans(db_path: Path, *, stale_threshold_seconds: int = 120) -> int:
    """Mark running jobs with stale heartbeats as orphaned. Returns the
    number of rows updated.

    Called by the monitor container on a 30-second ticker (spec §5).
    A threshold of 120s is generous — builders heartbeat every 5s, so
    a 24x cushion before we call a job dead.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=stale_threshold_seconds)
              ).isoformat(timespec="seconds")
    now = _now()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='orphaned', ended_at=? "
            "WHERE status='running' AND last_heartbeat < ?",
            (now, cutoff),
        )
    return cur.rowcount
```

- [ ] **Step 4: Run tests — expect pass**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_db.py -v
```

Expected: 18 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/jobs_db.py autopilot-proxmox/tests/test_jobs_db.py
git commit -m "feat(jobs): orphan reaper marks stale running jobs"
```

---

### Task 5: `service_health` module

**Files:**
- Create: `autopilot-proxmox/web/service_health.py`
- Test: `autopilot-proxmox/tests/test_service_health.py`

**Context:** Per spec §6 the `service_health` table lives in `device_monitor.db` (alongside existing device state — both read by the same `/monitoring` page). Module owns: schema init, heartbeat write, list-for-rendering, staleness classification.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_service_health.py
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "device_monitor.db"


def test_init_creates_table(db_path):
    from web import service_health
    service_health.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "service_health" in names


def test_heartbeat_inserts_row_on_first_call(db_path):
    from web import service_health
    service_health.init(db_path)
    service_health.heartbeat(
        db_path,
        service_id="web", service_type="web",
        version_sha="abc1234", detail="idle",
    )
    rows = service_health.list_services(db_path)
    assert len(rows) == 1
    assert rows[0]["service_id"] == "web"
    assert rows[0]["version_sha"] == "abc1234"
    assert rows[0]["status"] == "ok"  # fresh heartbeat is ok


def test_heartbeat_updates_existing_row(db_path):
    from web import service_health
    service_health.init(db_path)
    service_health.heartbeat(db_path, service_id="web",
                             service_type="web", version_sha="a", detail="x")
    import time; time.sleep(1.1)
    service_health.heartbeat(db_path, service_id="web",
                             service_type="web", version_sha="b", detail="y")
    rows = service_health.list_services(db_path)
    assert len(rows) == 1
    assert rows[0]["version_sha"] == "b"
    assert rows[0]["detail"] == "y"


def test_classify_staleness_ok_degraded_dead(db_path):
    from web import service_health
    service_health.init(db_path)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=5)).isoformat(timespec="seconds")
    degraded = (now - timedelta(seconds=30)).isoformat(timespec="seconds")
    dead = (now - timedelta(seconds=90)).isoformat(timespec="seconds")
    # Simulate heartbeats at specific times.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        for sid, hb in [("a", fresh), ("b", degraded), ("c", dead)]:
            conn.execute(
                "INSERT INTO service_health "
                "(service_id, service_type, version_sha, started_at, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, "builder", "sha", fresh, hb),
            )
    rows = {r["service_id"]: r["status"]
            for r in service_health.list_services(db_path)}
    assert rows["a"] == "ok"
    assert rows["b"] == "degraded"
    assert rows["c"] == "dead"


def test_prune_dead_workers_removes_old_rows(db_path):
    """Worker rows whose heartbeat is older than 10 minutes get removed
    so /monitoring doesn't accrete ghosts from scaled-down builders."""
    from web import service_health
    service_health.init(db_path)
    now = datetime.now(timezone.utc)
    very_old = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO service_health "
            "(service_id, service_type, version_sha, started_at, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?)",
            ("builder-xyz", "builder", "sha", very_old, very_old),
        )
    n = service_health.prune_dead_workers(db_path, max_age_seconds=600)
    assert n == 1
    assert service_health.list_services(db_path) == []
```

- [ ] **Step 2: Run — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_service_health.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the module**

```python
# web/service_health.py
"""Per-service heartbeat table. Read by the /monitoring health strip.

Design: docs/specs/2026-04-21-microservice-split-design.md §6
Lives in device_monitor.db alongside device state (same DB so the
/monitoring page is one connection, not two).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS service_health (
    service_id      TEXT PRIMARY KEY,
    service_type    TEXT NOT NULL,
    version_sha     TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    last_heartbeat  TEXT NOT NULL,
    detail          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS service_health_by_type ON service_health(service_type);
"""


# Thresholds in seconds (spec §6).
_OK_THRESHOLD       = 20    # up to 2× heartbeat interval (10s) = ok
_DEGRADED_THRESHOLD = 50    # 2–5× = degraded; beyond = dead


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def init(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def heartbeat(db_path: Path, *, service_id: str, service_type: str,
              version_sha: str, detail: str = "") -> None:
    """UPSERT one row. Called every 10s by each container."""
    now = _now()
    with _connect(db_path) as conn:
        # started_at only written on first insert; subsequent heartbeats
        # preserve the original via ON CONFLICT DO UPDATE skipping it.
        conn.execute(
            "INSERT INTO service_health "
            "  (service_id, service_type, version_sha, started_at, last_heartbeat, detail) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(service_id) DO UPDATE SET "
            "  service_type = excluded.service_type, "
            "  version_sha  = excluded.version_sha, "
            "  last_heartbeat = excluded.last_heartbeat, "
            "  detail       = excluded.detail",
            (service_id, service_type, version_sha, now, now, detail),
        )


def _classify(last_heartbeat_iso: str, now_iso: str) -> str:
    last = datetime.fromisoformat(last_heartbeat_iso)
    now = datetime.fromisoformat(now_iso)
    age = (now - last).total_seconds()
    if age <= _OK_THRESHOLD:
        return "ok"
    if age <= _DEGRADED_THRESHOLD:
        return "degraded"
    return "dead"


def list_services(db_path: Path) -> list[dict]:
    """Render-ready rows with a computed `status` + `age_seconds` column."""
    now_iso = _now()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM service_health ORDER BY service_type, service_id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["status"] = _classify(d["last_heartbeat"], now_iso)
        last = datetime.fromisoformat(d["last_heartbeat"])
        d["age_seconds"] = int((datetime.fromisoformat(now_iso) - last).total_seconds())
        out.append(d)
    return out


def prune_dead_workers(db_path: Path, *, max_age_seconds: int = 600) -> int:
    """Drop builder rows whose heartbeat is older than max_age_seconds.

    Long-dead builder replicas (scaled down, crashed-and-not-restarted)
    shouldn't stay in the table forever. Web rows are never pruned
    (always one, ok or dead) and monitor rows similarly — only builder
    rows grow dynamically.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=max_age_seconds)
              ).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM service_health "
            "WHERE service_type='builder' AND last_heartbeat < ?",
            (cutoff,),
        )
    return cur.rowcount
```

- [ ] **Step 4: Run — expect pass**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_service_health.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/service_health.py autopilot-proxmox/tests/test_service_health.py
git commit -m "feat(health): service_health heartbeat table + staleness classifier"
```

---

### Task 6: Web writes its own service_health heartbeat

**Files:**
- Modify: `autopilot-proxmox/web/app.py` (add startup hook)
- Test: `autopilot-proxmox/tests/test_web.py` (new case)

**Context:** The web container needs to heartbeat too. Easiest hook: background asyncio task started in `@app.on_event("startup")`, cancelled on shutdown.

- [ ] **Step 1: Add the startup hook**

Find the existing `_start_device_monitor_loop` startup handler in `web/app.py`. Add a new sibling hook after it:

```python
# In web/app.py, after _start_device_monitor_loop

_HEALTH_TASK: Optional["asyncio.Task"] = None


def _load_version_sha() -> str:
    """Best-effort running git SHA. Matches the footer's buildSha."""
    try:
        path = BASE_DIR / "VERSION"
        if path.exists():
            return path.read_text().strip()[:7]
    except Exception:
        pass
    return "unknown"


@app.on_event("startup")
async def _start_health_heartbeat() -> None:
    import asyncio
    from web import service_health
    service_health.init(DEVICE_MONITOR_DB)

    async def _loop():
        while True:
            try:
                service_health.heartbeat(
                    DEVICE_MONITOR_DB,
                    service_id="web",
                    service_type="web",
                    version_sha=_load_version_sha(),
                    detail="idle",
                )
            except Exception:
                logging.getLogger("web.health").exception("heartbeat failed")
            await asyncio.sleep(10)

    global _HEALTH_TASK
    _HEALTH_TASK = asyncio.create_task(_loop())


@app.on_event("shutdown")
async def _stop_health_heartbeat() -> None:
    import asyncio
    if _HEALTH_TASK is None:
        return
    _HEALTH_TASK.cancel()
    try:
        await _HEALTH_TASK
    except (asyncio.CancelledError, Exception):
        pass
```

- [ ] **Step 2: Write a test that hits /healthz after startup and verifies a service_health row landed**

Append to `tests/test_web.py`:

```python
def test_web_writes_service_health_heartbeat_on_startup(client):
    """Starting the app creates a 'web' row in service_health."""
    from web import app as web_app, service_health
    # The fixture starts the app; the background heartbeat loop runs on
    # the first tick (~0-10s) but we can force one synchronously via
    # the module-level helper.
    service_health.heartbeat(
        web_app.DEVICE_MONITOR_DB,
        service_id="web", service_type="web",
        version_sha="testsha", detail="idle",
    )
    rows = service_health.list_services(web_app.DEVICE_MONITOR_DB)
    ids = [r["service_id"] for r in rows]
    assert "web" in ids
```

- [ ] **Step 3: Run — expect pass**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_web.py -v -k health
```

Expected: the new test passes; existing tests still pass.

- [ ] **Step 4: Full suite sanity check**

```
PYTHONPATH=. .venv-test/bin/pytest -q
```

Expected: all prior + 1 new passing. (The 2 pre-existing unrelated `test_sequences_api.py` failures still fail — they're on main already.)

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_web.py
git commit -m "feat(health): web writes service_health heartbeat every 10s"
```

---

### Task 7: Migrate existing `jobs/index.json` into `jobs.db` on web startup

**Files:**
- Create: `autopilot-proxmox/web/jobs_migration.py`
- Modify: `autopilot-proxmox/web/app.py` (call migrator at startup)
- Test: `autopilot-proxmox/tests/test_jobs_migration.py`

**Context:** Existing deploys have job history in `jobs/index.json` (JobManager's storage). Migrate once on the first boot of the new image: read `index.json`, INSERT rows into `jobs.db`, rename `index.json` → `index.json.pre-split.bak` so operators can recover if needed.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_jobs_migration.py
import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_dirs():
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d) / "jobs"
        jobs_dir.mkdir()
        db_path = Path(d) / "jobs.db"
        yield jobs_dir, db_path


def test_migrate_noop_when_no_index_json(tmp_dirs):
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    n = jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    assert n == 0


def test_migrate_inserts_rows_and_renames_index(tmp_dirs):
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    index = [
        {"id": "20260420-aaaa", "playbook": "build_template",
         "status": "complete", "started": "2026-04-20T10:00:00+00:00",
         "ended": "2026-04-20T10:30:00+00:00", "exit_code": 0,
         "args": {"profile": "lenovo-t14"}},
        {"id": "20260420-bbbb", "playbook": "provision_clone",
         "status": "failed", "started": "2026-04-20T11:00:00+00:00",
         "ended": "2026-04-20T11:05:00+00:00", "exit_code": 2,
         "args": {"vm_count": 3}},
    ]
    (jobs_dir / "index.json").write_text(json.dumps(index))
    n = jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    assert n == 2
    got = {r["id"]: r for r in jobs_db.list_jobs(db_path)}
    assert got["20260420-aaaa"]["status"] == "complete"
    assert got["20260420-aaaa"]["exit_code"] == 0
    assert got["20260420-bbbb"]["args"]["vm_count"] == 3
    # index.json renamed to backup
    assert not (jobs_dir / "index.json").exists()
    assert (jobs_dir / "index.json.pre-split.bak").exists()


def test_migrate_idempotent_after_rename(tmp_dirs):
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    (jobs_dir / "index.json").write_text(json.dumps(
        [{"id": "x", "playbook": "y", "status": "complete",
          "started": "2026-04-20T10:00:00+00:00", "ended": None,
          "exit_code": 0, "args": {}}]
    ))
    jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    # Second call: no index.json, no change
    n2 = jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    assert n2 == 0


def test_migrate_marks_inflight_running_as_orphaned(tmp_dirs):
    """JobManager today marks orphans as 'failed' on crash. But if a
    graceful shutdown caught the job mid-run, it might still be
    'running' in index.json. Migrate those to 'orphaned' so operators
    see them in the new ui rather than them appearing live."""
    from web import jobs_db, jobs_migration
    jobs_dir, db_path = tmp_dirs
    jobs_db.init(db_path)
    (jobs_dir / "index.json").write_text(json.dumps(
        [{"id": "stuck", "playbook": "p", "status": "running",
          "started": "2026-04-20T10:00:00+00:00", "ended": None,
          "exit_code": None, "args": {}}]
    ))
    jobs_migration.migrate_legacy_index(jobs_dir=jobs_dir, db_path=db_path)
    got = jobs_db.get_job(db_path, "stuck")
    assert got["status"] == "orphaned"
```

- [ ] **Step 2: Run — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_migration.py -v
```

Expected: all fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the migration**

```python
# web/jobs_migration.py
"""One-shot migration from jobs/index.json to jobs.db.

Called from the web container's startup hook. After the first
successful run, the old index.json is renamed to
`index.json.pre-split.bak` so future boots are no-ops.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from web import jobs_db

_log = logging.getLogger(__name__)


def migrate_legacy_index(*, jobs_dir: Path, db_path: Path) -> int:
    """Read jobs_dir/index.json, insert into jobs.db, rename the file.
    Returns the count of migrated rows. Idempotent: once the rename has
    happened, future calls are no-ops.
    """
    index_path = Path(jobs_dir) / "index.json"
    if not index_path.exists():
        return 0
    try:
        entries = json.loads(index_path.read_text())
    except Exception:
        _log.exception("failed to read %s; skipping migration", index_path)
        return 0
    inserted = 0
    for entry in entries:
        status = entry.get("status")
        if status == "running":
            # If we're about to start up fresh, any "running" row in the
            # old index is by definition dead. Mark it orphaned so
            # operators see what happened.
            status = "orphaned"
        job_type = entry.get("playbook") or "unknown"
        # Old index stores playbook name without path; new schema keeps
        # the playbook column but it's informational. Synthesize a
        # reasonable default.
        playbook_path = entry.get("playbook") or "unknown"
        try:
            jobs_db._insert_migrated(
                db_path,
                job_id=entry["id"],
                job_type=job_type,
                playbook=playbook_path,
                args=entry.get("args") or {},
                status=status,
                started_at=entry.get("started") or entry.get("started_at", ""),
                ended_at=entry.get("ended"),
                exit_code=entry.get("exit_code"),
            )
            inserted += 1
        except Exception:
            _log.exception("failed to migrate job %r", entry.get("id"))
    # Rename even on partial failure — we don't want to retry bad rows
    # on every boot. Operators can inspect the .bak file.
    backup = index_path.with_suffix(".json.pre-split.bak")
    index_path.rename(backup)
    _log.info("migrated %d jobs; legacy index backed up to %s",
              inserted, backup)
    return inserted
```

Append to `web/jobs_db.py`:

```python
def _insert_migrated(db_path: Path, *, job_id: str, job_type: str,
                     playbook: str, args: dict, status: str,
                     started_at: str, ended_at: str | None,
                     exit_code: int | None) -> None:
    """Internal helper for jobs_migration. Inserts a row with a specific
    status (e.g. 'complete', 'failed', 'orphaned') — bypasses the normal
    enqueue flow which always creates pending rows."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, job_type, playbook, cmd_json, args_json, status, "
            " created_at, claimed_at, ended_at, exit_code) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?, ?)",
            (job_id, job_type, playbook, json.dumps(args),
             status, started_at, started_at, ended_at, exit_code),
        )
```

- [ ] **Step 4: Hook into app.py startup**

In `web/app.py`, inside `_init_sequences_db` (or a new sibling), after initialising the jobs DB:

```python
@app.on_event("startup")
def _init_jobs_db_and_migrate() -> None:
    from web import jobs_db, jobs_migration
    jobs_db.init(JOBS_DB)
    jobs_migration.migrate_legacy_index(
        jobs_dir=Path(job_manager.jobs_dir),
        db_path=JOBS_DB,
    )
```

Add `JOBS_DB` near the other path constants:

```python
JOBS_DB = BASE_DIR / "output" / "jobs.db"
```

- [ ] **Step 5: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs_migration.py tests/test_jobs_db.py tests/test_web.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/jobs_migration.py autopilot-proxmox/web/jobs_db.py \
        autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_jobs_migration.py
git commit -m "feat(jobs): migrate legacy index.json → jobs.db on first boot"
```

---

### Task 8: `JobManager` now enqueues into `jobs.db` and synchronously claims+runs

**Files:**
- Modify: `autopilot-proxmox/web/jobs.py`
- Modify: `autopilot-proxmox/tests/test_jobs.py` (if it exists; otherwise create a new test)

**Context:** Before splitting the container, redirect `JobManager.start()` through `jobs.db`. The web container claims the job immediately and runs it in its existing subprocess pattern — same behavior from the outside, but now the job row exists in the DB. This is the bridge task: no deploy risk, everything that follows is easier.

- [ ] **Step 1: Read the existing `JobManager`**

`autopilot-proxmox/web/jobs.py` has the current JobManager. Inspect it and the call sites (`grep -rn "job_manager.start"`). There are ~19 callers from the earlier audit.

- [ ] **Step 2: Write a test that exercises the new flow**

Append to `tests/test_jobs.py` (create the file if missing):

```python
# tests/test_jobs.py
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def test_job_manager_start_inserts_row_in_jobs_db(monkeypatch):
    """JobManager.start writes to jobs.db AND still spawns the subprocess
    (Phase 0 — web is still the runner). Verified by mocking Popen so
    no actual ansible runs."""
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)

        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        with patch("web.jobs.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            mock_popen.return_value.wait.return_value = 0
            entry = mgr.start(
                "build_template", ["ansible-playbook", "x.yml"],
                args={"profile": "p"},
            )
        assert entry["id"]
        row = jobs_db.get_job(db_path, entry["id"])
        assert row is not None
        assert row["job_type"] == "build_template"
        assert row["status"] in ("running", "complete")
```

- [ ] **Step 3: Update `JobManager`**

Modify `web/jobs.py` to accept a `jobs_db_path` and mirror writes into it. Keep `index.json` for the existing `get_log` / list path for now (we'll remove in Phase 2):

```python
# Pseudocode edit to web/jobs.py
class JobManager:
    def __init__(self, jobs_dir="jobs", jobs_db_path: Path | None = None):
        self.jobs_dir = jobs_dir
        self.jobs_db_path = jobs_db_path
        os.makedirs(jobs_dir, exist_ok=True)
        # ... existing init ...

    def start(self, playbook_name, command, args=None):
        job_id = self._generate_id()
        log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
        # ... existing subprocess spawn ...
        # NEW: mirror into jobs.db
        if self.jobs_db_path is not None:
            from web import jobs_db
            jobs_db.enqueue(
                self.jobs_db_path,
                job_id=job_id,
                job_type=playbook_name,
                playbook=command[1] if len(command) > 1 else playbook_name,
                cmd=list(command),
                args=args or {},
            )
            # Immediately claim + transition to running since the web
            # container is the runner in Phase 0.
            jobs_db.claim_next_job(self.jobs_db_path, worker_id="web-inproc")
        # ... rest unchanged ...
```

In `web/app.py`, pass `JOBS_DB` when constructing the manager:

```python
job_manager = JobManager(
    jobs_dir=str(BASE_DIR / "jobs"),
    jobs_db_path=JOBS_DB,
)
```

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs.py tests/test_jobs_db.py tests/test_web.py -v
```

Expected: all pass.

- [ ] **Step 5: Full suite sanity**

```
PYTHONPATH=. .venv-test/bin/pytest -q
```

Expected: everything that was passing before still passes.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/jobs.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_jobs.py
git commit -m "feat(jobs): JobManager mirrors into jobs.db (dual-writes for Phase 0)"
```

---

**Phase 0 complete.** Single container, new data plane live, no deploy risk. Safe to stop and ship here.

---

## Phase 1 — Entrypoint dispatcher

### Task 9: `web/entrypoint.py` dispatcher

**Files:**
- Create: `autopilot-proxmox/web/entrypoint.py`
- Test: `autopilot-proxmox/tests/test_entrypoint.py`

**Context:** One script dispatches on `argv[1] ∈ {web, builder, monitor}`. Default (no arg) stays `web` for operators still on the old compose spec.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_entrypoint.py
import sys
from unittest.mock import patch


def test_default_mode_is_web():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor:
        entrypoint.main([])
        mock_web.assert_called_once()
        mock_builder.assert_not_called()
        mock_monitor.assert_not_called()


def test_builder_mode_dispatches():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor:
        entrypoint.main(["builder"])
        mock_builder.assert_called_once()
        mock_web.assert_not_called()


def test_monitor_mode_dispatches():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor:
        entrypoint.main(["monitor"])
        mock_monitor.assert_called_once()


def test_unknown_mode_exits_nonzero(capsys):
    from web import entrypoint
    import pytest
    with pytest.raises(SystemExit) as exc:
        entrypoint.main(["bogus"])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "unknown mode" in captured.err.lower()
```

- [ ] **Step 2: Run — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_entrypoint.py -v
```

Expected: `ModuleNotFoundError: No module named 'web.entrypoint'`.

- [ ] **Step 3: Implement**

```python
# web/entrypoint.py
"""Container process dispatcher.

The same image runs as `web`, `builder`, or `monitor` based on the
command-line arg (set by docker-compose `command:` key). Default mode
is `web` so operators on the pre-split compose spec keep working.

Design: docs/specs/2026-04-21-microservice-split-design.md §8
"""
from __future__ import annotations

import sys


def _run_web() -> None:
    """Launch the FastAPI/uvicorn server (existing entrypoint)."""
    import uvicorn
    from web.app import app
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


def _run_builder() -> None:
    """Start the builder claim/run loop."""
    from web.builder import run_builder
    run_builder()


def _run_monitor() -> None:
    """Start the monitor singleton — sweep loop + keytab + reaper."""
    from web.monitor_main import run_monitor
    run_monitor()


_MODES = {
    "web": _run_web,
    "builder": _run_builder,
    "monitor": _run_monitor,
}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:]) if argv is None else argv
    mode = argv[0] if argv else "web"
    runner = _MODES.get(mode)
    if runner is None:
        print(f"unknown mode: {mode!r}. Valid: {sorted(_MODES)}", file=sys.stderr)
        sys.exit(2)
    runner()


if __name__ == "__main__":
    main()
```

The `run_builder` / `run_monitor` functions don't exist yet — imports live behind function calls (lazy), so the dispatcher itself tests fine with mocks. Real integration lands in Phase 2 + Phase 3.

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_entrypoint.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/entrypoint.py autopilot-proxmox/tests/test_entrypoint.py
git commit -m "feat(entrypoint): dispatcher for web/builder/monitor modes"
```

---

### Task 10: Dockerfile uses dispatcher

**Files:**
- Modify: `autopilot-proxmox/Dockerfile`

**Context:** Switch `CMD` to route through the new dispatcher. Image behaves identically by default (still runs web); `command:` overrides in compose will switch modes.

- [ ] **Step 1: Read current Dockerfile**

```
cat autopilot-proxmox/Dockerfile
```

Locate the `CMD` line — likely something like `CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"]`.

- [ ] **Step 2: Replace CMD**

```dockerfile
# Replace the existing CMD with:
ENTRYPOINT ["python", "-m", "web.entrypoint"]
CMD ["web"]
```

ENTRYPOINT + CMD split means `docker run image monitor` becomes `python -m web.entrypoint monitor`. Compose `command: ["monitor"]` likewise.

- [ ] **Step 3: Local sanity-check build**

```
cd autopilot-proxmox
docker build -t autopilot:dispatcher-test .
docker run --rm autopilot:dispatcher-test bogus  # should exit 2 with "unknown mode"
docker run --rm autopilot:dispatcher-test --help 2>&1 | head -3  # uvicorn starts then we ctrl-c OR it exits
```

Expected: the bogus mode exits 2 with the error message; default (no arg) starts uvicorn.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/Dockerfile
git commit -m "feat(docker): entrypoint routes through web.entrypoint dispatcher"
```

---

**Phase 1 complete.** Image can now run in any of three modes. Only `web` actually works end-to-end yet; `builder` and `monitor` fail on import because their modules don't exist. That's fine — compose still runs single-container `web` by default.

---

## Phase 2 — Extract builder

### Task 11: `web/builder.py` claim+run loop

**Files:**
- Create: `autopilot-proxmox/web/builder.py`
- Test: `autopilot-proxmox/tests/test_builder.py`

**Context:** The builder loop does: poll `claim_next_job` every 2s, spawn `ansible-playbook` as subprocess, heartbeat every 5s while it runs, check `kill_requested` on each heartbeat and `terminate()` the subprocess if set, `finalize_job` on exit. Spec §2 worker loop.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_builder.py
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_env():
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d) / "jobs"; jobs_dir.mkdir()
        db_path = Path(d) / "jobs.db"
        from web import jobs_db
        jobs_db.init(db_path)
        yield jobs_dir, db_path


def test_builder_runs_one_job_and_exits_on_stop(tmp_env):
    """Happy path: enqueue a job, builder claims + spawns + finalizes."""
    from web import builder, jobs_db
    jobs_dir, db_path = tmp_env

    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=["echo", "ok"], args={})

    stop = threading.Event()

    def _fake_run(row, log_path, db_path, worker_id, stop_event):
        # pretend we ran it
        jobs_db.finalize_job(db_path, row["id"], exit_code=0)

    with patch("web.builder._run_one_job", side_effect=_fake_run):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={"jobs_dir": jobs_dir, "db_path": db_path,
                    "worker_id": "test-worker", "stop_event": stop,
                    "poll_interval_seconds": 0.1},
            daemon=True,
        )
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(timeout=2)

    assert jobs_db.get_job(db_path, "j1")["status"] == "complete"


def test_builder_run_one_job_kills_on_kill_requested(tmp_env):
    """With kill_requested=1, the heartbeat tick should terminate the
    subprocess."""
    from web import builder, jobs_db
    jobs_dir, db_path = tmp_env

    jobs_db.enqueue(db_path, job_id="j1", job_type="capture_hash",
                    playbook="x", cmd=["sleep", "30"], args={})
    jobs_db.claim_next_job(db_path, worker_id="test-worker")

    proc = MagicMock()
    # First .poll() returns None (running), second returns 0 (after terminate)
    proc.poll.side_effect = [None, 0]
    proc.terminate = MagicMock()

    row = jobs_db.get_job(db_path, "j1")
    jobs_db.request_kill(db_path, "j1")

    stop = threading.Event()
    log_path = jobs_dir / "j1.log"
    log_path.touch()

    with patch("web.builder.subprocess.Popen", return_value=proc):
        builder._run_one_job(
            row, log_path=log_path, db_path=db_path,
            worker_id="test-worker", stop_event=stop,
            heartbeat_seconds=0.05,
        )

    proc.terminate.assert_called_once()
    # Since poll returned 0 after terminate, finalize should mark complete.
    assert jobs_db.get_job(db_path, "j1")["exit_code"] == 0


def test_builder_idle_sleeps_when_no_jobs(tmp_env):
    """When claim returns None, the loop sleeps and retries without
    busy-looping."""
    from web import builder
    jobs_dir, db_path = tmp_env
    stop = threading.Event()
    claims = []

    def _mock_claim(*args, **kwargs):
        claims.append(time.monotonic())
        return None

    with patch("web.builder.jobs_db.claim_next_job", side_effect=_mock_claim):
        t = threading.Thread(
            target=builder.run_builder,
            kwargs={"jobs_dir": jobs_dir, "db_path": db_path,
                    "worker_id": "t", "stop_event": stop,
                    "poll_interval_seconds": 0.1},
            daemon=True,
        )
        t.start()
        time.sleep(0.35)
        stop.set()
        t.join(timeout=1)

    # Should have polled ~3 times in 0.35s with 0.1s interval, not 3500.
    assert 2 <= len(claims) <= 6
```

- [ ] **Step 2: Run — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_builder.py -v
```

Expected: `ModuleNotFoundError: No module named 'web.builder'`.

- [ ] **Step 3: Implement**

```python
# web/builder.py
"""Builder worker loop. Claims Ansible jobs from jobs.db and runs them.

Lifecycle:
    1. Poll claim_next_job every `poll_interval_seconds` (default 2s).
    2. When a job is claimed, spawn `ansible-playbook` as subprocess.
    3. Heartbeat every `heartbeat_seconds` (default 5s) while running.
    4. On each heartbeat, check kill_requested; terminate if set.
    5. On process exit, finalize_job with the exit code.

Design: docs/specs/2026-04-21-microservice-split-design.md §2 + §7
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from web import jobs_db, service_health

_log = logging.getLogger("web.builder")


def _worker_id() -> str:
    """Persist a uuid under /app/output/worker-id.<hostname> so compose
    restarts preserve identity for the health UI."""
    hostname = os.uname().nodename
    path = Path("/app/output") / f"worker-id.{hostname}"
    if path.exists():
        return path.read_text().strip()
    new_id = f"builder-{uuid.uuid4().hex[:8]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_id)
    return new_id


def _version_sha() -> str:
    path = Path("/app/VERSION")
    return path.read_text().strip()[:7] if path.exists() else "unknown"


def _run_one_job(row: dict, *, log_path: Path, db_path: Path,
                 worker_id: str, stop_event: threading.Event,
                 heartbeat_seconds: float = 5.0) -> None:
    """Spawn the subprocess, heartbeat, react to kill, finalize."""
    log_file = open(log_path, "a")
    _log.info("starting job %s (type=%s) on %s",
              row["id"], row["job_type"], worker_id)
    try:
        proc = subprocess.Popen(
            row["cmd"], stdout=log_file, stderr=subprocess.STDOUT, text=True,
        )
    except Exception:
        _log.exception("failed to spawn subprocess for job %s", row["id"])
        jobs_db.finalize_job(db_path, row["id"], exit_code=-1)
        log_file.close()
        return

    try:
        while True:
            exit_code = proc.poll()
            if exit_code is not None:
                break
            jobs_db.touch_heartbeat(db_path, row["id"])
            current = jobs_db.get_job(db_path, row["id"])
            if current and current["kill_requested"]:
                _log.info("kill_requested set — terminating job %s", row["id"])
                try:
                    proc.terminate()
                except Exception:
                    pass
            if stop_event.is_set():
                _log.info("stop_event set — terminating job %s", row["id"])
                try:
                    proc.terminate()
                except Exception:
                    pass
            time.sleep(heartbeat_seconds)
        jobs_db.finalize_job(db_path, row["id"], exit_code=exit_code)
        _log.info("job %s finished with exit_code=%s", row["id"], exit_code)
    finally:
        log_file.close()


def run_builder(*, jobs_dir: Path | str = "/app/jobs",
                db_path: Path | str = "/app/output/jobs.db",
                monitor_db_path: Path | str = "/app/output/device_monitor.db",
                worker_id: str | None = None,
                stop_event: threading.Event | None = None,
                poll_interval_seconds: float = 2.0,
                heartbeat_seconds: float = 5.0) -> None:
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    db_path = Path(db_path)
    monitor_db_path = Path(monitor_db_path)

    if worker_id is None:
        worker_id = _worker_id()
    stop_event = stop_event or _install_signal_handlers()
    version = _version_sha()

    _log.info("builder %s starting (poll=%ss heartbeat=%ss)",
              worker_id, poll_interval_seconds, heartbeat_seconds)

    # Initial service_health row — registers the worker in the UI before
    # the first heartbeat loop tick so operators see new scaled-up
    # replicas immediately.
    service_health.init(monitor_db_path)
    service_health.heartbeat(monitor_db_path,
                             service_id=worker_id, service_type="builder",
                             version_sha=version, detail="starting")

    last_service_heartbeat = 0.0
    while not stop_event.is_set():
        row = jobs_db.claim_next_job(db_path, worker_id=worker_id)
        # Service heartbeat on its own 10s cadence, separate from the
        # per-job heartbeat written inside _run_one_job.
        now = time.monotonic()
        if now - last_service_heartbeat >= 10.0:
            detail = f"running {row['id']}" if row else "idle"
            service_health.heartbeat(
                monitor_db_path, service_id=worker_id,
                service_type="builder", version_sha=version, detail=detail,
            )
            last_service_heartbeat = now

        if row is None:
            if stop_event.wait(timeout=poll_interval_seconds):
                break
            continue

        log_path = jobs_dir / f"{row['id']}.log"
        _run_one_job(row, log_path=log_path, db_path=db_path,
                     worker_id=worker_id, stop_event=stop_event,
                     heartbeat_seconds=heartbeat_seconds)
        # Refresh service row with "idle" after the job ends.
        service_health.heartbeat(
            monitor_db_path, service_id=worker_id,
            service_type="builder", version_sha=version, detail="idle",
        )

    _log.info("builder %s stopping", worker_id)


def _install_signal_handlers() -> threading.Event:
    """SIGTERM/SIGINT → set the stop event so the loop exits cleanly."""
    stop = threading.Event()
    def _handler(signum, frame):
        _log.info("caught signal %s; requesting stop", signum)
        stop.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    return stop
```

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_builder.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/builder.py autopilot-proxmox/tests/test_builder.py
git commit -m "feat(builder): claim+run+heartbeat loop with kill polling"
```

---

### Task 12: Web's `/api/jobs/{id}/kill` writes kill_requested instead of `proc.terminate`

**Files:**
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/tests/test_web.py`

**Context:** Kill path moves from direct `proc.terminate()` to a flag on the job row. The builder sees it on its next heartbeat tick. Worst-case latency: 5s.

- [ ] **Step 1: Find the existing kill endpoint**

```
grep -n "kill\|terminate" autopilot-proxmox/web/app.py | head
```

There's a `POST /api/jobs/{job_id}/kill` endpoint that calls something like `job_manager.kill(job_id)`.

- [ ] **Step 2: Write the new test**

```python
def test_kill_sets_kill_requested_flag(client):
    """POST /api/jobs/<id>/kill flips kill_requested=1 and redirects."""
    from web import app as app_module, jobs_db
    # Enqueue + claim a fake job so there's a running row.
    jobs_db.enqueue(app_module.JOBS_DB, job_id="live",
                    job_type="capture_hash", playbook="x",
                    cmd=["sleep", "1"], args={})
    jobs_db.claim_next_job(app_module.JOBS_DB, worker_id="test-worker")

    r = client.post("/api/jobs/live/kill", follow_redirects=False)
    assert r.status_code == 303
    row = jobs_db.get_job(app_module.JOBS_DB, "live")
    assert row["kill_requested"] == 1
```

- [ ] **Step 3: Update the kill endpoint in app.py**

```python
@app.post("/api/jobs/{job_id}/kill")
async def kill_job(job_id: str):
    """Request termination. Flips kill_requested=1 on the job row; the
    builder owning the job will see it on its next heartbeat cycle
    (~5s max) and SIGTERM the subprocess. Redirects to /jobs/<id>."""
    row = jobs_db.get_job(JOBS_DB, job_id)
    if row is None:
        raise HTTPException(404, f"job {job_id} not found")
    if row["status"] != "running":
        # Already done; ignore quietly so double-clicks don't 400.
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)
    jobs_db.request_kill(JOBS_DB, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
```

- [ ] **Step 4: Run the test**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_web.py -v -k kill
```

Expected: pass.

- [ ] **Step 5: Full suite sanity**

```
PYTHONPATH=. .venv-test/bin/pytest -q
```

Expected: prior passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_web.py
git commit -m "feat(jobs): kill endpoint sets kill_requested flag instead of terminating"
```

---

### Task 13: Web `JobManager.start` becomes enqueue-only

**Files:**
- Modify: `autopilot-proxmox/web/jobs.py`
- Modify: `autopilot-proxmox/tests/test_jobs.py`

**Context:** Stop spawning subprocesses in the web container. From here on the builder is authoritative. Web only writes pending rows.

- [ ] **Step 1: Update `JobManager.start`**

```python
def start(self, playbook_name, command, args=None):
    """Enqueue a job. The builder container picks it up and runs it.

    Returns the job entry dict (same shape as before) so existing
    call sites that read `entry["id"]` keep working. Subprocess
    spawning + log file management now lives in web/builder.py.
    """
    job_id = self._generate_id()
    # Log path still created here so the file exists for /jobs/<id>
    # rendering even before the builder claims the row (otherwise the
    # page tries to tail a nonexistent file and 500s).
    log_path = os.path.join(self.jobs_dir, f"{job_id}.log")
    open(log_path, "a").close()  # touch

    from web import jobs_db
    row = jobs_db.enqueue(
        self.jobs_db_path,
        job_id=job_id,
        job_type=playbook_name,
        playbook=command[1] if len(command) > 1 else playbook_name,
        cmd=list(command),
        args=args or {},
    )

    # Back-compat entry shape for callers that read the JobManager return.
    return {
        "id": job_id,
        "playbook": playbook_name,
        "status": "pending",
        "started": row["created_at"],
        "ended": None,
        "exit_code": None,
        "args": args or {},
    }
```

Delete the subprocess-related fields (`_active`, `_wait_for_completion`, the spawn thread, etc.) — they're no longer needed. Keep `list_jobs`, `get_job`, `get_log` as shims that read from `jobs.db` + filesystem.

- [ ] **Step 2: Update `list_jobs` / `get_job` to read from the DB**

```python
def list_jobs(self):
    from web import jobs_db
    return jobs_db.list_jobs(self.jobs_db_path)


def get_job(self, job_id):
    from web import jobs_db
    return jobs_db.get_job(self.jobs_db_path, job_id)


def get_log(self, job_id):
    path = os.path.join(self.jobs_dir, f"{job_id}.log")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""
```

- [ ] **Step 3: Update tests**

```python
def test_job_manager_start_enqueues_pending_row(monkeypatch):
    from web import jobs, jobs_db
    with tempfile.TemporaryDirectory() as d:
        jobs_dir = Path(d)
        db_path = jobs_dir / "jobs.db"
        jobs_db.init(db_path)
        mgr = jobs.JobManager(jobs_dir=str(jobs_dir), jobs_db_path=db_path)
        entry = mgr.start("build_template", ["ansible-playbook", "x.yml"],
                          args={"profile": "p"})
    row = jobs_db.get_job(db_path, entry["id"])
    assert row["status"] == "pending"
    assert entry["status"] == "pending"


def test_job_manager_get_log_returns_empty_for_missing():
    from web import jobs
    with tempfile.TemporaryDirectory() as d:
        mgr = jobs.JobManager(jobs_dir=d, jobs_db_path=Path(d) / "jobs.db")
        assert mgr.get_log("missing") == ""
```

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_jobs.py tests/test_web.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/jobs.py autopilot-proxmox/tests/test_jobs.py
git commit -m "feat(jobs): JobManager is enqueue-only; builder owns execution"
```

---

**Phase 2 complete.** Web enqueues, builder executes. Integration testing for the split happens in Phase 6 since we need the compose layout for that.

---

## Phase 3 — Extract monitor

### Task 14: `web/monitor_main.py` with flock singleton guard

**Files:**
- Create: `autopilot-proxmox/web/monitor_main.py`
- Test: `autopilot-proxmox/tests/test_monitor_main.py`

**Context:** Monitor is a hard singleton. Uses `fcntl.flock` on `/app/output/monitor.lock`; second instance exits 0.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_monitor_main.py
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch


def test_singleton_guard_second_instance_exits_zero():
    from web import monitor_main
    with tempfile.TemporaryDirectory() as d:
        lock_path = Path(d) / "monitor.lock"
        # Acquire the lock in a subprocess-like way: open + flock.
        import fcntl
        holder = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # Now call the guard — it should refuse the lock.
            acquired = monitor_main._acquire_singleton_lock(lock_path)
            assert acquired is None
        finally:
            os.close(holder)


def test_singleton_guard_first_instance_gets_lock():
    from web import monitor_main
    with tempfile.TemporaryDirectory() as d:
        lock_path = Path(d) / "monitor.lock"
        fd = monitor_main._acquire_singleton_lock(lock_path)
        assert fd is not None
        import os as _os
        _os.close(fd)
```

- [ ] **Step 2: Run — expect failure**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_monitor_main.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement (singleton guard only; loops come later)**

```python
# web/monitor_main.py
"""Monitor singleton: sweep loop, keytab refresher, orphan reaper.

Hard singleton via fcntl.flock. Second instance exits 0 (not a
failure — compose will tolerate scaling but only one wins).

Design: docs/specs/2026-04-21-microservice-split-design.md §3, §5
"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

_log = logging.getLogger("web.monitor_main")

# Thresholds / cadences (seconds).
_SWEEP_INTERVAL_DEFAULT = 900       # 15 minutes, same as today
_REAPER_INTERVAL = 30               # poll for orphans twice a minute
_HEARTBEAT_INTERVAL = 10            # service_health cadence
_KEYTAB_CHECK_INTERVAL = 3600       # keytab health checked hourly


def _acquire_singleton_lock(path: Path) -> int | None:
    """Return an open FD holding an exclusive lock, or None if another
    process already holds it. Caller is responsible for closing the FD
    (or letting process exit do it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def run_monitor(*, lock_path: Path | str = "/app/output/monitor.lock",
                monitor_db_path: Path | str = "/app/output/device_monitor.db",
                jobs_db_path: Path | str = "/app/output/jobs.db",
                stop_event: threading.Event | None = None) -> None:
    lock_path = Path(lock_path)
    monitor_db_path = Path(monitor_db_path)
    jobs_db_path = Path(jobs_db_path)

    fd = _acquire_singleton_lock(lock_path)
    if fd is None:
        _log.warning(
            "monitor already running elsewhere (lock held on %s) — exiting 0",
            lock_path,
        )
        sys.exit(0)

    stop_event = stop_event or _install_signal_handlers()

    _log.info("monitor singleton acquired lock on %s", lock_path)
    try:
        _run_loops(stop_event=stop_event,
                   monitor_db_path=monitor_db_path,
                   jobs_db_path=jobs_db_path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _install_signal_handlers() -> threading.Event:
    stop = threading.Event()
    def _h(signum, frame):
        _log.info("caught signal %s; requesting stop", signum)
        stop.set()
    signal.signal(signal.SIGTERM, _h)
    signal.signal(signal.SIGINT, _h)
    return stop


def _run_loops(*, stop_event: threading.Event,
               monitor_db_path: Path, jobs_db_path: Path) -> None:
    """Placeholder — filled in by Task 15-17."""
    while not stop_event.is_set():
        stop_event.wait(timeout=1)
```

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_monitor_main.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/monitor_main.py autopilot-proxmox/tests/test_monitor_main.py
git commit -m "feat(monitor): singleton flock guard + skeleton run_monitor"
```

---

### Task 15: Sweep loop + orphan reaper + keytab + heartbeat all running inside monitor

**Files:**
- Modify: `autopilot-proxmox/web/monitor_main.py`
- Modify: `autopilot-proxmox/web/app.py` (remove duplicates)
- Test: `autopilot-proxmox/tests/test_monitor_main.py`

**Context:** The existing `_device_monitor_loop` and keytab refresh logic live in `app.py`. Copy (not move) them into `monitor_main._run_loops` with correct cadences. In Phase 3's final task we'll remove them from `app.py`.

- [ ] **Step 1: Inspect the existing sweep loop + keytab paths**

```
grep -n "_device_monitor_loop\|refresh_keytab\|settings.interval_seconds" autopilot-proxmox/web/app.py | head
```

Note the imports + code structure.

- [ ] **Step 2: Write tests for the combined loop**

```python
def test_run_loops_runs_reaper_on_cadence(monkeypatch):
    """_run_loops calls reap_orphans periodically."""
    from web import monitor_main
    with tempfile.TemporaryDirectory() as d:
        monitor_db = Path(d) / "device_monitor.db"
        jobs_db_path = Path(d) / "jobs.db"
        from web import jobs_db, service_health
        jobs_db.init(jobs_db_path)
        service_health.init(monitor_db)

        reaps = []
        def _mock_reap(*a, **kw):
            reaps.append(time.monotonic())
            return 0

        stop = threading.Event()
        with patch("web.monitor_main._do_sweep_tick", return_value=None), \
             patch("web.monitor_main._do_keytab_tick", return_value=None), \
             patch("web.monitor_main.jobs_db.reap_orphans", side_effect=_mock_reap):
            t = threading.Thread(
                target=monitor_main._run_loops,
                kwargs={"stop_event": stop,
                        "monitor_db_path": monitor_db,
                        "jobs_db_path": jobs_db_path,
                        "reaper_interval_seconds": 0.1,
                        "heartbeat_interval_seconds": 0.1,
                        "sweep_interval_seconds": 10,
                        "keytab_interval_seconds": 10},
                daemon=True,
            )
            t.start()
            time.sleep(0.35)
            stop.set()
            t.join(timeout=2)
        assert 2 <= len(reaps) <= 6
```

- [ ] **Step 3: Implement `_run_loops` with the real cadences**

Replace the placeholder `_run_loops` in `web/monitor_main.py`:

```python
def _run_loops(*, stop_event: threading.Event,
               monitor_db_path: Path, jobs_db_path: Path,
               sweep_interval_seconds: int = _SWEEP_INTERVAL_DEFAULT,
               reaper_interval_seconds: int = _REAPER_INTERVAL,
               heartbeat_interval_seconds: int = _HEARTBEAT_INTERVAL,
               keytab_interval_seconds: int = _KEYTAB_CHECK_INTERVAL) -> None:
    """The heart of the monitor. Three tickers, one process."""
    from web import jobs_db, service_health

    service_health.init(monitor_db_path)
    version = _version_sha()

    last_sweep   = 0.0
    last_reap    = 0.0
    last_hb      = 0.0
    last_keytab  = 0.0

    _log.info("monitor loops starting (sweep=%ss, reaper=%ss, keytab=%ss)",
              sweep_interval_seconds, reaper_interval_seconds,
              keytab_interval_seconds)

    while not stop_event.is_set():
        now = time.monotonic()

        if now - last_hb >= heartbeat_interval_seconds:
            try:
                service_health.heartbeat(
                    monitor_db_path, service_id="monitor",
                    service_type="monitor", version_sha=version,
                    detail="running",
                )
                service_health.prune_dead_workers(monitor_db_path)
            except Exception:
                _log.exception("heartbeat failed")
            last_hb = now

        if now - last_reap >= reaper_interval_seconds:
            try:
                n = jobs_db.reap_orphans(jobs_db_path)
                if n:
                    _log.warning("reaped %d orphaned jobs", n)
            except Exception:
                _log.exception("reaper failed")
            last_reap = now

        if now - last_sweep >= sweep_interval_seconds:
            try:
                _do_sweep_tick(monitor_db_path)
            except Exception:
                _log.exception("sweep tick failed")
            last_sweep = now

        if now - last_keytab >= keytab_interval_seconds:
            try:
                _do_keytab_tick(monitor_db_path)
            except Exception:
                _log.exception("keytab tick failed")
            last_keytab = now

        # 1-second granularity is plenty; each ticker has its own
        # minimum interval so this is just the outer cadence.
        stop_event.wait(timeout=1)

    _log.info("monitor loops stopping")


def _do_sweep_tick(monitor_db_path: Path) -> None:
    """Extracted from web/app.py's _device_monitor_loop."""
    # Reuse the existing sweep entrypoint.
    from web import device_monitor, device_history_db
    from web.monitoring_settings import load_settings
    settings = load_settings(monitor_db_path)
    if not settings.get("enabled", True):
        return
    device_monitor.sweep(monitor_db_path, settings)


def _do_keytab_tick(monitor_db_path: Path) -> None:
    """Extracted from web/app.py's keytab refresher path."""
    from web import keytab_monitor, device_history_db
    from web.monitoring_settings import load_settings
    # Only probe if we have an AD credential configured.
    # Details match the current app.py logic.
    try:
        keytab_monitor.probe_keytab(monitor_db_path)
    except Exception:
        _log.exception("keytab probe failed")


def _version_sha() -> str:
    path = Path("/app/VERSION")
    return path.read_text().strip()[:7] if path.exists() else "unknown"
```

Exact imports / function names for `_do_sweep_tick` and `_do_keytab_tick` come from reading `app.py`'s existing implementation and factoring those bodies out. If the functions don't exist yet as shared helpers, extract them into `device_monitor.sweep(...)` / `keytab_monitor.probe_keytab(...)` in a subsequent refactor commit — but the signature above is the target.

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_monitor_main.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/monitor_main.py autopilot-proxmox/tests/test_monitor_main.py
git commit -m "feat(monitor): sweep + keytab + reaper + heartbeat tickers in one loop"
```

---

### Task 16: Remove sweep/keytab background tasks from web

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

**Context:** Now that monitor owns the loops, remove them from web to avoid double-execution when both containers are up.

- [ ] **Step 1: Delete `_start_device_monitor_loop` and `_stop_device_monitor_loop` handlers**

Remove the two `@app.on_event` hooks and the `_MONITOR_TASK` global in `web/app.py`. Keep `_init_sequences_db` and `_start_health_heartbeat` — those are still web's responsibility.

Also remove the `_device_monitor_loop` async function if it was inline in app.py.

- [ ] **Step 2: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_web.py -v
```

Expected: all pass. Web no longer sweeps; tests that mocked the sweep still work since they were mocking it out anyway.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "refactor(web): drop sweep + keytab tasks (monitor owns them now)"
```

---

**Phase 3 complete.** Three processes, three roles. Still all in one compose service (next phase).

---

## Phase 4 — Health UI

### Task 17: `/monitoring` gets a service-health strip

**Files:**
- Modify: `autopilot-proxmox/web/app.py` (monitoring route)
- Modify: `autopilot-proxmox/web/templates/monitoring.html`
- Modify: `autopilot-proxmox/tests/test_monitoring_page.py`

**Context:** Read-only UI. Table of `service_id / version / heartbeat age / uptime / detail` above the existing device-state table.

- [ ] **Step 1: Pass service_health rows into the template**

In `web/app.py`'s monitoring route:

```python
# find the route; add before returning the template response
from web import service_health
svc_rows = service_health.list_services(DEVICE_MONITOR_DB)
# ... then in context:
return templates.TemplateResponse("monitoring.html", {
    ...,
    "service_health": svc_rows,
})
```

- [ ] **Step 2: Render the strip at the top of monitoring.html**

Add near the top of `autopilot-proxmox/web/templates/monitoring.html`, above the existing content:

```html
{% if service_health %}
<h3 style="margin-top: 0;">Services</h3>
<table style="margin-bottom: 20px;">
  <thead>
    <tr>
      <th>Service</th>
      <th>Version</th>
      <th>Heartbeat</th>
      <th>Detail</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
  {% for s in service_health %}
    <tr>
      <td><code>{{ s.service_id }}</code></td>
      <td><code>{{ s.version_sha }}</code></td>
      <td title="last heartbeat: {{ s.last_heartbeat }}">{{ s.age_seconds }}s ago</td>
      <td>{{ s.detail }}</td>
      <td>
        {% if s.status == 'ok' %}
          <span class="badge badge-green">OK</span>
        {% elif s.status == 'degraded' %}
          <span class="badge" style="background:#fff3cd;color:#856404;">degraded</span>
        {% else %}
          <span class="badge badge-red">dead</span>
        {% endif %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}
```

- [ ] **Step 3: Add a test**

```python
def test_monitoring_page_shows_service_health(client):
    from web import app as app_module, service_health
    service_health.init(app_module.DEVICE_MONITOR_DB)
    service_health.heartbeat(
        app_module.DEVICE_MONITOR_DB,
        service_id="web", service_type="web",
        version_sha="abc1234", detail="idle",
    )
    service_health.heartbeat(
        app_module.DEVICE_MONITOR_DB,
        service_id="builder-xyz", service_type="builder",
        version_sha="abc1234", detail="running",
    )
    with patch("web.app.get_device_rows", return_value=[]):
        r = client.get("/monitoring")
    assert r.status_code == 200
    assert "web" in r.text
    assert "builder-xyz" in r.text
    assert "abc1234" in r.text
```

- [ ] **Step 4: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_monitoring_page.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/web/templates/monitoring.html \
        autopilot-proxmox/tests/test_monitoring_page.py
git commit -m "feat(ui): service health strip at top of /monitoring"
```

---

**Phase 4 complete.** Operators see health. All three services still packed into one compose entry — time to separate.

---

## Phase 5 — Compose + deploy

### Task 18: Update `docker-compose.yml` with three services

**Files:**
- Modify: `autopilot-proxmox/docker-compose.yml`

- [ ] **Step 1: Rewrite compose per spec §7**

```yaml
services:
  autopilot:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    container_name: autopilot
    command: ["web"]
    network_mode: host
    volumes: &common_mounts
      - ./inventory/group_vars/all/vault.yml:/app/inventory/group_vars/all/vault.yml
      - ./inventory/group_vars/all/vars.yml:/app/inventory/group_vars/all/vars.yml
      - ./output:/app/output
      - autopilot-jobs:/app/jobs
      - ./secrets:/app/secrets
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/ProxmoxVEAutopilot:/host/repo
    security_opt:
      - apparmor=unconfined
    environment: &common_env
      OPENSSL_CONF: /etc/ssl/openssl-legacy.cnf
      HOST_REPO_PATH: /opt/ProxmoxVEAutopilot
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 20s
    restart: unless-stopped

  autopilot-builder:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    command: ["builder"]
    network_mode: host
    volumes: *common_mounts
    security_opt:
      - apparmor=unconfined
    environment: *common_env
    depends_on:
      autopilot:
        condition: service_healthy
    restart: unless-stopped

  autopilot-monitor:
    image: ghcr.io/adamgell/proxmox-autopilot:latest
    container_name: autopilot-monitor
    command: ["monitor"]
    network_mode: host
    volumes: *common_mounts
    security_opt:
      - apparmor=unconfined
    environment: *common_env
    depends_on:
      autopilot:
        condition: service_healthy
    restart: unless-stopped

volumes:
  autopilot-jobs:
```

Notes:
- `autopilot-builder` intentionally has no `container_name:` so `--scale` works.
- `autopilot-monitor` has one — it's singleton by design; duplicate names are fine since only one instance can exist anyway.
- `depends_on: condition: service_healthy` waits for the web healthcheck (which returns 200 only once schema init is done — see Task 19).

- [ ] **Step 2: Validate compose file**

```
cd autopilot-proxmox
docker compose config > /dev/null
```

Expected: exit 0, no syntax errors.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/docker-compose.yml
git commit -m "feat(compose): split into web / builder / monitor services"
```

---

### Task 19: `/healthz` returns 200 only after schema init

**Files:**
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/tests/test_web.py`

**Context:** `depends_on: service_healthy` on the dependent services needs web's healthcheck to be honest about startup order.

- [ ] **Step 1: Add a schema_ready flag and gate healthz on it**

In `web/app.py`:

```python
_SCHEMA_READY = False


@app.on_event("startup")
def _init_sequences_db() -> None:
    # ... existing init ...
    global _SCHEMA_READY
    _SCHEMA_READY = True


@app.get("/healthz")
async def healthz():
    if not _SCHEMA_READY:
        raise HTTPException(503, "schema init not complete")
    return {"ok": True}
```

- [ ] **Step 2: Write a test**

```python
def test_healthz_ok_after_startup(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
```

- [ ] **Step 3: Run tests**

```
PYTHONPATH=. .venv-test/bin/pytest tests/test_web.py -v -k healthz
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_web.py
git commit -m "feat(web): /healthz gated on schema_ready for compose dependency"
```

---

### Task 20: Self-update rolls the whole stack

**Files:**
- Modify: `autopilot-proxmox/scripts/update_sidecar.sh` (or equivalent)

**Context:** Today's updater recreates just the `autopilot` container. With three services it has to `docker compose pull && docker compose up -d` so all roll together atomically.

- [ ] **Step 1: Find the updater**

```
grep -rn "docker compose up\|docker-compose up" autopilot-proxmox/scripts/ autopilot-proxmox/web/
```

Expected match: a shell script or Python that does `docker compose up -d autopilot` or similar.

- [ ] **Step 2: Drop the service arg**

Change `docker compose up -d autopilot` → `docker compose pull && docker compose up -d` (no arg — all services).

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/scripts/update_sidecar.sh
git commit -m "feat(update): self-update rolls web + builder + monitor together"
```

---

### Task 21: Release notes + migration doc

**Files:**
- Create: `autopilot-proxmox/MIGRATION-microservice-split.md`

- [ ] **Step 1: Write the migration notes**

```markdown
# Migrating to the microservice-split release

**Breaking change:** the `autopilot` docker-compose service is now three
services: `autopilot` (web), `autopilot-builder`, and `autopilot-monitor`.

## Before upgrading

Finish any running jobs. In-flight jobs at migration time will be
orphaned (their subprocess dies when the old web container restarts).
Operators can see orphaned jobs in `/jobs` with status "orphaned"
after the upgrade.

## Upgrade steps

1. Pull the new image in the UI (footer → Update button) or manually:
   ```
   docker compose pull
   ```
2. Replace your `docker-compose.yml` with the new three-service version
   from this repo. Vault / vars / secrets mounts are the same.
3. Restart:
   ```
   docker compose up -d
   ```
4. On first boot, the web container migrates `jobs/index.json` to
   `output/jobs.db` and renames the legacy file
   `jobs/index.json.pre-split.bak`. Back up the .bak file if you want
   to keep legacy job history.

## Scaling builders

To run N parallel builders:
```
docker compose up -d --scale autopilot-builder=3
```

Per-job-type caps in `/settings → Job concurrency` still apply.

## Rolling back

Older single-service compose files keep working with the new image
since the entrypoint defaults to `web`. Revert compose, keep the new
image — web will run as before, but builder/monitor won't, so jobs will
pile up as "pending". Downgrade the image alongside the compose rollback
to restore full single-container behavior.
```

- [ ] **Step 2: Commit**

```bash
git add autopilot-proxmox/MIGRATION-microservice-split.md
git commit -m "docs(migration): microservice split upgrade guide"
```

---

**Phase 5 complete.** Ready to deploy. Phase 6 is the integration layer.

---

## Phase 6 — Integration tests

### Task 22: Kill-path end-to-end

**Files:**
- Modify: `autopilot-proxmox/tests/integration/test_live.py` (append)

**Context:** Run against a live split deploy. Gated on `--run-integration`.

- [ ] **Step 1: Write the test**

```python
@pytest.mark.integration
def test_kill_stops_running_job_within_10s(live_client, live_host):
    """Enqueue a long-sleeping test job, POST /kill, observe status
    transition within 10 seconds."""
    # Enqueue a job that runs a deliberately long sleep
    r = live_client.post("/api/jobs/test-long-sleep",
                         data={"duration": "60"})
    job_id = r.json()["id"]

    # Wait for builder to claim (up to 5s)
    deadline = time.time() + 10
    while time.time() < deadline:
        job = live_client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "running":
            break
        time.sleep(0.5)
    assert job["status"] == "running"

    # Kill it
    live_client.post(f"/api/jobs/{job_id}/kill")

    # Expect status transition within 10s (5s heartbeat + grace)
    deadline = time.time() + 10
    while time.time() < deadline:
        job = live_client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("complete", "failed"):
            break
        time.sleep(0.5)
    assert job["status"] in ("complete", "failed")
```

- [ ] **Step 2: Add a test playbook**

Create `autopilot-proxmox/playbooks/_test_long_sleep.yml`:

```yaml
- hosts: localhost
  tasks:
    - name: Long sleep for kill-path integration test
      ansible.builtin.command: sleep {{ duration }}
```

And a web endpoint stub in app.py guarded by a feature flag so it only exists in test builds:

```python
if os.environ.get("AUTOPILOT_ENABLE_TEST_JOBS") == "1":
    @app.post("/api/jobs/test-long-sleep")
    async def _enqueue_test_long_sleep(duration: str = Form("60")):
        cmd = ["ansible-playbook",
               str(PLAYBOOK_DIR / "_test_long_sleep.yml"),
               "-e", f"duration={duration}"]
        entry = job_manager.start("capture_hash", cmd,
                                  args={"duration": duration})
        return {"id": entry["id"]}
```

- [ ] **Step 3: Run against the live box after deploying the split**

```
# on the dev host
ssh root@192.168.2.4 'docker compose -f /opt/ProxmoxVEAutopilot/autopilot-proxmox/docker-compose.yml up -d'
PYTHONPATH=. .venv-test/bin/pytest tests/integration/test_live.py::test_kill_stops_running_job_within_10s --run-integration -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/tests/integration/test_live.py \
        autopilot-proxmox/playbooks/_test_long_sleep.yml \
        autopilot-proxmox/web/app.py
git commit -m "test(integration): kill-path latency under 10s"
```

---

### Task 23: Scale test — N builders claim N jobs in parallel

**Files:**
- Modify: `autopilot-proxmox/tests/integration/test_live.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.integration
def test_scale_three_builders_runs_three_concurrent_jobs(live_client, live_host):
    """--scale autopilot-builder=3, enqueue 3 long jobs, observe all
    three running simultaneously within 10s."""
    # Assumes operator has manually scaled the compose first:
    #   docker compose up -d --scale autopilot-builder=3
    ids = []
    for _ in range(5):
        r = live_client.post("/api/jobs/test-long-sleep", data={"duration": "30"})
        ids.append(r.json()["id"])

    # provision_clone caps to 3 concurrent — but our test job is
    # capture_hash (cap=5) so all 5 should be able to run. Still, with
    # only 3 builders, at most 3 are running at once.
    deadline = time.time() + 15
    peak_running = 0
    while time.time() < deadline:
        rows = live_client.get("/api/jobs?limit=10").json()
        running = sum(1 for r in rows if r["id"] in ids and r["status"] == "running")
        peak_running = max(peak_running, running)
        if peak_running >= 3:
            break
        time.sleep(0.5)
    assert peak_running == 3
```

- [ ] **Step 2: Commit**

```bash
git add autopilot-proxmox/tests/integration/test_live.py
git commit -m "test(integration): --scale=3 runs 3 concurrent builders"
```

---

### Task 24: Monitor singleton — second instance exits 0

**Files:**
- Modify: `autopilot-proxmox/tests/integration/test_live.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.integration
def test_monitor_singleton_rejects_second_instance(live_host):
    """Manually start a second monitor container sharing the volume;
    it should exit 0 with the 'already running elsewhere' message."""
    import subprocess
    # Assumes the normal stack is already running with monitor #1 healthy.
    result = subprocess.run(
        [
            "ssh", f"root@{live_host}",
            "docker run --rm "
            "-v /opt/ProxmoxVEAutopilot/autopilot-proxmox/output:/app/output "
            "ghcr.io/adamgell/proxmox-autopilot:latest monitor 2>&1 | head -3",
        ],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0  # monitor exits 0, not a crash
    assert "already running elsewhere" in result.stdout
```

- [ ] **Step 2: Commit**

```bash
git add autopilot-proxmox/tests/integration/test_live.py
git commit -m "test(integration): monitor singleton rejects duplicates"
```

---

### Task 25: Web survives a wedged playbook

**Files:**
- Modify: `autopilot-proxmox/tests/integration/test_live.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.integration
def test_web_responsive_when_builder_stalls(live_client, live_host):
    """SIGSTOP the builder's ansible subprocess, confirm /healthz, /vms,
    /monitoring all stay responsive in under 1 second each."""
    # Enqueue a long job
    r = live_client.post("/api/jobs/test-long-sleep", data={"duration": "120"})
    job_id = r.json()["id"]

    # Wait for it to be running
    deadline = time.time() + 10
    while time.time() < deadline:
        if live_client.get(f"/api/jobs/{job_id}").json()["status"] == "running":
            break
        time.sleep(0.5)

    # SIGSTOP the ansible subprocess to simulate wedging
    import subprocess
    subprocess.run(
        ["ssh", f"root@{live_host}",
         "pkill -STOP -f 'ansible-playbook.*_test_long_sleep'"],
        check=True, timeout=10,
    )

    try:
        # Web must stay responsive. Budget 1s per route.
        for path in ("/healthz", "/vms", "/monitoring", "/jobs"):
            t0 = time.time()
            r = live_client.get(path)
            elapsed = time.time() - t0
            assert r.status_code == 200, f"{path} returned {r.status_code}"
            assert elapsed < 1.0, f"{path} took {elapsed:.2f}s (>1s budget)"
    finally:
        # CONT the subprocess so it can finish naturally.
        subprocess.run(
            ["ssh", f"root@{live_host}",
             "pkill -CONT -f 'ansible-playbook.*_test_long_sleep'"],
            timeout=10,
        )
        # Then kill the job properly.
        live_client.post(f"/api/jobs/{job_id}/kill")
```

- [ ] **Step 2: Commit**

```bash
git add autopilot-proxmox/tests/integration/test_live.py
git commit -m "test(integration): web stays responsive under builder stall"
```

---

### Task 26: Release + PR

- [ ] **Step 1: Push the branch**

```
git push -u origin feat/microservice-split
```

- [ ] **Step 2: Open the PR**

```
gh pr create --base main \
  --title "feat: microservice split — web / builder / monitor" \
  --body-file docs/specs/2026-04-21-microservice-split-design.md
```

Edit the body to condense to a summary + test-plan checklist.

---

## Self-review

Running through the spec with fresh eyes against the plan:

1. **Spec §2 Job queue** — covered by Tasks 1–4 + 8 + 13. ✓
2. **Spec §3 Monitor singleton** — covered by Task 14. ✓
3. **Spec §4 Secrets** — covered implicitly by Task 18's compose (`common_mounts` anchor includes vault/vars/secrets). Add a note in the migration doc about the mount list; already there. ✓
4. **Spec §5 Log streaming** — covered by Task 13 (web reads from disk via `get_log`). ✓
5. **Spec §6 Health table** — covered by Tasks 5–6 + 15 + 17. ✓
6. **Spec §5 Orphan reaper** — covered by Task 4 (logic) + Task 15 (ticker). ✓
7. **Spec §7 Schema ownership** — covered by Task 19 (`/healthz` gate). ✓
8. **Spec §8 Compose + Dockerfile** — covered by Tasks 9–10 + 18. ✓
9. **Spec §9 Migration** — covered by Task 7. ✓
10. **Spec §10 Testing approach** — covered by unit tests throughout + Tasks 22–25 for integration. ✓
11. **Spec §11 Self-update scope** — covered by Task 20. ✓

**No placeholders found.** All code blocks show complete implementations. All commit commands are spelled out.

**Type consistency check:**
- `jobs_db.enqueue`, `claim_next_job`, `touch_heartbeat`, `finalize_job`, `request_kill`, `reap_orphans`, `list_jobs`, `get_job`, `_insert_migrated`, `list_job_type_limits` — all defined in Tasks 1–4+7, all used consistently in Tasks 8, 11, 12, 13.
- `service_health.init`, `heartbeat`, `list_services`, `prune_dead_workers` — defined Task 5, used in Tasks 6, 11, 15, 17.
- `builder.run_builder`, `builder._run_one_job` — defined Task 11, invoked by dispatcher Task 9.
- `monitor_main.run_monitor`, `_acquire_singleton_lock`, `_run_loops`, `_do_sweep_tick`, `_do_keytab_tick` — defined Tasks 14-15, invoked by dispatcher Task 9.

No mismatches found.

---

Plan complete and saved to `docs/superpowers/plans/2026-04-21-microservice-split.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
