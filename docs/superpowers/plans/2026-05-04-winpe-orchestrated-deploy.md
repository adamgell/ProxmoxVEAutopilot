# WinPE-Orchestrated Proxmox Deploy Implementation Plan

Version: v2.4 (2026-05-04, fifth review pass)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a blank-disk WinPE provisioning path to ProxmoxVEAutopilot that boots a Proxmox VM into a custom WinPE image, partitions and applies a stock Windows install.wim, injects boot-critical drivers, stages the Autopilot configuration JSON offline, then hands off to Specialize/OOBE/FirstLogon for the existing FLC sequence to run unchanged. Hash capture stays in OOBE FLC for M1; pre-OS hash capture in WinPE is M2.

**Architecture:** Reuses the existing `proxmox_vm_clone` role (with a new `_skip_panther_injection` flag) cloning from a new empty `winpe_blank_template_vmid`. A new in-WinPE PowerShell agent phones home to a new `/winpe/*` Flask endpoint family that serves a per-run action list (compiled by a new `compile_winpe()`), an Autopilot config payload, and the post-WinPE unattend XML. Phase 0 actions: partition_disk, apply_wim, inject_drivers (from VirtIO ISO), validate_boot_drivers, stage_autopilot_config (when autopilot_enabled), bake_boot_entry, stage_unattend.

**Tech Stack:** Python 3.13 + FastAPI, SQLite, Jinja2, PowerShell 5.1 (WinPE-bundled), Pester 5, Ansible, Proxmox REST API, Windows ADK + DISM.

**Reference spec:** `docs/superpowers/specs/2026-05-04-winpe-orchestrated-proxmox-deploy-design.md`

---

## File Structure

**New files:**

| Path | Purpose |
|---|---|
| `autopilot-proxmox/files/autounattend.post_winpe.xml.j2` | Unattend template without windowsPE pass |
| `autopilot-proxmox/web/winpe_token.py` | HMAC bearer token signer/verifier |
| `autopilot-proxmox/web/winpe_endpoints.py` | FastAPI router mounted at `/winpe/*` |
| `autopilot-proxmox/web/templates/run_detail.html` | `/runs/<id>` timeline page |
| `autopilot-proxmox/playbooks/_provision_proxmox_winpe_vm.yml` | Inner playbook |
| `autopilot-proxmox/playbooks/provision_proxmox_winpe.yml` | Top-level wrapper |
| `autopilot-proxmox/roles/common/tasks/wait_for_run_state.yml` | Polling helper |
| `autopilot-proxmox/tests/test_provisioning_runs_db.py` | DB helper tests |
| `autopilot-proxmox/tests/test_sequence_compiler_winpe.py` | `compile_winpe` tests |
| `autopilot-proxmox/tests/test_unattend_renderer_post_winpe.py` | Renderer tests |
| `autopilot-proxmox/tests/test_winpe_token.py` | Token tests |
| `autopilot-proxmox/tests/test_winpe_endpoints.py` | Flask endpoint tests |
| `tools/winpe-build/build-winpe.ps1` | Main build script |
| `tools/winpe-build/Invoke-AutopilotWinPE.ps1` | In-WinPE agent (copied into WIM) |
| `tools/winpe-build/config.json` | Runtime config (copied into WIM) |
| `tools/winpe-build/startnet.cmd` | WinPE boot entry (copied into WIM) |
| `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1` | Pester tests |
| `tools/winpe-build/README.md` | Build prerequisites |
| `docs/WINPE_E2E_RUNBOOK.md` | M1 merge-gate manual runbook |

**Modified:**

| Path | Change |
|---|---|
| `autopilot-proxmox/web/sequences_db.py` | New tables, `hash_capture_phase` column, run helpers |
| `autopilot-proxmox/web/sequence_compiler.py` | Add `compile_winpe()`, `CompiledWinPEPhase` |
| `autopilot-proxmox/web/unattend_renderer.py` | Add `phase_layout` argument |
| `autopilot-proxmox/web/app.py` | Mount `winpe_endpoints` router, provision-page boot-mode toggle, `/runs/<id>` route |
| `autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml` | Gate Panther inject on `_skip_panther_injection` |
| `autopilot-proxmox/inventory/group_vars/all/vars.yml` | Add `winpe_blank_template_vmid`, `proxmox_winpe_iso`, `autopilot_winpe_token_secret` |
| `autopilot-proxmox/inventory/group_vars/all/vault.yml.example` | Add `vault_autopilot_winpe_token_secret` |
| `autopilot-proxmox/web/templates/provision.html` | Boot mode toggle |
| `autopilot-proxmox/web/templates/sequence_edit.html` | `hash_capture_phase` dropdown (M2-enabled) |

---

## Phase A: Schema and run-row helpers

The existing `task_sequences` table grows one column. Two new tables hold the per-run state and step timeline. `vm_provisioning` is unchanged for back-compat.

### Task A1: Add `hash_capture_phase` column to `task_sequences`

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py` (SCHEMA string + `init()` migration)
- Test: `autopilot-proxmox/tests/test_sequences_db.py`

- [ ] **Step 1: Write the failing test**

Add at the end of `tests/test_sequences_db.py`:

```python
def test_hash_capture_phase_column_exists_after_init(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task_sequences)")}
    assert "hash_capture_phase" in cols


def test_hash_capture_phase_default_is_oobe(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    sequences_db.create_sequence(
        db_path, name="t", description="", target_os="windows",
        produces_autopilot_hash=False, is_default=False,
    )
    seqs = sequences_db.list_sequences(db_path)
    assert seqs[0]["hash_capture_phase"] == "oobe"


def test_hash_capture_phase_migration_idempotent(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    sequences_db.init(db_path)  # must not raise on existing column
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequences_db.py::test_hash_capture_phase_column_exists_after_init tests/test_sequences_db.py::test_hash_capture_phase_default_is_oobe tests/test_sequences_db.py::test_hash_capture_phase_migration_idempotent -v
```

Expected: 3 FAIL (column not in schema; `_row_to_sequence` doesn't return field).

- [ ] **Step 3: Add column to SCHEMA and migration in init()**

In `web/sequences_db.py`, find the `task_sequences` `CREATE TABLE` in the `SCHEMA` string and add a column after `target_os`:

```python
CREATE TABLE IF NOT EXISTS task_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    produces_autopilot_hash INTEGER NOT NULL DEFAULT 0,
    target_os TEXT NOT NULL DEFAULT 'windows',
    hash_capture_phase TEXT NOT NULL DEFAULT 'oobe' CHECK (hash_capture_phase IN ('winpe','oobe')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

In `init()`, after the existing `target_os` migration block, add the same idempotent ALTER for `hash_capture_phase`:

```python
        # --- Migration: add hash_capture_phase column (pre-existing DBs)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(task_sequences)")}
        if "hash_capture_phase" not in cols:
            conn.execute(
                "ALTER TABLE task_sequences "
                "ADD COLUMN hash_capture_phase TEXT NOT NULL DEFAULT 'oobe' "
                "CHECK (hash_capture_phase IN ('winpe','oobe'))"
            )
```

- [ ] **Step 4: Update `_row_to_sequence` to surface the field**

Find `_row_to_sequence()` (around line 211) and add the field to the returned dict:

```python
        "hash_capture_phase": row["hash_capture_phase"],
```

Insert it next to `"target_os"` so dict ordering is consistent.

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequences_db.py -v
```

Expected: all sequences-db tests PASS, including the three new ones.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequences_db.py
git commit -m "feat(db): add hash_capture_phase column to task_sequences"
```

### Task A2: Add `provisioning_runs` and `provisioning_run_steps` tables

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py` (SCHEMA string)
- Test: `autopilot-proxmox/tests/test_provisioning_runs_db.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provisioning_runs_db.py`:

```python
"""Tests for provisioning_runs + provisioning_run_steps schema."""
import sqlite3
import pytest


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sequences.db"


def test_init_creates_provisioning_runs_table(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "provisioning_runs" in tables
    assert "provisioning_run_steps" in tables


def test_provisioning_runs_columns(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {r[1]: r for r in conn.execute(
            "PRAGMA table_info(provisioning_runs)"
        )}
    assert "id" in cols and "vmid" in cols and "vm_uuid" in cols
    assert "provision_path" in cols and "state" in cols
    # vmid must be NULLABLE because Ansible owns /cluster/nextid allocation
    assert cols["vmid"][3] == 0, "vmid must be NULL-able"


def test_provision_path_check_constraint(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        # Need a sequence to satisfy FK
        conn.execute(
            "INSERT INTO task_sequences (name,description,created_at,updated_at)"
            " VALUES ('x','',datetime('now'),datetime('now'))"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO provisioning_runs "
                "(sequence_id, provision_path, state, started_at) "
                "VALUES (1, 'pxe', 'queued', datetime('now'))"
            )


def test_provisioning_run_steps_cascade_delete(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO task_sequences (name,description,created_at,updated_at)"
            " VALUES ('x','',datetime('now'),datetime('now'))"
        )
        conn.execute(
            "INSERT INTO provisioning_runs "
            "(sequence_id, provision_path, state, started_at) "
            "VALUES (1, 'winpe', 'queued', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO provisioning_run_steps "
            "(run_id, order_index, phase, kind, state) "
            "VALUES (1, 0, 'winpe', 'apply_wim', 'pending')"
        )
        conn.execute("DELETE FROM provisioning_runs WHERE id=1")
        n = conn.execute(
            "SELECT COUNT(*) FROM provisioning_run_steps"
        ).fetchone()[0]
    assert n == 0


def test_uuid_state_index_present(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    with sqlite3.connect(db_path) as conn:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
    assert "idx_provisioning_runs_vm_uuid_state" in idx
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_provisioning_runs_db.py -v
```

Expected: 5 FAIL ("no such table: provisioning_runs").

- [ ] **Step 3: Append the schema**

Append to the `SCHEMA` string in `web/sequences_db.py`, after the existing `answer_iso_cache` table:

```sql

CREATE TABLE IF NOT EXISTS provisioning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vmid INTEGER,
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id),
    provision_path TEXT NOT NULL CHECK (provision_path IN ('clone','winpe')),
    state TEXT NOT NULL,
    vm_uuid TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_provisioning_runs_vm_uuid_state
    ON provisioning_runs(vm_uuid, state);

CREATE TABLE IF NOT EXISTS provisioning_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES provisioning_runs(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    kind TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    UNIQUE (run_id, order_index)
);
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_provisioning_runs_db.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_provisioning_runs_db.py
git commit -m "feat(db): add provisioning_runs and provisioning_run_steps tables"
```

### Task A3: Add run + step helper functions

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`
- Test: `autopilot-proxmox/tests/test_provisioning_runs_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provisioning_runs_db.py`:

```python
def test_create_run_returns_id_with_null_vmid(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    assert run_id == 1
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["vmid"] is None
    assert run["vm_uuid"] is None
    assert run["state"] == "queued"
    assert run["provision_path"] == "winpe"


def test_set_run_identity_populates_vmid_and_uuid(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1234,
        vm_uuid="00000000-0000-0000-0000-aabbccddeeff",
    )
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["vmid"] == 1234
    assert run["vm_uuid"] == "00000000-0000-0000-0000-aabbccddeeff"
    assert run["state"] == "awaiting_winpe"


def test_get_run_by_uuid_state_finds_match(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1234, vm_uuid="abc",
    )
    found = sequences_db.find_run_by_uuid_state(
        db_path, vm_uuid="abc", state="awaiting_winpe",
    )
    assert found["id"] == run_id


def test_get_run_by_uuid_state_returns_none_when_state_wrong(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1234, vm_uuid="abc",
    )
    found = sequences_db.find_run_by_uuid_state(
        db_path, vm_uuid="abc", state="firstlogon",
    )
    assert found is None


def test_append_step_assigns_order_index_and_pending_state(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    s1 = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="partition_disk",
        params={"layout": "default"},
    )
    s2 = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    assert s1["order_index"] == 0
    assert s2["order_index"] == 1
    assert s1["state"] == "pending"
    assert s1["params_json"] == '{"layout": "default"}'


def test_update_step_state_records_timestamps(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="s", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    s = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="running",
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="ok",
    )
    steps = sequences_db.list_run_steps(db_path, run_id=run_id)
    assert steps[0]["state"] == "ok"
    assert steps[0]["started_at"] is not None
    assert steps[0]["finished_at"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_provisioning_runs_db.py -v
```

Expected: 6 new FAILs ("module 'web.sequences_db' has no attribute 'create_provisioning_run'").

- [ ] **Step 3: Implement the helpers**

Append to `web/sequences_db.py` after the existing helpers (use `_now()` and the existing `_connect` context manager pattern):

```python
def create_provisioning_run(db_path, *,
                            sequence_id: int,
                            provision_path: str) -> int:
    if provision_path not in ("clone", "winpe"):
        raise ValueError(f"invalid provision_path: {provision_path!r}")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO provisioning_runs "
            "(sequence_id, provision_path, state, started_at) "
            "VALUES (?, ?, 'queued', ?)",
            (sequence_id, provision_path, _now()),
        )
        return cur.lastrowid


def get_provisioning_run(db_path, run_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_runs WHERE id=?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def _normalize_uuid(vm_uuid: str) -> str:
    """Canonical UUID form for DB writes and lookups: lowercase, stripped.
    The Ansible role's _vm_identity.uuid is upper-case; WMI in WinPE
    typically returns lower-case but is documented as case-insensitive.
    Persist one form so an exact-match WHERE clause can be used."""
    return (vm_uuid or "").strip().lower()


def set_provisioning_run_identity(db_path, *,
                                  run_id: int,
                                  vmid: int,
                                  vm_uuid: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE provisioning_runs "
            "SET vmid=?, vm_uuid=?, state='awaiting_winpe' "
            "WHERE id=? AND state='queued'",
            (vmid, _normalize_uuid(vm_uuid), run_id),
        )


def find_run_by_uuid_state(db_path, *,
                           vm_uuid: str,
                           state: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_runs "
            "WHERE vm_uuid=? AND state=? "
            "ORDER BY id DESC LIMIT 1",
            (_normalize_uuid(vm_uuid), state),
        ).fetchone()
    return dict(row) if row else None


def update_provisioning_run_state(db_path, *,
                                  run_id: int,
                                  state: str,
                                  last_error: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        if state in ("done", "failed"):
            conn.execute(
                "UPDATE provisioning_runs "
                "SET state=?, last_error=?, finished_at=? "
                "WHERE id=?",
                (state, last_error, _now(), run_id),
            )
        else:
            conn.execute(
                "UPDATE provisioning_runs "
                "SET state=?, last_error=? WHERE id=?",
                (state, last_error, run_id),
            )


def append_run_step(db_path, *,
                    run_id: int,
                    phase: str,
                    kind: str,
                    params: dict) -> dict:
    with _connect(db_path) as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(order_index)+1, 0) "
            "FROM provisioning_run_steps WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO provisioning_run_steps "
            "(run_id, order_index, phase, kind, params_json, state) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (run_id, nxt, phase, kind, json.dumps(params, sort_keys=True)),
        )
        sid = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM provisioning_run_steps WHERE id=?", (sid,)
        ).fetchone()
        return dict(row)


def update_run_step_state(db_path, *,
                          step_id: int,
                          state: str,
                          error: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        if state == "running":
            conn.execute(
                "UPDATE provisioning_run_steps "
                "SET state='running', started_at=COALESCE(started_at, ?) "
                "WHERE id=?",
                (_now(), step_id),
            )
        elif state in ("ok", "error"):
            conn.execute(
                "UPDATE provisioning_run_steps "
                "SET state=?, finished_at=?, error=? WHERE id=?",
                (state, _now(), error, step_id),
            )
        else:
            raise ValueError(f"invalid step state: {state!r}")


def list_run_steps(db_path, run_id: int) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM provisioning_run_steps "
            "WHERE run_id=? ORDER BY order_index",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_step(db_path, step_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_run_steps WHERE id=?", (step_id,)
        ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_provisioning_runs_db.py -v
```

Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_provisioning_runs_db.py
git commit -m "feat(db): add provisioning run + step helper functions"
```

### Task A4: Stale-run reaper (request-time, no background task)

If the WinPE agent crashes mid-`apply_wim` or the controller-side Ansible job dies, the run stays in `awaiting_winpe` forever and the latest step stays in `running` forever. Operators see the timeline freeze with no error. We avoid a background reaper thread (extra moving part, deployment shape change) and instead sweep stale runs whenever someone reads run state via `/api/runs/<id>` or registers via `/winpe/register`. A sweep walks runs in active states (`awaiting_winpe`, `awaiting_specialize`) and flips any whose newest step has been `running` for longer than the configured TTL to `failed`, with `last_error="stale; no step update for >N min"`.

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`
- Modify: `autopilot-proxmox/tests/test_provisioning_runs_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provisioning_runs_db.py`:

```python
def test_sweep_stale_runs_marks_run_failed_after_ttl(db_path, monkeypatch):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="x", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1, vm_uuid="u",
    )
    s = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="running",
    )
    # Force the step's started_at into the distant past so it looks stale.
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE provisioning_run_steps "
            "SET started_at = '2000-01-01T00:00:00+00:00' WHERE id=?",
            (s["id"],),
        )

    n = sequences_db.sweep_stale_runs(db_path, ttl_seconds=600)
    assert n == 1
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["state"] == "failed"
    assert "stale" in (run["last_error"] or "")


def test_sweep_stale_runs_leaves_active_runs_alone(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="y", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1, vm_uuid="u",
    )
    s = sequences_db.append_run_step(
        db_path, run_id=run_id, phase="winpe", kind="apply_wim", params={},
    )
    sequences_db.update_run_step_state(
        db_path, step_id=s["id"], state="running",
    )
    n = sequences_db.sweep_stale_runs(db_path, ttl_seconds=3600)
    assert n == 0
    run = sequences_db.get_provisioning_run(db_path, run_id)
    assert run["state"] == "awaiting_winpe"


def test_sweep_stale_runs_skips_runs_in_terminal_state(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="z", description="",
        target_os="windows", produces_autopilot_hash=False, is_default=False,
    )
    run_id = sequences_db.create_provisioning_run(
        db_path, sequence_id=seq_id, provision_path="winpe",
    )
    sequences_db.set_provisioning_run_identity(
        db_path, run_id=run_id, vmid=1, vm_uuid="u",
    )
    sequences_db.update_provisioning_run_state(
        db_path, run_id=run_id, state="done",
    )
    n = sequences_db.sweep_stale_runs(db_path, ttl_seconds=1)
    assert n == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_provisioning_runs_db.py -v -k stale
```

Expected: 3 FAIL ("module 'web.sequences_db' has no attribute 'sweep_stale_runs'").

- [ ] **Step 3: Implement `sweep_stale_runs`**

Append to `web/sequences_db.py`:

```python
def sweep_stale_runs(db_path, *, ttl_seconds: int = 1800) -> int:
    """Mark any run whose newest step has been 'running' for longer
    than ttl_seconds as failed. Returns the count of runs flipped.

    Called inline by /api/runs/<id> reads and /winpe/register so we
    do not need a background reaper. ttl_seconds = 30 min by default
    (covers a slow apply_wim + driver inject on cold storage; tighten
    if your hardware allows).
    """
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    flipped = 0
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT r.id AS run_id, MAX(s.started_at) AS last_started "
            "FROM provisioning_runs r "
            "JOIN provisioning_run_steps s ON s.run_id = r.id "
            "WHERE r.state IN ('awaiting_winpe','awaiting_specialize') "
            "  AND s.state = 'running' "
            "GROUP BY r.id "
            "HAVING last_started IS NOT NULL AND last_started < ?",
            (cutoff_iso,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE provisioning_runs "
                "SET state='failed', last_error=?, finished_at=? "
                "WHERE id=? AND state IN ('awaiting_winpe','awaiting_specialize')",
                (
                    f"stale; no step update for >{ttl_seconds//60} min",
                    _now(), row["run_id"],
                ),
            )
            conn.execute(
                "UPDATE provisioning_run_steps "
                "SET state='error', finished_at=?, error='stale: agent silent' "
                "WHERE run_id=? AND state='running'",
                (_now(), row["run_id"]),
            )
            flipped += 1
    return flipped
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_provisioning_runs_db.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_provisioning_runs_db.py
git commit -m "feat(db): sweep_stale_runs marks long-silent agents failed"
```

---

## Phase B: Compiler and renderer

`compile_winpe()` is a peer to the existing `compile()`. It always emits the canonical phase-0 action list when called for a WinPE run, regardless of operator-authored steps (those still flow through `compile()` and end up as FLC). The post-WinPE unattend template is a fork of `autounattend.xml.j2` with the `windowsPE` settings block deleted.

### Task B1: Add `CompiledWinPEPhase` dataclass

**Files:**
- Modify: `autopilot-proxmox/web/sequence_compiler.py`
- Test: `autopilot-proxmox/tests/test_sequence_compiler_winpe.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sequence_compiler_winpe.py`:

```python
"""Tests for compile_winpe and CompiledWinPEPhase."""
import pytest


def test_compiled_winpe_phase_default_fields():
    from web.sequence_compiler import CompiledWinPEPhase
    p = CompiledWinPEPhase()
    assert p.actions == []
    assert p.requires_windows_iso is True
    assert p.requires_virtio_iso is True
    assert p.expected_reboot_count == 1
    assert p.autopilot_enabled is False


def test_compiled_winpe_phase_actions_is_independent_per_instance():
    from web.sequence_compiler import CompiledWinPEPhase
    a = CompiledWinPEPhase()
    b = CompiledWinPEPhase()
    a.actions.append({"kind": "x"})
    assert b.actions == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequence_compiler_winpe.py -v
```

Expected: 2 FAIL ("cannot import name 'CompiledWinPEPhase'").

- [ ] **Step 3: Add the dataclass**

In `web/sequence_compiler.py`, after the existing `CompiledSequence` dataclass (around line 53), add:

```python
@dataclass
class CompiledWinPEPhase:
    actions: list = field(default_factory=list)
    requires_windows_iso: bool = True
    requires_virtio_iso: bool = True
    expected_reboot_count: int = 1
    autopilot_enabled: bool = False
    # Note: actual AutopilotConfigurationFile.json bytes are NOT carried
    # on this struct. The /winpe/autopilot-config/<run_id> endpoint reads
    # them from autopilot_config_path at request time (matching what
    # roles/autopilot_inject does today) so updates to the file take
    # effect without recompiling existing runs.
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequence_compiler_winpe.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequence_compiler_winpe.py
git commit -m "feat(compiler): add CompiledWinPEPhase dataclass"
```

### Task B2: Implement `compile_winpe()`

**Files:**
- Modify: `autopilot-proxmox/web/sequence_compiler.py`
- Test: `autopilot-proxmox/tests/test_sequence_compiler_winpe.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sequence_compiler_winpe.py`:

```python
def _seq(name="s", steps=None, autopilot_enabled=False, hash_phase="oobe"):
    """Build a minimal sequence dict matching sequences_db.get_sequence's shape."""
    return {
        "id": 1, "name": name, "description": "",
        "is_default": False, "produces_autopilot_hash": False,
        "target_os": "windows",
        "hash_capture_phase": hash_phase,
        "steps": steps or [],
    }


def test_compile_winpe_baseline_action_order():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    kinds = [a["kind"] for a in p.actions]
    assert kinds == [
        "partition_disk",
        "apply_wim",
        "inject_drivers",
        "validate_boot_drivers",
        "bake_boot_entry",
        "stage_unattend",
    ]


def test_compile_winpe_inserts_stage_autopilot_config_when_enabled():
    from web.sequence_compiler import compile_winpe
    seq = _seq(steps=[{
        "step_type": "autopilot_entra",
        "params_json": "{}",
        "enabled": True, "order_index": 0,
    }])
    p = compile_winpe(seq)
    kinds = [a["kind"] for a in p.actions]
    assert "stage_autopilot_config" in kinds
    # Must come after apply_wim (writes to V:\) but before bake_boot_entry
    assert kinds.index("stage_autopilot_config") > kinds.index("apply_wim")
    assert kinds.index("stage_autopilot_config") < kinds.index("bake_boot_entry")


def test_compile_winpe_omits_stage_autopilot_config_when_not_enabled():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    kinds = [a["kind"] for a in p.actions]
    assert "stage_autopilot_config" not in kinds


def test_compile_winpe_appends_capture_hash_when_phase_winpe():
    from web.sequence_compiler import compile_winpe
    seq = _seq(hash_phase="winpe")
    seq["produces_autopilot_hash"] = True
    p = compile_winpe(seq)
    kinds = [a["kind"] for a in p.actions]
    # capture_hash runs first because it must read SMBIOS before disk is touched
    assert kinds[0] == "capture_hash"


def test_compile_winpe_omits_capture_hash_when_phase_oobe():
    from web.sequence_compiler import compile_winpe
    seq = _seq(hash_phase="oobe")
    seq["produces_autopilot_hash"] = True
    p = compile_winpe(seq)
    kinds = [a["kind"] for a in p.actions]
    assert "capture_hash" not in kinds


def test_compile_winpe_partition_disk_carries_layout_param():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    pd = next(a for a in p.actions if a["kind"] == "partition_disk")
    assert pd["params"]["layout"] == "recovery_before_c"


def test_compile_winpe_inject_drivers_lists_required_infs():
    from web.sequence_compiler import compile_winpe
    p = compile_winpe(_seq())
    inj = next(a for a in p.actions if a["kind"] == "inject_drivers")
    assert set(inj["params"]["required_infs"]) >= {
        "vioscsi.inf", "netkvm.inf", "vioser.inf",
    }


def test_compile_winpe_marks_autopilot_when_enabled():
    """The compiler signals autopilot via the presence of the
    stage_autopilot_config action; the actual JSON bytes are loaded
    by the Flask endpoint from autopilot_config_path at request time
    (not embedded in the compiled phase, since the file may change
    between compile and serve)."""
    from web.sequence_compiler import compile_winpe
    seq = _seq(steps=[{
        "step_type": "autopilot_entra",
        "params_json": "{}",
        "enabled": True, "order_index": 0,
    }])
    p = compile_winpe(seq)
    assert any(a["kind"] == "stage_autopilot_config" for a in p.actions)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequence_compiler_winpe.py -v
```

Expected: 7 new FAILs ("cannot import name 'compile_winpe'").

- [ ] **Step 3: Implement `compile_winpe()`**

Append to `web/sequence_compiler.py`:

```python
# ---------------------------------------------------------------------------
# WinPE phase compiler
# ---------------------------------------------------------------------------

def _sequence_has_autopilot(sequence: dict) -> bool:
    for step in sequence.get("steps", []) or []:
        if not step.get("enabled", True):
            continue
        if step.get("step_type") == "autopilot_entra":
            return True
    return False


def compile_winpe(sequence: dict,
                  resolver: Optional[Callable] = None,
                  ) -> "CompiledWinPEPhase":
    """Compile the phase-0 (WinPE) action list for a sequence.

    Returns the canonical action list every WinPE run executes. The
    ordering is fixed; operator-authored steps do not appear in this
    output (they flow through compile() and become FLC entries).
    """
    out = CompiledWinPEPhase()

    autopilot = _sequence_has_autopilot(sequence)
    capture_hash_in_winpe = (
        bool(sequence.get("produces_autopilot_hash"))
        and sequence.get("hash_capture_phase") == "winpe"
    )

    if capture_hash_in_winpe:
        out.actions.append({"kind": "capture_hash", "params": {}})

    out.actions.append({
        "kind": "partition_disk",
        "params": {"layout": "recovery_before_c"},
    })
    out.actions.append({
        "kind": "apply_wim",
        "params": {"image_index_metadata_name": "Windows 11 Enterprise"},
    })
    out.actions.append({
        "kind": "inject_drivers",
        "params": {
            "required_infs": [
                "vioscsi.inf", "netkvm.inf", "vioser.inf",
                "balloon.inf", "vioinput.inf",
            ],
        },
    })
    out.actions.append({
        "kind": "validate_boot_drivers",
        "params": {
            "required_infs": [
                "vioscsi.inf", "netkvm.inf", "vioser.inf",
            ],
        },
    })

    if autopilot:
        # Phase 0 applies Windows to V:\ (the soon-to-be-C:\ partition).
        # The agent runs from X:\ (WinPE RAM drive) and has no C:\, so
        # we MUST stage to V:\Windows\... here. The OS sees this as
        # C:\Windows\... after first boot when V: is remapped to C:.
        out.actions.append({
            "kind": "stage_autopilot_config",
            "params": {
                "guest_path": (
                    "V:\\Windows\\Provisioning\\Autopilot\\"
                    "AutopilotConfigurationFile.json"
                ),
            },
        })
        out.autopilot_enabled = True

    out.actions.append({"kind": "bake_boot_entry", "params": {}})
    out.actions.append({"kind": "stage_unattend", "params": {}})

    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequence_compiler_winpe.py -v
```

Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequence_compiler.py autopilot-proxmox/tests/test_sequence_compiler_winpe.py
git commit -m "feat(compiler): implement compile_winpe with canonical action list"
```

### Task B3: Create `autounattend.post_winpe.xml.j2`

**Files:**
- Create: `autopilot-proxmox/files/autounattend.post_winpe.xml.j2`
- Test: `autopilot-proxmox/tests/test_unattend_renderer_post_winpe.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_unattend_renderer_post_winpe.py`:

```python
"""Tests for the post_winpe unattend template (no windowsPE pass)."""
from pathlib import Path

import pytest


_TEMPLATE = Path(__file__).resolve().parent.parent / "files" / "autounattend.post_winpe.xml.j2"


def test_template_file_exists():
    assert _TEMPLATE.is_file(), f"missing: {_TEMPLATE}"


def test_template_has_no_windowsPE_pass():
    text = _TEMPLATE.read_text()
    assert 'pass="windowsPE"' not in text, (
        "post_winpe template must not contain the windowsPE settings block"
    )


def test_template_has_specialize_pass():
    text = _TEMPLATE.read_text()
    assert 'pass="specialize"' in text


def test_template_has_oobeSystem_pass():
    text = _TEMPLATE.read_text()
    assert 'pass="oobeSystem"' in text


def test_template_has_no_disk_config_or_image_install():
    """Setup's windowsPE pass owns DiskConfiguration / ImageInstall.
    The post_winpe path bypasses Setup, so neither block must remain.
    Drivers come from phase-0 dism /add-driver against the VirtIO ISO,
    not from a PnpCustomizations pass in the unattend."""
    text = _TEMPLATE.read_text()
    assert "<DiskConfiguration>" not in text
    assert "<ImageInstall>" not in text
    assert "Microsoft-Windows-PnpCustomizationsWinPE" not in text


def test_template_jinja_blocks_match_full_template():
    """post_winpe must accept the same {{ ... }} block names so renderer
    code path is unified."""
    full = (_TEMPLATE.parent / "autounattend.xml.j2").read_text()
    pw = _TEMPLATE.read_text()
    for var in ("oobe_user_accounts", "oobe_auto_logon",
                "specialize_computer_name",
                "specialize_identification_component",
                "extra_first_logon_commands"):
        assert f"{{{{ {var} }}}}" in pw or f"{{{{{var}}}}}" in pw, (
            f"post_winpe template missing placeholder for {var}"
        )
        # same placeholder must exist in the full template (sanity check)
        assert f"{{{{ {var} }}}}" in full or f"{{{{{var}}}}}" in full
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_unattend_renderer_post_winpe.py -v
```

Expected: 6 FAIL (template missing).

- [ ] **Step 3: Create the template**

Copy `files/autounattend.xml.j2` to `files/autounattend.post_winpe.xml.j2`, then DELETE the entire `<settings pass="windowsPE">` block (everything from `<settings pass="windowsPE">` through its closing `</settings>`). Keep all other passes byte-identical to the full template.

```bash
cp autopilot-proxmox/files/autounattend.xml.j2 autopilot-proxmox/files/autounattend.post_winpe.xml.j2
```

Then open the new file and remove the `windowsPE` settings block. Concretely: open the file in your editor, find the line `<settings pass="windowsPE">`, and delete from that line through its matching `</settings>` (the next `<settings pass="...">` is the start of the block to KEEP).

After editing, verify:

```bash
grep -c 'pass="windowsPE"' autopilot-proxmox/files/autounattend.post_winpe.xml.j2
```

Expected: 0.

```bash
grep -c 'pass="specialize"' autopilot-proxmox/files/autounattend.post_winpe.xml.j2
grep -c 'pass="oobeSystem"' autopilot-proxmox/files/autounattend.post_winpe.xml.j2
grep -c 'DiskConfiguration\|ImageInstall\|PnpCustomizationsWinPE' autopilot-proxmox/files/autounattend.post_winpe.xml.j2
```

Expected: first two each at least 1; the third must be 0 (those blocks all lived inside the deleted windowsPE pass).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_unattend_renderer_post_winpe.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/files/autounattend.post_winpe.xml.j2 autopilot-proxmox/tests/test_unattend_renderer_post_winpe.py
git commit -m "feat(unattend): add post_winpe template (no windowsPE pass)"
```

### Task B4: Add `phase_layout` argument to `render_unattend`

**Files:**
- Modify: `autopilot-proxmox/web/unattend_renderer.py`
- Test: `autopilot-proxmox/tests/test_unattend_renderer_post_winpe.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_unattend_renderer_post_winpe.py`:

```python
def test_render_default_phase_layout_is_full(monkeypatch):
    """phase_layout default 'full' must use the existing template path."""
    from web import unattend_renderer
    from web.sequence_compiler import CompiledSequence
    out = unattend_renderer.render_unattend(CompiledSequence())
    assert 'pass="windowsPE"' in out


def test_render_phase_layout_post_winpe_uses_new_template():
    from web import unattend_renderer
    from web.sequence_compiler import CompiledSequence
    out = unattend_renderer.render_unattend(
        CompiledSequence(), phase_layout="post_winpe",
    )
    assert 'pass="windowsPE"' not in out
    assert 'pass="specialize"' in out


def test_render_phase_layout_invalid_raises():
    from web import unattend_renderer
    from web.sequence_compiler import CompiledSequence
    with pytest.raises(ValueError):
        unattend_renderer.render_unattend(
            CompiledSequence(), phase_layout="bogus",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_unattend_renderer_post_winpe.py::test_render_default_phase_layout_is_full tests/test_unattend_renderer_post_winpe.py::test_render_phase_layout_post_winpe_uses_new_template tests/test_unattend_renderer_post_winpe.py::test_render_phase_layout_invalid_raises -v
```

Expected: 3 FAIL ("got an unexpected keyword argument 'phase_layout'").

- [ ] **Step 3: Add `phase_layout` to renderer**

In `web/unattend_renderer.py`, add a constant near the top:

```python
_POST_WINPE_TEMPLATE_PATH = _FILES_DIR / "autounattend.post_winpe.xml.j2"
```

Modify `render_unattend()` signature and body:

```python
def render_unattend(compiled: CompiledSequence,
                    *,
                    template_path: Optional[Path] = None,
                    phase_layout: str = "full") -> str:
    """Render unattend XML bytes for the given compiled sequence.

    phase_layout:
      "full"        -- include the windowsPE pass (default; clone path).
      "post_winpe"  -- omit windowsPE; for the WinPE provisioning path.
    """
    if phase_layout not in ("full", "post_winpe"):
        raise ValueError(f"invalid phase_layout: {phase_layout!r}")
    if template_path is None:
        path = (_POST_WINPE_TEMPLATE_PATH if phase_layout == "post_winpe"
                else _TEMPLATE_PATH)
    else:
        path = template_path
    env = Environment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    template = env.from_string(path.read_text())
    blocks = compiled.unattend_blocks
    return template.render(
        oobe_user_accounts=blocks.get("oobe_user_accounts",
                                      _DEFAULT_USER_ACCOUNTS),
        oobe_auto_logon=blocks.get("oobe_auto_logon",
                                   _DEFAULT_AUTO_LOGON),
        specialize_computer_name=blocks.get("specialize_computer_name", "*"),
        specialize_identification_component=_wrap_identification(
            blocks.get("specialize_identification", "")),
        extra_first_logon_commands=_render_first_logon_extras(
            compiled.first_logon_commands),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_unattend_renderer_post_winpe.py tests/test_answer_iso_encoding.py tests/test_unattend_renderer.py -v
```

Expected: all PASS (existing renderer tests must still pass, default behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/unattend_renderer.py autopilot-proxmox/tests/test_unattend_renderer_post_winpe.py
git commit -m "feat(unattend): add phase_layout arg to render_unattend"
```

---

## Phase C: Token + Flask endpoints

All `/winpe/*` endpoints live in a new module `web/winpe_endpoints.py` (FastAPI APIRouter), mounted in `web/app.py`. Bearer tokens are HMAC-SHA256 signed `{run_id, expires_at}`. The token secret is read from `os.environ["AUTOPILOT_WINPE_TOKEN_SECRET"]` at request time (test-overridable).

### Task C1: Implement `winpe_token` (sign + verify)

**Files:**
- Create: `autopilot-proxmox/web/winpe_token.py`
- Test: `autopilot-proxmox/tests/test_winpe_token.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_winpe_token.py`:

```python
"""Tests for HMAC bearer token sign + verify."""
import time

import pytest


def test_sign_and_verify_round_trip(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    payload = winpe_token.verify(tok)
    assert payload["run_id"] == 42


def test_verify_rejects_expired_token(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=-1)
    with pytest.raises(winpe_token.TokenExpired):
        winpe_token.verify(tok)


def test_verify_rejects_tampered_payload(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    head, sig = tok.rsplit(".", 1)
    # flip a character in the payload
    tampered = head[:-1] + ("A" if head[-1] != "A" else "B") + "." + sig
    with pytest.raises(winpe_token.TokenInvalid):
        winpe_token.verify(tampered)


def test_verify_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "secret-A")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "secret-B")
    with pytest.raises(winpe_token.TokenInvalid):
        winpe_token.verify(tok)


def test_sign_without_secret_raises(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_WINPE_TOKEN_SECRET", raising=False)
    from web import winpe_token
    with pytest.raises(winpe_token.TokenSecretMissing):
        winpe_token.sign(run_id=1, ttl_seconds=60)


def test_token_is_url_safe(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=42, ttl_seconds=60)
    # Acceptable chars: base64url alphabet plus the "." separator
    import re
    assert re.fullmatch(r"[A-Za-z0-9_\-.]+", tok) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_token.py -v
```

Expected: 6 FAIL ("No module named 'web.winpe_token'").

- [ ] **Step 3: Implement the module**

Create `web/winpe_token.py`:

```python
"""HMAC-signed bearer tokens for the WinPE phase-0 agent.

Tokens encode {run_id, expires_at} as base64url(JSON).base64url(HMAC).
The shared secret comes from the AUTOPILOT_WINPE_TOKEN_SECRET env var,
populated by web/app.py from vault_autopilot_winpe_token_secret.

Tokens are stateless: the server does not store them. Verification is
constant-time hmac.compare_digest. Tokens carry only a run_id; per-call
authorization (e.g. step belongs to run) lives in the endpoint code.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import time
from hashlib import sha256


class TokenError(Exception):
    pass


class TokenInvalid(TokenError):
    pass


class TokenExpired(TokenError):
    pass


class TokenSecretMissing(TokenError):
    pass


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _secret() -> bytes:
    s = os.environ.get("AUTOPILOT_WINPE_TOKEN_SECRET")
    if not s:
        raise TokenSecretMissing(
            "AUTOPILOT_WINPE_TOKEN_SECRET is not set; "
            "configure vault_autopilot_winpe_token_secret"
        )
    return s.encode("utf-8")


def sign(*, run_id: int, ttl_seconds: int) -> str:
    payload = {"run_id": run_id, "exp": int(time.time()) + ttl_seconds}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    head = _b64url(raw)
    sig = hmac.new(_secret(), head.encode("ascii"), sha256).digest()
    return head + "." + _b64url(sig)


def verify(token: str) -> dict:
    try:
        head, sig_b64 = token.rsplit(".", 1)
    except ValueError:
        raise TokenInvalid("malformed token")
    expected = hmac.new(_secret(), head.encode("ascii"), sha256).digest()
    try:
        actual = _b64url_decode(sig_b64)
    except Exception:
        raise TokenInvalid("malformed signature")
    if not hmac.compare_digest(expected, actual):
        raise TokenInvalid("signature mismatch")
    try:
        payload = json.loads(_b64url_decode(head))
    except Exception:
        raise TokenInvalid("malformed payload")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenExpired("token expired")
    if "run_id" not in payload:
        raise TokenInvalid("payload missing run_id")
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_token.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_token.py autopilot-proxmox/tests/test_winpe_token.py
git commit -m "feat(winpe): HMAC bearer token sign/verify"
```

### Task C2: Skeleton router + `POST /winpe/run/<id>/identity`

**Files:**
- Create: `autopilot-proxmox/web/winpe_endpoints.py`
- Modify: `autopilot-proxmox/web/app.py` (mount router, IP allowlist env)
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py` (new)
- Modify: `autopilot-proxmox/tests/conftest.py` (set token secret + identity-allowlist for web fixtures)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_winpe_endpoints.py`:

```python
"""Tests for /winpe/* endpoints."""
import pytest


def _create_seq(client, **overrides):
    """Helper: create a sequence via the existing API and return its id.

    The real API uses _StepIn with .params (NOT params_json) and returns
    HTTP 201. Caller may pass `steps` already in {step_type, params,
    enabled} shape; we coerce loose enabled values to bool.

    Names are globally unique per process via uuid; multiple calls in
    the same test against the same web_client must not collide on the
    UNIQUE(name) constraint.
    """
    import uuid as _uuid
    raw_steps = overrides.get("steps", [])
    steps = []
    for s in raw_steps:
        steps.append({
            "step_type": s["step_type"],
            "params": s.get("params") or s.get("params_json") and __import__("json").loads(s["params_json"]) or {},
            "enabled": bool(s.get("enabled", True)),
        })
    body = {
        "name": overrides.get("name", f"wpe-{_uuid.uuid4().hex[:8]}"),
        "description": "",
        "target_os": "windows",
        "produces_autopilot_hash": overrides.get("autopilot", False),
        "is_default": False,
        "steps": steps,
    }
    r = client.post("/api/sequences", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_run(db_path, sequence_id):
    from web import sequences_db
    return sequences_db.create_provisioning_run(
        db_path, sequence_id=sequence_id, provision_path="winpe",
    )


def test_post_identity_sets_vmid_and_uuid(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "abc-1"},
    )
    assert r.status_code == 200, r.text
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["vmid"] == 1234
    assert run["vm_uuid"] == "abc-1"
    assert run["state"] == "awaiting_winpe"


def test_post_identity_rejects_unknown_run(web_client):
    r = web_client.post(
        "/winpe/run/99999/identity",
        json={"vmid": 1234, "vm_uuid": "abc"},
    )
    assert r.status_code == 404


def test_post_identity_is_idempotent_within_state(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r1 = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "abc-1"},
    )
    assert r1.status_code == 200
    r2 = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "abc-1"},
    )
    # Already past 'queued', so identity update is a no-op success
    assert r2.status_code == 200


def test_post_identity_rejects_missing_fields(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r = web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234},
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Update conftest to set the token secret**

In `tests/conftest.py`, find the env-var setup near the top (before `web.app` import) and add:

```python
os.environ.setdefault("AUTOPILOT_WINPE_TOKEN_SECRET", "test-token-secret")
os.environ.setdefault("AUTOPILOT_WINPE_IDENTITY_ALLOWLIST", "testclient,127.0.0.1")
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: 4 FAIL ("404 Not Found" or "No module named 'web.winpe_endpoints'").

- [ ] **Step 4: Create the router**

Create `web/winpe_endpoints.py`:

```python
"""FastAPI router for the WinPE phase-0 agent.

All endpoints prefixed /winpe/. Routes:
  POST /winpe/run/<id>/identity        Ansible writes vmid + vm_uuid post-clone
  POST /winpe/register                 Agent registers, gets actions + token
  GET  /winpe/sequence/<run_id>        Re-fetch action list (idempotent)
  GET  /winpe/autopilot-config/<run_id>  Per-run JSON payload
  GET  /winpe/unattend/<run_id>        Per-run post_winpe unattend XML
  POST /winpe/step/<step_id>/result    Step state telemetry, refreshes token
  POST /winpe/done                     Detach ide2+sata0, mark awaiting_specialize

Token secret: AUTOPILOT_WINPE_TOKEN_SECRET env var.
Identity-endpoint client allowlist: AUTOPILOT_WINPE_IDENTITY_ALLOWLIST
(comma-separated hostnames or IPs; matched against request.client.host).
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from web import sequences_db, winpe_token


router = APIRouter(prefix="/winpe", tags=["winpe"])


class IdentityBody(BaseModel):
    vmid: int
    vm_uuid: str


def _identity_allowlist() -> set[str]:
    raw = os.environ.get("AUTOPILOT_WINPE_IDENTITY_ALLOWLIST", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _check_identity_caller(request: Request) -> None:
    allow = _identity_allowlist()
    if not allow:
        return
    client = request.client.host if request.client else ""
    if client not in allow:
        raise HTTPException(status_code=403, detail="caller not allowed")


def _db_path() -> str:
    from web import app as web_app
    return web_app.SEQUENCES_DB


@router.post("/run/{run_id}/identity")
def post_identity(run_id: int, body: IdentityBody, request: Request):
    _check_identity_caller(request)
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["state"] == "queued":
        sequences_db.set_provisioning_run_identity(
            db, run_id=run_id, vmid=body.vmid, vm_uuid=body.vm_uuid,
        )
    # Already past queued: idempotent no-op (rerun-safe from Ansible)
    return {"ok": True}
```

- [ ] **Step 5: Mount the router**

In `web/app.py`, find where existing routers are imported/included and add (place near the existing router includes):

```python
from web.winpe_endpoints import router as _winpe_router
app.include_router(_winpe_router)
```

(If the module organizes app + routes inline rather than via include_router, place the router include right after the app instance is created.)

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: 4 PASS.

- [ ] **Step 7: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_winpe_endpoints.py autopilot-proxmox/tests/conftest.py
git commit -m "feat(winpe): /winpe router + POST /winpe/run/<id>/identity"
```

### Task C3: `POST /winpe/register`

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_winpe_endpoints.py`:

```python
def test_register_returns_actions_and_token(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa:bb:cc:dd:ee:ff",
              "build_sha": "deadbeef"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["bearer_token"]
    assert isinstance(body["actions"], list)
    kinds = [a["kind"] for a in body["actions"]]
    assert "partition_disk" in kinds


def test_register_persists_steps(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    )
    from web import sequences_db
    steps = sequences_db.list_run_steps(test_db, run_id=run_id)
    kinds = [s["kind"] for s in steps]
    assert kinds[0] == "partition_disk"
    assert all(s["state"] == "pending" for s in steps)


def test_register_matches_uppercase_identity_to_lowercase_register(
    web_client, test_db,
):
    """Ansible's _vm_identity.uuid is uppercase. The agent reads SMBIOS
    via WMI in WinPE and lowercases the result. Both must reach the
    same DB row, so the layer normalizes UUIDs to lowercase on every
    write and lookup."""
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100,
              "vm_uuid": "AABBCCDD-EEFF-0011-2233-445566778899"},
    )
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "aabbccdd-eeff-0011-2233-445566778899",
              "mac": "aa", "build_sha": "x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == run_id


def test_register_returns_404_for_unknown_uuid(web_client):
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "nope", "mac": "aa", "build_sha": "x"},
    )
    assert r.status_code == 404


def test_register_returns_409_when_state_wrong(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    from web import sequences_db
    sequences_db.update_provisioning_run_state(
        test_db, run_id=run_id, state="awaiting_specialize",
    )
    r = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    )
    assert r.status_code == 409


def test_register_token_is_verifiable(web_client, test_db, monkeypatch):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    body = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    ).json()
    from web import winpe_token
    payload = winpe_token.verify(body["bearer_token"])
    assert payload["run_id"] == run_id
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k register
```

Expected: 5 FAIL (404 for /register).

- [ ] **Step 3: Implement /winpe/register**

Append to `web/winpe_endpoints.py`:

```python
class RegisterBody(BaseModel):
    vm_uuid: str
    mac: str
    build_sha: str


_REGISTER_TOKEN_TTL = 60 * 60  # 60 minutes


def _build_actions_for_run(db: str, run_id: int, sequence_id: int) -> list[dict]:
    """Compile WinPE actions, persist as pending steps, return action dicts
    augmented with the assigned step_id."""
    from web import sequence_compiler
    seq = sequences_db.get_sequence(db, sequence_id)
    phase = sequence_compiler.compile_winpe(seq)
    out = []
    for action in phase.actions:
        step = sequences_db.append_run_step(
            db, run_id=run_id, phase="winpe",
            kind=action["kind"], params=action["params"],
        )
        out.append({
            "step_id": step["id"],
            "kind": action["kind"],
            "params": action["params"],
        })
    return out


@router.post("/register")
def post_register(body: RegisterBody):
    db = _db_path()
    run = sequences_db.find_run_by_uuid_state(
        db, vm_uuid=body.vm_uuid, state="awaiting_winpe",
    )
    if run is None:
        # Distinguish 404 (no such uuid at all) from 409 (uuid exists, wrong state)
        with __import__("sqlite3").connect(db) as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT state FROM provisioning_runs WHERE vm_uuid=? "
                "ORDER BY id DESC LIMIT 1", (body.vm_uuid,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no run for vm_uuid")
        raise HTTPException(
            status_code=409,
            detail=f"run state is {row['state']!r}, expected awaiting_winpe",
        )

    # Idempotency: if steps already exist (re-registration), reuse them.
    existing = sequences_db.list_run_steps(db, run_id=run["id"])
    if existing:
        actions = [
            {"step_id": s["id"], "kind": s["kind"],
             "params": __import__("json").loads(s["params_json"])}
            for s in existing
        ]
    else:
        actions = _build_actions_for_run(db, run["id"], run["sequence_id"])

    token = winpe_token.sign(
        run_id=run["id"], ttl_seconds=_REGISTER_TOKEN_TTL,
    )
    return {
        "run_id": run["id"],
        "bearer_token": token,
        "actions": actions,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: 9 PASS (all from C2 + C3).

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): POST /winpe/register builds actions and signs token"
```

### Task C4: `GET /winpe/sequence/<run_id>` (idempotent re-fetch)

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_winpe_endpoints.py`:

```python
def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_sequence_get_returns_same_actions_after_register(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-100"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-100", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/sequence/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert [a["kind"] for a in actions] == [a["kind"] for a in reg["actions"]]
    assert [a["step_id"] for a in actions] == [a["step_id"] for a in reg["actions"]]


def test_sequence_get_rejects_missing_token(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    r = web_client.get(f"/winpe/sequence/{run_id}")
    assert r.status_code == 401


def test_sequence_get_rejects_token_for_wrong_run(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_a = _create_run(test_db, seq_id)
    run_b = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_a}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    web_client.post(
        f"/winpe/run/{run_b}/identity",
        json={"vmid": 101, "vm_uuid": "u-B"},
    )
    reg_a = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/sequence/{run_b}",
        headers=_bearer(reg_a["bearer_token"]),
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k sequence_get
```

Expected: 3 FAIL.

- [ ] **Step 3: Add bearer dependency + endpoint**

Append to `web/winpe_endpoints.py`:

```python
from fastapi import Depends, Header


def _require_bearer_for_run(run_id: int,
                            authorization: Optional[str] = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = winpe_token.verify(token)
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    if int(payload["run_id"]) != int(run_id):
        raise HTTPException(status_code=403, detail="token/run mismatch")
    return int(payload["run_id"])


@router.get("/sequence/{run_id}")
def get_sequence(run_id: int,
                 _: int = Depends(_require_bearer_for_run)):
    db = _db_path()
    steps = sequences_db.list_run_steps(db, run_id=run_id)
    return {
        "run_id": run_id,
        "actions": [
            {"step_id": s["id"], "kind": s["kind"],
             "params": __import__("json").loads(s["params_json"])}
            for s in steps
        ],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): GET /winpe/sequence/<run_id> with bearer auth"
```

### Task C5: `GET /winpe/autopilot-config/<run_id>`

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_autopilot_config_returns_real_file_bytes_when_enabled(
    web_client, test_db, tmp_path, monkeypatch,
):
    """The endpoint must serve the real AutopilotConfigurationFile.json
    that roles/autopilot_inject would otherwise inject via QGA, not a
    compiler-side placeholder. Operator-managed bytes flow unchanged."""
    real = tmp_path / "AutopilotConfigurationFile.json"
    real.write_bytes(
        b'{"CloudAssignedTenantId":"00000000-0000-0000-0000-000000000001",'
        b'"Version":2049}'
    )
    from web import winpe_endpoints
    monkeypatch.setattr(
        winpe_endpoints, "_resolve_autopilot_config_path",
        lambda: real,
    )
    seq_id = _create_seq(web_client, steps=[{
        "step_type": "autopilot_entra",
        "params": {}, "enabled": True, "order_index": 0,
    }], autopilot=True)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/autopilot-config/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.content == real.read_bytes()


def test_autopilot_config_returns_404_when_not_enabled(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/autopilot-config/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k autopilot_config
```

Expected: 2 FAIL.

- [ ] **Step 3: Implement endpoint**

Append to `web/winpe_endpoints.py` (top of the file, with the other imports). `Response` is reused by /winpe/unattend in C6 and /winpe/hash in M2, so we add it once now:

```python
from fastapi.responses import Response
```

Then append the endpoint body:

```python
def _resolve_autopilot_config_path():
    """Resolve the on-disk path to AutopilotConfigurationFile.json.
    Mirrors what roles/autopilot_inject reads from
    `autopilot_config_path` (defaults to
    autopilot-proxmox/files/AutopilotConfigurationFile.json).
    Tests monkeypatch this to point at a fixture."""
    from pathlib import Path
    from web import app as web_app
    cfg = web_app._load_vars()
    p = cfg.get("autopilot_config_path")
    if p:
        return Path(p)
    # Default mirrors inventory/group_vars/all/vars.yml
    base = Path(__file__).resolve().parent.parent
    return base / "files" / "AutopilotConfigurationFile.json"


@router.get("/autopilot-config/{run_id}")
def get_autopilot_config(run_id: int,
                         _: int = Depends(_require_bearer_for_run)):
    from web import sequence_compiler
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    seq = sequences_db.get_sequence(db, run["sequence_id"])
    phase = sequence_compiler.compile_winpe(seq)
    if not phase.autopilot_enabled:
        raise HTTPException(status_code=404, detail="autopilot not enabled")
    path = _resolve_autopilot_config_path()
    if not path.is_file():
        raise HTTPException(
            status_code=500,
            detail=(
                f"autopilot enabled but {path} is missing; "
                "operator must populate AutopilotConfigurationFile.json"
            ),
        )
    return Response(content=path.read_bytes(),
                    media_type="application/json")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): GET /winpe/autopilot-config/<run_id>"
```

### Task C6: `GET /winpe/unattend/<run_id>`

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_unattend_returns_xml_without_windowsPE(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": "u-A"},
    )
    reg = web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-A", "mac": "aa", "build_sha": "x"},
    ).json()
    r = web_client.get(
        f"/winpe/unattend/{run_id}",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml") or \
           r.headers["content-type"].startswith("text/xml")
    assert b'pass="windowsPE"' not in r.content
    assert b'pass="specialize"' in r.content


def test_unattend_returns_404_for_unknown_run(web_client, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_WINPE_TOKEN_SECRET", "test-token-secret")
    from web import winpe_token
    tok = winpe_token.sign(run_id=99999, ttl_seconds=60)
    r = web_client.get(
        "/winpe/unattend/99999",
        headers=_bearer(tok),
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k unattend
```

Expected: 2 FAIL.

- [ ] **Step 3: Implement endpoint**

Append to `web/winpe_endpoints.py` (`Response` is already imported at the top of the file from Task C5):

```python
def _credential_resolver_for_run():
    """Build a credential resolver matching the existing /api/jobs/provision
    pattern. Local-admin / domain-join steps need this to compile."""
    from web import app as web_app
    def _resolve(cid: int):
        rec = sequences_db.get_credential(_db_path(), web_app._cipher(), cid)
        return rec["payload"] if rec else None
    return _resolve


@router.get("/unattend/{run_id}")
def get_unattend(run_id: int,
                 _: int = Depends(_require_bearer_for_run)):
    from web import sequence_compiler, unattend_renderer
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    seq = sequences_db.get_sequence(db, run["sequence_id"])
    try:
        compiled = sequence_compiler.compile(
            seq, resolve_credential=_credential_resolver_for_run(),
        )
    except sequence_compiler.CompilerError as e:
        raise HTTPException(status_code=400, detail=f"compile failed: {e}")
    xml = unattend_renderer.render_unattend(
        compiled, phase_layout="post_winpe",
    )
    return Response(content=xml, media_type="application/xml")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): GET /winpe/unattend/<run_id> serves post_winpe XML"
```

### Task C7: `POST /winpe/step/<step_id>/result` with token refresh

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def _register(client, db, vm_uuid="u-X"):
    seq_id = _create_seq(client)
    run_id = _create_run(db, seq_id)
    client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 100, "vm_uuid": vm_uuid},
    )
    reg = client.post(
        "/winpe/register",
        json={"vm_uuid": vm_uuid, "mac": "aa", "build_sha": "x"},
    ).json()
    return run_id, reg


def test_step_result_running_then_ok_records_state(web_client, test_db):
    run_id, reg = _register(web_client, test_db)
    step_id = reg["actions"][0]["step_id"]
    web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "running"},
        headers=_bearer(reg["bearer_token"]),
    )
    r = web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "ok", "stdout_tail": "done", "elapsed_seconds": 12},
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert "bearer_token" in body
    from web import sequences_db
    s = sequences_db.get_run_step(test_db, step_id)
    assert s["state"] == "ok"
    assert s["started_at"] is not None
    assert s["finished_at"] is not None


def test_step_result_error_marks_run_failed(web_client, test_db):
    run_id, reg = _register(web_client, test_db)
    step_id = reg["actions"][0]["step_id"]
    web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "error", "error": "disk too small"},
        headers=_bearer(reg["bearer_token"]),
    )
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "failed"
    assert "disk too small" in (run["last_error"] or "")


def test_step_result_token_refresh_is_verifiable(web_client, test_db):
    run_id, reg = _register(web_client, test_db)
    step_id = reg["actions"][0]["step_id"]
    r = web_client.post(
        f"/winpe/step/{step_id}/result",
        json={"state": "running"},
        headers=_bearer(reg["bearer_token"]),
    )
    new_tok = r.json()["bearer_token"]
    from web import winpe_token
    assert winpe_token.verify(new_tok)["run_id"] == run_id


def test_step_result_rejects_step_in_different_run(web_client, test_db):
    run_a, reg_a = _register(web_client, test_db, vm_uuid="u-A")
    run_b, reg_b = _register(web_client, test_db, vm_uuid="u-B")
    step_in_b = reg_b["actions"][0]["step_id"]
    r = web_client.post(
        f"/winpe/step/{step_in_b}/result",
        json={"state": "ok"},
        headers=_bearer(reg_a["bearer_token"]),
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k step_result
```

Expected: 4 FAIL.

- [ ] **Step 3: Implement endpoint**

Append to `web/winpe_endpoints.py`:

```python
class StepResultBody(BaseModel):
    state: str
    error: Optional[str] = None
    stdout_tail: Optional[str] = None
    stderr_tail: Optional[str] = None
    elapsed_seconds: Optional[float] = None


def _require_bearer_token(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    try:
        return winpe_token.verify(
            authorization.removeprefix("Bearer ").strip()
        )
    except winpe_token.TokenExpired:
        raise HTTPException(status_code=401, detail="token expired")
    except winpe_token.TokenError:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/step/{step_id}/result")
def post_step_result(step_id: int, body: StepResultBody,
                     payload: dict = Depends(_require_bearer_token)):
    db = _db_path()
    step = sequences_db.get_run_step(db, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="step not found")
    if int(step["run_id"]) != int(payload["run_id"]):
        raise HTTPException(status_code=403, detail="token/run mismatch")

    if body.state == "running":
        sequences_db.update_run_step_state(
            db, step_id=step_id, state="running",
        )
    elif body.state == "ok":
        sequences_db.update_run_step_state(
            db, step_id=step_id, state="ok",
        )
    elif body.state == "error":
        sequences_db.update_run_step_state(
            db, step_id=step_id, state="error", error=body.error or "",
        )
        sequences_db.update_provisioning_run_state(
            db, run_id=int(step["run_id"]), state="failed",
            last_error=f"step {step['kind']}: {body.error or 'unknown'}",
        )
    else:
        raise HTTPException(status_code=400, detail=f"bad state: {body.state}")

    new_token = winpe_token.sign(
        run_id=int(payload["run_id"]), ttl_seconds=_REGISTER_TOKEN_TTL,
    )
    return {"ok": True, "bearer_token": new_token}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): POST /winpe/step/<id>/result with token refresh"
```

### Task C8: `POST /winpe/done` (detach ide2+sata0, advance state)

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Test: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_done_advances_run_state_and_calls_proxmox(web_client, test_db, monkeypatch):
    calls = []

    def fake_detach(*, vmid, slots, set_boot_order):
        calls.append({"vmid": vmid, "slots": list(slots),
                      "boot": set_boot_order})

    from web import winpe_endpoints
    monkeypatch.setattr(winpe_endpoints, "_proxmox_detach_and_set_boot",
                        fake_detach)
    run_id, reg = _register(web_client, test_db)
    r = web_client.post(
        "/winpe/done",
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "awaiting_specialize"
    assert calls == [{"vmid": 100, "slots": ["ide2", "sata0"],
                      "boot": "order=scsi0"}]


def test_done_is_idempotent(web_client, test_db, monkeypatch):
    from web import winpe_endpoints
    monkeypatch.setattr(winpe_endpoints, "_proxmox_detach_and_set_boot",
                        lambda **kw: None)
    run_id, reg = _register(web_client, test_db)
    web_client.post("/winpe/done", headers=_bearer(reg["bearer_token"]))
    r = web_client.post("/winpe/done", headers=_bearer(reg["bearer_token"]))
    # Already past awaiting_winpe; second call must not error
    assert r.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k done
```

Expected: 2 FAIL.

- [ ] **Step 3: Implement endpoint + Proxmox helper stub**

Append to `web/winpe_endpoints.py`:

```python
def _proxmox_detach_and_set_boot(*, vmid: int, slots: list[str],
                                 set_boot_order: str) -> None:
    """Detach disks and set boot order via Proxmox API.

    Reuses web.app._proxmox_api_put (line 1294), which constructs the
    URL + auth header from primitive vault fields. We deliberately do
    NOT read proxmox_api_base / proxmox_api_auth_header from
    _load_proxmox_config -- those values are Jinja strings in vars.yml
    that _load_vars never renders.

    One PUT carries delete= and boot= together; Proxmox accepts both
    keys in the same form body (same shape as
    roles/cleanup_answer_media.yml uses).
    """
    from web import app as web_app
    cfg = web_app._load_proxmox_config()
    node = cfg.get("proxmox_node") or "pve"
    body = {
        "delete": ",".join(slots),
        "boot": set_boot_order,
    }
    web_app._proxmox_api_put(
        f"/nodes/{node}/qemu/{vmid}/config", data=body,
    )


@router.post("/done")
def post_done(payload: dict = Depends(_require_bearer_token)):
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, int(payload["run_id"]))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["vmid"] is None:
        raise HTTPException(
            status_code=409, detail="run identity not set"
        )
    if run["state"] == "awaiting_winpe":
        _proxmox_detach_and_set_boot(
            vmid=int(run["vmid"]),
            slots=["ide2", "sata0"],
            set_boot_order="order=scsi0",
        )
        sequences_db.update_provisioning_run_state(
            db, run_id=int(run["id"]),
            state="awaiting_specialize",
        )
    return {"ok": True}
```

The pattern `body = {"delete": ",".join(slots), "boot": set_boot_order}` mirrors `cleanup_answer_media.yml` (which uses `body: { delete: "sata0" }` form-urlencoded). One PUT carries both keys; Proxmox accepts the combined body.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): POST /winpe/done detaches ISOs and advances state"
```

---

## Phase D: In-WinPE PowerShell agent

The agent is one file (`Invoke-AutopilotWinPE.ps1`) that runs in WinPE-bundled PowerShell 5.1. Pester 5 tests run on the controller via `pwsh` (PowerShell 7) using a small mock HTTP server (Pwshttp/HttpListener) so we can iterate without bouncing to a Windows VM. WinPE-specific calls (diskpart, dism, bcdboot, wpeutil) are wrapped in script-scope helpers we replace with mocks during tests.

**Pester install (one-time, on the controller Mac):**

```bash
pwsh -NoProfile -Command "Install-Module -Name Pester -RequiredVersion 5.5.0 -Scope CurrentUser -Force"
```

### Task D1: Logger and config bootstrap

**Files:**
- Create: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Create: `tools/winpe-build/config.json`
- Create: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p tools/winpe-build/tests
```

- [ ] **Step 2: Write the failing Pester test**

Create `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`:

```powershell
BeforeAll {
    $script:AgentPath = (Resolve-Path "$PSScriptRoot/../Invoke-AutopilotWinPE.ps1").Path
    . $script:AgentPath
}

Describe 'Read-AgentConfig' {
    It 'returns the parsed config from a JSON file' {
        $tmp = [System.IO.Path]::GetTempFileName()
        '{"flask_base_url": "http://10.0.0.1:5000", "build_sha": "abc"}' |
            Set-Content -Path $tmp -Encoding UTF8
        try {
            $cfg = Read-AgentConfig -Path $tmp
            $cfg.flask_base_url | Should -Be 'http://10.0.0.1:5000'
            $cfg.build_sha | Should -Be 'abc'
        } finally {
            Remove-Item $tmp -Force
        }
    }

    It 'throws on missing file' {
        { Read-AgentConfig -Path '/nonexistent' } | Should -Throw
    }
}

Describe 'Write-AgentLog' {
    It 'appends to the log file with a timestamp prefix' {
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            Write-AgentLog -Path $tmp -Level 'INFO' -Message 'hello'
            $content = Get-Content $tmp
            $content | Should -Match '\[INFO\]'
            $content | Should -Match 'hello'
        } finally {
            Remove-Item $tmp -Force
        }
    }
}
```

- [ ] **Step 3: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: FAIL ("file not found" or "command not recognized").

- [ ] **Step 4: Create the agent skeleton with the two helpers**

Create `tools/winpe-build/Invoke-AutopilotWinPE.ps1`:

```powershell
# Invoke-AutopilotWinPE.ps1
#
# In-WinPE phase-0 agent. Boots a Proxmox VM into Windows by:
#   register -> capture_hash (M2) -> partition_disk -> apply_wim ->
#   inject_drivers -> validate_boot_drivers -> stage_autopilot_config ->
#   bake_boot_entry -> stage_unattend -> done -> reboot.
#
# Designed for PowerShell 5.1 (WinPE-bundled). Sourced by Pester tests
# during development; running it from startnet.cmd at WinPE boot drives
# the live flow.

Set-StrictMode -Version Latest

function Read-AgentConfig {
    param([Parameter(Mandatory)] [string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "config not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-AgentLog {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('DEBUG','INFO','WARN','ERROR')] [string] $Level,
        [Parameter(Mandatory)] [string] $Message
    )
    $ts = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fffK')
    $line = "$ts [$Level] $Message"
    Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
    Write-Host $line
}
```

Create `tools/winpe-build/config.json`:

```json
{
  "flask_base_url": "http://192.168.2.4:5000",
  "flask_base_url_fallback": null,
  "build_sha": "DEV"
}
```

`flask_base_url` SHOULD be an IP literal so the agent has zero DNS dependency on first contact (chicken-and-egg: the agent cannot ask `/winpe/sequence/<id>` to learn its DNS server because it cannot reach the orchestrator without one). `flask_base_url_fallback` is optional; when set, `Invoke-OrchestratorRequest` retries it after `flask_base_url` exhausts its own retry budget. Operators with a stable hostname can put the hostname in `flask_base_url` and a static IP fallback here.

**Bootstrap order at WinPE boot** (each step must succeed before the next can run; failure at any step leaves the VM at the WinPE prompt with the log on screen for triage):

1. UEFI loads the WinPE ISO (`ide2`).
2. `wpeinit` runs (winload). At this point WinPE has whatever drivers were baked into `boot.wim` at build time -- per Task E2 that's vioscsi (so the disk is visible) + NetKVM (so a NIC is up) + vioser. **Without these baked, every other step is unreachable.**
3. `startnet.cmd` runs `drvload` against any `X:\autopilot\drivers\*.inf` (currently empty; reserved for ad-hoc driver drops without rebuilding the ISO).
4. `startnet.cmd` launches `Invoke-AutopilotWinPE.ps1`.
5. The agent's `Wait for network` step polls `Test-NetConnection <flask_base_url>:5000` for up to 60s. If `flask_base_url_fallback` is configured, the same poll runs against the fallback after the first URL exhausts.
6. `POST /winpe/register` -> action list arrives -> `inject_drivers` action runs `dism /add-driver` against the attached VirtIO ISO. (Drivers added here are for the Windows OS image at `V:\`, not WinPE itself.)

- [ ] **Step 5: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/config.json tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): logger and config-loader scaffolding"
```

### Task D2: SMBIOS UUID + MAC discovery

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append to `tests/Invoke-AutopilotWinPE.Tests.ps1`:

```powershell
Describe 'Get-VMIdentity' {
    It 'returns uuid and mac from injected resolvers' {
        $uuidResolver = { '11111111-2222-3333-4444-555555555555' }
        $macResolver = { '00:11:22:33:44:55' }
        $id = Get-VMIdentity -UuidResolver $uuidResolver -MacResolver $macResolver
        $id.vm_uuid | Should -Be '11111111-2222-3333-4444-555555555555'
        $id.mac | Should -Be '00:11:22:33:44:55'
    }

    It 'normalizes uuid to lowercase' {
        $uuidResolver = { 'AABBCCDD-EEFF-0011-2233-445566778899' }
        $macResolver = { 'aa:bb:cc:dd:ee:ff' }
        $id = Get-VMIdentity -UuidResolver $uuidResolver -MacResolver $macResolver
        $id.vm_uuid | Should -Be 'aabbccdd-eeff-0011-2233-445566778899'
    }

    It 'throws when resolver returns empty' {
        { Get-VMIdentity -UuidResolver { '' } -MacResolver { 'aa' } } |
            Should -Throw '*UUID*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 3 new FAIL.

- [ ] **Step 3: Implement `Get-VMIdentity`**

Append to `Invoke-AutopilotWinPE.ps1`:

```powershell
function Get-VMIdentity {
    param(
        [scriptblock] $UuidResolver = { (Get-CimInstance Win32_ComputerSystemProduct).UUID },
        [scriptblock] $MacResolver  = {
            (Get-NetAdapter -Physical |
                Where-Object Status -eq 'Up' |
                Sort-Object ifIndex |
                Select-Object -First 1).MacAddress
        }
    )
    $uuid = & $UuidResolver
    $mac  = & $MacResolver
    if ([string]::IsNullOrWhiteSpace($uuid)) { throw "could not read SMBIOS UUID" }
    if ([string]::IsNullOrWhiteSpace($mac))  { throw "could not read MAC address"  }
    return [pscustomobject]@{
        vm_uuid = $uuid.ToString().ToLowerInvariant()
        mac     = $mac.ToString()
    }
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Get-VMIdentity with injectable resolvers"
```

### Task D3: HTTP client wrapper (Invoke-OrchestratorRequest)

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing tests**

Append:

```powershell
Describe 'Invoke-OrchestratorRequest' {
    BeforeAll {
        function global:_MockInvokeRest {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:lastUri = $Uri
            $script:lastMethod = $Method
            $script:lastHeaders = $Headers
            $script:lastBody = $Body
            return [pscustomobject]@{ ok = $true; uri = $Uri }
        }
    }

    It 'sends a POST with bearer header when token provided' {
        $r = Invoke-OrchestratorRequest -BaseUrl 'http://x:5000' `
            -Path '/winpe/register' -Method POST `
            -Body @{ vm_uuid = 'u' } -BearerToken 'tok' `
            -RestInvoker (Get-Item Function:_MockInvokeRest).ScriptBlock
        $script:lastUri | Should -Be 'http://x:5000/winpe/register'
        $script:lastMethod | Should -Be 'POST'
        $script:lastHeaders.Authorization | Should -Be 'Bearer tok'
        $r.ok | Should -BeTrue
    }

    It 'omits bearer header when token is null' {
        $r = Invoke-OrchestratorRequest -BaseUrl 'http://x:5000' `
            -Path '/winpe/register' -Method POST -Body @{} `
            -RestInvoker (Get-Item Function:_MockInvokeRest).ScriptBlock
        $script:lastHeaders.ContainsKey('Authorization') | Should -BeFalse
    }

    It 'falls back to FallbackBaseUrl when BaseUrl exhausts retries' {
        $script:visited = @()
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:visited += $Uri
            if ($Uri -match '^http://primary') { throw 'connection refused' }
            return [pscustomobject]@{ ok = $true; uri = $Uri }
        }
        $r = Invoke-OrchestratorRequest -BaseUrl 'http://primary:5000' `
            -Path '/winpe/register' -Method POST -Body @{} `
            -FallbackBaseUrl 'http://fallback:5000' `
            -MaxAttempts 2 -RetryDelayMs 1 -RestInvoker $invoker
        $r.ok | Should -BeTrue
        ($script:visited | Where-Object { $_ -match 'primary' }).Count | Should -Be 2
        ($script:visited | Where-Object { $_ -match 'fallback' }).Count | Should -BeGreaterThan 0
    }

    It 'retries on transient failure up to MaxAttempts then throws' {
        $script:attempts = 0
        $boom = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:attempts++
            throw [System.Net.WebException]::new('connection refused')
        }
        { Invoke-OrchestratorRequest -BaseUrl 'http://x:5000' `
            -Path '/x' -Method GET -RestInvoker $boom `
            -MaxAttempts 3 -RetryDelayMs 1 } |
            Should -Throw '*connection refused*'
        $script:attempts | Should -Be 3
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 3 new FAIL.

- [ ] **Step 3: Implement `Invoke-OrchestratorRequest`**

Append:

```powershell
function Invoke-OrchestratorRequest {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [ValidateSet('GET','POST')] [string] $Method,
        [hashtable] $Body,
        [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [int] $MaxAttempts = 5,
        [int] $RetryDelayMs = 2000,
        [int] $TimeoutSec = 30,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $headers = @{}
    if ($BearerToken) { $headers.Authorization = "Bearer $BearerToken" }
    $payload = $null
    if ($Body) { $payload = $Body | ConvertTo-Json -Depth 10 -Compress }

    $bases = @($BaseUrl)
    if ($FallbackBaseUrl) { $bases += $FallbackBaseUrl }

    $lastErr = $null
    foreach ($base in $bases) {
        $uri = ($base.TrimEnd('/')) + '/' + $Path.TrimStart('/')
        for ($i = 1; $i -le $MaxAttempts; $i++) {
            try {
                return & $RestInvoker $uri $Method $headers $payload 'application/json' $TimeoutSec
            } catch {
                $lastErr = $_
                if ($i -lt $MaxAttempts) { Start-Sleep -Milliseconds $RetryDelayMs }
            }
        }
    }
    throw $lastErr
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-OrchestratorRequest with retries + bearer"
```

### Task D4: Action-dispatcher loop (skeleton; individual actions in D5+)

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing tests**

Append:

```powershell
Describe 'Invoke-ActionLoop' {
    It 'runs each action and refreshes the bearer token from step results' {
        $script:invoked = @()
        $script:tokens = @('initial')
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:invoked += "$Method $Uri"
            if ($Uri -match '/step/\d+/result$') {
                return [pscustomobject]@{ ok = $true; bearer_token = "tok-$($script:invoked.Count)" }
            }
            return [pscustomobject]@{ ok = $true }
        }
        $handlers = @{
            'partition_disk' = { param($p, $tok) }
            'apply_wim'      = { param($p, $tok) }
        }
        $actions = @(
            @{ step_id = 1; kind = 'partition_disk'; params = @{} },
            @{ step_id = 2; kind = 'apply_wim'; params = @{} }
        )
        $finalToken = Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers $handlers -RestInvoker $invoker
        $finalToken | Should -Not -Be 'initial'
        ($script:invoked | Where-Object { $_ -match 'POST.*/step/1/result' }).Count |
            Should -Be 2  # running + ok
    }

    It 'aborts the loop on handler failure and posts state=error' {
        $script:reported = @()
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            if ($Uri -match '/step/\d+/result$') {
                $script:reported += $Body
                return [pscustomobject]@{ ok = $true; bearer_token = 'rolling' }
            }
            return [pscustomobject]@{ ok = $true }
        }
        $handlers = @{
            'apply_wim' = { param($p, $tok) throw "disk too small" }
        }
        $actions = @(@{ step_id = 99; kind = 'apply_wim'; params = @{} })
        { Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers $handlers -RestInvoker $invoker } |
            Should -Throw '*disk too small*'
        ($script:reported -join '|') | Should -Match 'error'
        ($script:reported -join '|') | Should -Match 'disk too small'
    }

    It 'fails fast when no handler is registered for a kind' {
        $invoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            return [pscustomobject]@{ ok = $true; bearer_token = 'rolling' }
        }
        $actions = @(@{ step_id = 1; kind = 'unknown_kind'; params = @{} })
        { Invoke-ActionLoop -BaseUrl 'http://x:5000' `
            -BearerToken 'initial' -Actions $actions `
            -Handlers @{} -RestInvoker $invoker } |
            Should -Throw '*no handler*unknown_kind*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 3 new FAIL.

- [ ] **Step 3: Implement `Invoke-ActionLoop`**

Append:

```powershell
function _ReportStepState {
    param(
        [string] $BaseUrl, [string] $BearerToken, [int] $StepId,
        [string] $State, [string] $ErrorMessage,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker
    )
    $body = @{ state = $State }
    if ($ErrorMessage) { $body.error = $ErrorMessage }
    $reqArgs = @{
        BaseUrl = $BaseUrl
        Path = "/winpe/step/$StepId/result"
        Method = 'POST'
        Body = $body
        BearerToken = $BearerToken
        RestInvoker = $RestInvoker
    }
    if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
    $r = Invoke-OrchestratorRequest @reqArgs
    if ($r.PSObject.Properties.Match('bearer_token').Count -gt 0 -and $r.bearer_token) {
        return $r.bearer_token
    }
    return $BearerToken
}

function Invoke-ActionLoop {
    param(
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [string] $BearerToken,
        [Parameter(Mandatory)] [object[]] $Actions,
        [Parameter(Mandatory)] [hashtable] $Handlers,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $token = $BearerToken
    foreach ($action in $Actions) {
        $kind = $action.kind
        $stepId = [int] $action.step_id
        if (-not $Handlers.ContainsKey($kind)) {
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' `
                -ErrorMessage "no handler for kind: $kind" `
                -FallbackBaseUrl $FallbackBaseUrl `
                -RestInvoker $RestInvoker
            throw "no handler registered for kind: $kind"
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'running' `
            -FallbackBaseUrl $FallbackBaseUrl `
            -RestInvoker $RestInvoker
        try {
            # Handlers receive the CURRENT token (post-running-refresh)
            # rather than capturing the original via closure, so a long
            # apply_wim followed by stage_unattend GETs use a fresh
            # token, not one that may have expired during the apply.
            & $Handlers[$kind] $action.params $token
        } catch {
            $msg = $_.Exception.Message
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' -ErrorMessage $msg `
                -FallbackBaseUrl $FallbackBaseUrl `
                -RestInvoker $RestInvoker
            throw
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'ok' `
            -FallbackBaseUrl $FallbackBaseUrl `
            -RestInvoker $RestInvoker
    }
    return $token
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-ActionLoop dispatcher"
```

### Task D5: Action handler `partition_disk`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-PartitionDisk' {
    It 'emits a diskpart script with Recovery before C: for layout=recovery_before_c' {
        $captured = $null
        $runner = { param($script) $script:captured = $script }
        Invoke-Action-PartitionDisk -Params @{ layout = 'recovery_before_c' } `
            -DiskpartRunner $runner
        $script:captured | Should -Match 'select disk 0'
        $script:captured | Should -Match 'create partition efi size=100'
        $script:captured | Should -Match 'create partition msr size=16'
        # Recovery comes before Windows
        $idxRecovery = $script:captured.IndexOf("create partition primary size=1024")
        $idxOs = $script:captured.IndexOf("create partition primary",
                                          $idxRecovery + 1)
        $idxRecovery | Should -BeLessThan $idxOs
        $idxRecovery | Should -BeGreaterThan -1
        $idxOs | Should -BeGreaterThan -1
    }

    It 'rejects unknown layout values' {
        { Invoke-Action-PartitionDisk -Params @{ layout = 'unknown' } `
            -DiskpartRunner { param($s) } } | Should -Throw '*layout*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
$script:DiskpartScriptRecoveryBeforeC = @'
select disk 0
clean
convert gpt
create partition efi size=100
format fs=fat32 quick label="EFI"
assign letter=S
create partition msr size=16
create partition primary size=1024
format fs=ntfs quick label="Recovery"
set id="de94bba4-06d1-4d40-a16a-bfd50179d6ac"
gpt attributes=0x8000000000000001
create partition primary
format fs=ntfs quick label="Windows"
assign letter=V
exit
'@

function Invoke-Action-PartitionDisk {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DiskpartRunner = { param($script)
            $tmp = [System.IO.Path]::GetTempFileName()
            try {
                Set-Content -LiteralPath $tmp -Value $script -Encoding ASCII
                & diskpart.exe /s $tmp
                if ($LASTEXITCODE -ne 0) { throw "diskpart failed: $LASTEXITCODE" }
            } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        }
    )
    switch ($Params.layout) {
        'recovery_before_c' {
            & $DiskpartRunner $script:DiskpartScriptRecoveryBeforeC
        }
        default { throw "partition_disk: unknown layout '$($Params.layout)'" }
    }
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-PartitionDisk (recovery before C:)"
```

### Task D6: Action handler `apply_wim`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-ApplyWim' {
    It 'invokes dism /apply-image with index resolved by metadata name' {
        $invocations = @()
        $dismRunner = { param($args) $script:invocations += ,$args
            return @{ ExitCode = 0; Stdout = ''; Stderr = '' } }
        $resolveIndex = { param($wim,$name) 6 }   # known mock index
        $resolveSource = { 'D:\sources\install.wim' }
        Invoke-Action-ApplyWim `
            -Params @{ image_index_metadata_name = 'Windows 11 Enterprise' } `
            -DismRunner $dismRunner `
            -SourceWimResolver $resolveSource `
            -IndexResolver $resolveIndex
        $applied = $script:invocations[0]
        ($applied -join ' ') | Should -Match '/Apply-Image'
        ($applied -join ' ') | Should -Match '/ImageFile:D:\\sources\\install.wim'
        ($applied -join ' ') | Should -Match '/Index:6'
        ($applied -join ' ') | Should -Match '/ApplyDir:V:\\\\'
    }

    It 'throws on dism non-zero exit' {
        $dismRunner = { param($args)
            return @{ ExitCode = 5; Stdout = ''; Stderr = 'access denied' } }
        { Invoke-Action-ApplyWim `
            -Params @{ image_index_metadata_name = 'X' } `
            -DismRunner $dismRunner `
            -SourceWimResolver { 'D:\x.wim' } `
            -IndexResolver { 1 } } | Should -Throw '*dism*5*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
function _RunDism {
    param([string[]] $Args)
    $stdout = & dism.exe @Args 2>&1 | Out-String
    return @{ ExitCode = $LASTEXITCODE; Stdout = $stdout; Stderr = '' }
}

function _ResolveSourceWim {
    foreach ($drive in @('D','E','F','G','H')) {
        $p = "$($drive):\sources\install.wim"
        if (Test-Path -LiteralPath $p) { return $p }
    }
    throw "could not find sources\install.wim on attached CD-ROMs"
}

function _ResolveIndexByName {
    param([string] $Wim, [string] $Name)
    $out = (& dism.exe /Get-WimInfo /WimFile:$Wim) 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "dism /Get-WimInfo failed: $LASTEXITCODE" }
    # Parse blocks. dism output:
    #   Index : 6
    #   Name : Windows 11 Enterprise
    $current = $null
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*Index\s*:\s*(\d+)\s*$') { $current = [int]$Matches[1] }
        elseif ($line -match '^\s*Name\s*:\s*(.+?)\s*$' -and $Matches[1] -eq $Name) {
            return $current
        }
    }
    throw "no image index matched name: $Name"
}

function Invoke-Action-ApplyWim {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DismRunner = { param($a) _RunDism -Args $a },
        [scriptblock] $SourceWimResolver = { _ResolveSourceWim },
        [scriptblock] $IndexResolver = { param($wim, $name)
            _ResolveIndexByName -Wim $wim -Name $name
        }
    )
    $name = [string] $Params.image_index_metadata_name
    if ([string]::IsNullOrWhiteSpace($name)) { throw "apply_wim: missing image_index_metadata_name" }
    $wim = & $SourceWimResolver
    $index = & $IndexResolver $wim $name
    $args = @(
        '/Apply-Image',
        "/ImageFile:$wim",
        "/Index:$index",
        '/ApplyDir:V:\'
    )
    $r = & $DismRunner $args
    if ($r.ExitCode -ne 0) {
        throw "dism /Apply-Image failed (exit $($r.ExitCode)): $($r.Stdout)"
    }
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-ApplyWim with index lookup by name"
```

### Task D7: Action handler `inject_drivers`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-InjectDrivers' {
    It 'invokes dism /add-driver against the VirtIO ISO root with /recurse' {
        $invocations = @()
        $dismRunner = { param($a) $script:invocations += ,$a
            return @{ ExitCode = 0; Stdout = '' } }
        $resolveVirtio = { 'E:\' }
        Invoke-Action-InjectDrivers `
            -Params @{ required_infs = @('vioscsi.inf') } `
            -DismRunner $dismRunner `
            -VirtioPathResolver $resolveVirtio
        $args = $script:invocations[0] -join ' '
        $args | Should -Match '/Image:V:\\\\'
        $args | Should -Match '/Add-Driver'
        $args | Should -Match '/Driver:E:\\'
        $args | Should -Match '/Recurse'
        $args | Should -Match '/ForceUnsigned'
    }

    It 'throws when the VirtIO source cannot be located' {
        { Invoke-Action-InjectDrivers `
            -Params @{ required_infs = @('vioscsi.inf') } `
            -DismRunner { param($a) @{ ExitCode = 0 } } `
            -VirtioPathResolver { throw 'no virtio iso found' } } |
            Should -Throw '*virtio*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
function _ResolveVirtioPath {
    # Scan D: through I:. WinPE assigns CD-ROM letters in attach
    # order, and we cannot guarantee D: is the Windows source ISO
    # (the QEMU device order Proxmox emits to the guest depends on
    # bus type + slot, not config order). The marker checks below
    # disambiguate VirtIO from Windows source ISO regardless of
    # which letter each lands on, so we scan the same range as the
    # WIM resolver to avoid an avoidable "could not find VirtIO"
    # failure when the operator's storage assigned letters differently.
    foreach ($drive in @('D','E','F','G','H','I')) {
        $marker = "$($drive):\virtio-win_license.txt"
        if (Test-Path -LiteralPath $marker) { return "$($drive):\" }
        $marker = "$($drive):\NetKVM"
        if (Test-Path -LiteralPath $marker) { return "$($drive):\" }
    }
    throw "could not find VirtIO ISO on any attached CD-ROM (D-I)"
}

function Invoke-Action-InjectDrivers {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DismRunner = { param($a) _RunDism -Args $a },
        [scriptblock] $VirtioPathResolver = { _ResolveVirtioPath }
    )
    $virtioRoot = & $VirtioPathResolver
    $args = @(
        '/Image:V:\',
        '/Add-Driver',
        "/Driver:$virtioRoot",
        '/Recurse',
        '/ForceUnsigned'
    )
    $r = & $DismRunner $args
    if ($r.ExitCode -ne 0) {
        throw "dism /Add-Driver failed (exit $($r.ExitCode)): $($r.Stdout)"
    }
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-InjectDrivers from VirtIO ISO"
```

### Task D8: Action handler `validate_boot_drivers`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-ValidateBootDrivers' {
    It 'passes when all required INFs are present' {
        $resolver = { @('vioscsi.inf', 'netkvm.inf', 'vioser.inf', 'extra.inf') }
        { Invoke-Action-ValidateBootDrivers `
            -Params @{ required_infs = @('vioscsi.inf','netkvm.inf','vioser.inf') } `
            -DriverInfResolver $resolver } | Should -Not -Throw
    }

    It 'throws listing every missing INF' {
        $resolver = { @('vioscsi.inf') }   # netkvm + vioser missing
        { Invoke-Action-ValidateBootDrivers `
            -Params @{ required_infs = @('vioscsi.inf','netkvm.inf','vioser.inf') } `
            -DriverInfResolver $resolver } |
            Should -Throw '*netkvm.inf*vioser.inf*'
    }
}

Describe '_GetInjectedDriverInfs (parses dism /Format:List output)' {
    It 'extracts the leaf INF name from each "Original File Name" line' {
        # Realistic dism /Get-Drivers /Format:List shape (truncated):
        #   Published Name : oem3.inf
        #   Original File Name : E:\NetKVM\w11\amd64\netkvm.inf
        #   Inbox : No
        #   Class Name : Net
        #   ...
        $sampleOutput = @"
Deployment Image Servicing and Management tool
Version: 10.0.26100.1

Image Version: 10.0.26100.1

Driver packages listing:

Published Name : oem3.inf
Original File Name : E:\NetKVM\w11\amd64\netkvm.inf
Inbox : No
Class Name : Net
Provider Name : Red Hat, Inc.
Date : 1/8/2025
Version : 100.95.104.26200

Published Name : oem4.inf
Original File Name : E:\vioscsi\w11\amd64\vioscsi.inf
Inbox : No
Class Name : SCSIAdapter
Provider Name : Red Hat, Inc.
Date : 1/8/2025
Version : 100.95.104.26200

Published Name : oem5.inf
Original File Name : E:\vioserial\w11\amd64\vioser.inf
Inbox : No
Class Name : System
Provider Name : Red Hat, Inc.
Date : 1/8/2025
Version : 100.95.104.26200

The operation completed successfully.
"@
        # Stub dism.exe + LASTEXITCODE for the duration of the call.
        function global:dism.exe { $sampleOutput; $global:LASTEXITCODE = 0 }
        try {
            $infs = _GetInjectedDriverInfs
            $infs | Should -Contain 'netkvm.inf'
            $infs | Should -Contain 'vioscsi.inf'
            $infs | Should -Contain 'vioser.inf'
        } finally {
            Remove-Item Function:\dism.exe -ErrorAction SilentlyContinue
        }
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
function _GetInjectedDriverInfs {
    # /Format:Table truncates "Original File Name" and pads columns
    # unpredictably across DISM versions, so a tail-of-line regex
    # silently returns nothing on real output and validate_boot_drivers
    # incorrectly fails every run. /Format:List emits each driver as a
    # block of "Key : Value" lines; "Original File Name : <path>" is
    # the original INF path (e.g. "E:\NetKVM\w11\amd64\netkvm.inf"),
    # which we tail-split on \ to get the leaf filename.
    $out = (& dism.exe /Image:V:\ /Get-Drivers /Format:List) 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "dism /Get-Drivers failed: $LASTEXITCODE" }
    $infs = @()
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*Original File Name\s*:\s*(.+\\)?(\S+\.inf)\s*$') {
            $infs += $Matches[2].ToLowerInvariant()
        }
    }
    return $infs
}

function Invoke-Action-ValidateBootDrivers {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $DriverInfResolver = { _GetInjectedDriverInfs }
    )
    $required = @($Params.required_infs) | ForEach-Object { $_.ToLowerInvariant() }
    $present = @(& $DriverInfResolver) | ForEach-Object { $_.ToLowerInvariant() }
    $missing = $required | Where-Object { $_ -notin $present }
    if ($missing.Count -gt 0) {
        throw "validate_boot_drivers: missing INFs: $($missing -join ', ')"
    }
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-ValidateBootDrivers"
```

### Task D9: Action handler `stage_autopilot_config`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-StageAutopilotConfig' {
    It 'fetches /winpe/autopilot-config and writes it to V:\Windows\Provisioning\Autopilot' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-stage-$(New-Guid)"
        try {
            $invoker = {
                param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
                return [pscustomobject]@{ Version = 2049; ZtdCorrelationId = 'x' }
            }
            Invoke-Action-StageAutopilotConfig `
                -Params @{ guest_path = "$tmp\AutopilotConfigurationFile.json" } `
                -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
                -RestInvoker $invoker
            $written = Get-Content "$tmp\AutopilotConfigurationFile.json" -Raw | ConvertFrom-Json
            $written.Version | Should -Be 2049
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It 'creates the directory tree if missing' {
        $tmp = "$env:TEMP/wpe-stage-deep-$(New-Guid)/a/b/c"
        try {
            $invoker = {
                param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
                return [pscustomobject]@{ Version = 1 }
            }
            Invoke-Action-StageAutopilotConfig `
                -Params @{ guest_path = "$tmp\AutopilotConfigurationFile.json" } `
                -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
                -RestInvoker $invoker
            Test-Path "$tmp\AutopilotConfigurationFile.json" | Should -BeTrue
        } finally {
            $root = "$env:TEMP/wpe-stage-deep-$(New-Guid)" -replace '[^/]+$',''
            Remove-Item ($tmp -replace '\\a\\b\\c$','') -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
function Invoke-Action-StageAutopilotConfig {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [int] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $guestPath = [string] $Params.guest_path
    if ([string]::IsNullOrWhiteSpace($guestPath)) {
        throw "stage_autopilot_config: missing guest_path"
    }
    $reqArgs = @{
        BaseUrl = $BaseUrl
        Path = "/winpe/autopilot-config/$RunId"
        Method = 'GET'
        BearerToken = $BearerToken
        RestInvoker = $RestInvoker
    }
    if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
    $payload = Invoke-OrchestratorRequest @reqArgs
    $dir = Split-Path -Parent $guestPath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $json = $payload | ConvertTo-Json -Depth 10
    Set-Content -LiteralPath $guestPath -Value $json -Encoding UTF8
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-StageAutopilotConfig"
```

### Task D10: Action handler `bake_boot_entry`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-BakeBootEntry' {
    It 'invokes bcdboot V:\Windows /s S: /f UEFI' {
        $script:lastArgs = $null
        $runner = { param($args)
            $script:lastArgs = $args
            return @{ ExitCode = 0 } }
        Invoke-Action-BakeBootEntry -Params @{} -BcdbootRunner $runner
        ($script:lastArgs -join ' ') | Should -Match 'V:\\\\Windows\s+/s\s+S:\s+/f\s+UEFI'
    }

    It 'throws on non-zero exit' {
        $runner = { param($args) return @{ ExitCode = 1; Stdout = 'no bootmgr' } }
        { Invoke-Action-BakeBootEntry -Params @{} -BcdbootRunner $runner } |
            Should -Throw '*bcdboot*1*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
function _RunBcdboot {
    param([string[]] $Args)
    $stdout = & bcdboot.exe @Args 2>&1 | Out-String
    return @{ ExitCode = $LASTEXITCODE; Stdout = $stdout }
}

function Invoke-Action-BakeBootEntry {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [scriptblock] $BcdbootRunner = { param($a) _RunBcdboot -Args $a }
    )
    $args = @('V:\Windows', '/s', 'S:', '/f', 'UEFI')
    $r = & $BcdbootRunner $args
    if ($r.ExitCode -ne 0) {
        throw "bcdboot failed (exit $($r.ExitCode)): $($r.Stdout)"
    }
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-BakeBootEntry"
```

### Task D11: Action handler `stage_unattend`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Invoke-Action-StageUnattend' {
    It 'fetches /winpe/unattend and writes V:\Windows\Panther\unattend.xml' {
        $tmp = New-Item -Type Directory -Path "$env:TEMP/wpe-unat-$(New-Guid)"
        try {
            $invoker = {
                param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
                return '<unattend>...</unattend>'
            }
            Invoke-Action-StageUnattend `
                -Params @{} -BaseUrl 'http://x:5000' -RunId 7 `
                -BearerToken 'tok' `
                -PantherDirOverride "$tmp" `
                -RestInvoker $invoker
            $body = Get-Content "$tmp\unattend.xml" -Raw
            $body | Should -Match '<unattend>'
        } finally {
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 1 new FAIL.

- [ ] **Step 3: Implement the handler**

Append:

```powershell
function Invoke-Action-StageUnattend {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [int] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [string] $PantherDirOverride,
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $dir = if ($PantherDirOverride) { $PantherDirOverride } else { 'V:\Windows\Panther' }
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $reqArgs = @{
        BaseUrl = $BaseUrl
        Path = "/winpe/unattend/$RunId"
        Method = 'GET'
        BearerToken = $BearerToken
        RestInvoker = $RestInvoker
    }
    if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
    $xml = Invoke-OrchestratorRequest @reqArgs
    Set-Content -LiteralPath (Join-Path $dir 'unattend.xml') `
        -Value $xml -Encoding UTF8
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-StageUnattend"
```

### Task D12: Top-level `Start-AutopilotWinPE` entry + handler registration

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`
- Create: `tools/winpe-build/startnet.cmd`

- [ ] **Step 1: Write the failing test**

Append:

```powershell
Describe 'Start-AutopilotWinPE' {
    It 'registers, runs the action loop, calls /winpe/done, then reboots' {
        $script:posted = @()
        $script:rebootCalled = $false
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:posted += "$Method $Uri"
            if ($Uri -match '/register$') {
                return [pscustomobject]@{
                    run_id = 7
                    bearer_token = 't1'
                    actions = @(
                        @{ step_id = 1; kind = 'partition_disk'; params = @{ layout = 'recovery_before_c' } }
                    )
                }
            } elseif ($Uri -match '/step/\d+/result$') {
                return [pscustomobject]@{ ok = $true; bearer_token = 't2' }
            } elseif ($Uri -match '/done$') {
                return [pscustomobject]@{ ok = $true }
            }
        }
        $rebootRunner = { $script:rebootCalled = $true }
        $tmpCfg = [System.IO.Path]::GetTempFileName()
        Set-Content -LiteralPath $tmpCfg -Value '{"flask_base_url":"http://x:5000","build_sha":"DEV"}' -Encoding UTF8
        try {
            Start-AutopilotWinPE `
                -ConfigPath $tmpCfg `
                -LogPath ([System.IO.Path]::GetTempFileName()) `
                -RestInvoker $invoker `
                -RebootRunner $rebootRunner `
                -UuidResolver { 'fake-uuid' } `
                -MacResolver { 'aa:bb' } `
                -PartitionRunner { param($s) }
            ($script:posted -join '|') | Should -Match 'POST.*/register'
            ($script:posted -join '|') | Should -Match 'POST.*/done'
            $script:rebootCalled | Should -BeTrue
        } finally {
            Remove-Item $tmpCfg -Force -ErrorAction SilentlyContinue
        }
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 1 new FAIL.

- [ ] **Step 3: Implement `Start-AutopilotWinPE`**

Append:

```powershell
function Start-AutopilotWinPE {
    param(
        [string] $ConfigPath = 'X:\autopilot\config.json',
        [string] $LogPath = 'X:\Windows\Temp\autopilot-winpe.log',
        [scriptblock] $RestInvoker = $null,
        [scriptblock] $RebootRunner = { & wpeutil.exe reboot },
        [scriptblock] $UuidResolver = $null,
        [scriptblock] $MacResolver = $null,
        [scriptblock] $PartitionRunner = $null,
        [scriptblock] $DismRunner = $null,
        [scriptblock] $BcdbootRunner = $null,
        [scriptblock] $VirtioPathResolver = $null,
        [scriptblock] $SourceWimResolver = $null,
        [scriptblock] $IndexResolver = $null,
        [scriptblock] $DriverInfResolver = $null,
        [string] $PantherDirOverride
    )
    $cfg = Read-AgentConfig -Path $ConfigPath
    Write-AgentLog -Path $LogPath -Level INFO -Message "agent starting build_sha=$($cfg.build_sha)"

    $idArgs = @{}
    if ($UuidResolver) { $idArgs.UuidResolver = $UuidResolver }
    if ($MacResolver)  { $idArgs.MacResolver  = $MacResolver }
    $id = Get-VMIdentity @idArgs
    Write-AgentLog -Path $LogPath -Level INFO -Message "vm_uuid=$($id.vm_uuid) mac=$($id.mac)"

    $fallbackUrl = $null
    if ($cfg.PSObject.Properties.Match('flask_base_url_fallback').Count -gt 0) {
        $fallbackUrl = $cfg.flask_base_url_fallback
    }
    $reqArgs = @{
        BaseUrl = $cfg.flask_base_url
        Path = '/winpe/register'
        Method = 'POST'
        Body = @{ vm_uuid = $id.vm_uuid; mac = $id.mac; build_sha = $cfg.build_sha }
    }
    if ($fallbackUrl) { $reqArgs.FallbackBaseUrl = $fallbackUrl }
    if ($RestInvoker) { $reqArgs.RestInvoker = $RestInvoker }
    $reg = Invoke-OrchestratorRequest @reqArgs

    $token = $reg.bearer_token
    $runId = [int] $reg.run_id

    $handlers = @{
        'partition_disk' = { param($p, $tok)
            $a = @{ Params = $p }
            if ($PartitionRunner) { $a.DiskpartRunner = $PartitionRunner }
            Invoke-Action-PartitionDisk @a
        }
        'apply_wim' = { param($p, $tok)
            $a = @{ Params = $p }
            if ($DismRunner)        { $a.DismRunner = $DismRunner }
            if ($SourceWimResolver) { $a.SourceWimResolver = $SourceWimResolver }
            if ($IndexResolver)     { $a.IndexResolver = $IndexResolver }
            Invoke-Action-ApplyWim @a
        }
        'inject_drivers' = { param($p, $tok)
            $a = @{ Params = $p }
            if ($DismRunner)         { $a.DismRunner = $DismRunner }
            if ($VirtioPathResolver) { $a.VirtioPathResolver = $VirtioPathResolver }
            Invoke-Action-InjectDrivers @a
        }
        'validate_boot_drivers' = { param($p, $tok)
            $a = @{ Params = $p }
            if ($DriverInfResolver) { $a.DriverInfResolver = $DriverInfResolver }
            Invoke-Action-ValidateBootDrivers @a
        }
        'stage_autopilot_config' = { param($p, $tok)
            $a = @{
                Params = $p; BaseUrl = $cfg.flask_base_url
                RunId = $runId; BearerToken = $tok
            }
            if ($fallbackUrl) { $a.FallbackBaseUrl = $fallbackUrl }
            if ($RestInvoker) { $a.RestInvoker = $RestInvoker }
            Invoke-Action-StageAutopilotConfig @a
        }
        'bake_boot_entry' = { param($p, $tok)
            $a = @{ Params = $p }
            if ($BcdbootRunner) { $a.BcdbootRunner = $BcdbootRunner }
            Invoke-Action-BakeBootEntry @a
        }
        'stage_unattend' = { param($p, $tok)
            $a = @{
                Params = $p; BaseUrl = $cfg.flask_base_url
                RunId = $runId; BearerToken = $tok
            }
            if ($fallbackUrl)        { $a.FallbackBaseUrl = $fallbackUrl }
            if ($RestInvoker)        { $a.RestInvoker = $RestInvoker }
            if ($PantherDirOverride) { $a.PantherDirOverride = $PantherDirOverride }
            Invoke-Action-StageUnattend @a
        }
    }

    $loopArgs = @{
        BaseUrl = $cfg.flask_base_url
        BearerToken = $token
        Actions = $reg.actions
        Handlers = $handlers
    }
    if ($fallbackUrl) { $loopArgs.FallbackBaseUrl = $fallbackUrl }
    if ($RestInvoker) { $loopArgs.RestInvoker = $RestInvoker }
    $token = Invoke-ActionLoop @loopArgs

    $doneArgs = @{
        BaseUrl = $cfg.flask_base_url; Path = '/winpe/done'
        Method = 'POST'; Body = @{}; BearerToken = $token
    }
    if ($fallbackUrl) { $doneArgs.FallbackBaseUrl = $fallbackUrl }
    if ($RestInvoker) { $doneArgs.RestInvoker = $RestInvoker }
    Invoke-OrchestratorRequest @doneArgs

    Write-AgentLog -Path $LogPath -Level INFO -Message 'rebooting'
    & $RebootRunner
}
```

(Note: the `partition_disk` handler in the dispatcher above uses a complex shape so the `PartitionRunner` injection in tests works. Simplify to whatever the test expects when implementing; the test suite covers the contract.)

Create `tools/winpe-build/startnet.cmd`:

```bat
@echo off
wpeinit
if exist X:\autopilot\drivers (
    drvload X:\autopilot\drivers\*\*.inf
)
powershell -NoProfile -ExecutionPolicy Bypass -File X:\autopilot\Invoke-AutopilotWinPE.ps1
```

The agent script must call `Start-AutopilotWinPE` when invoked directly. Append at the very end of `Invoke-AutopilotWinPE.ps1`:

```powershell
# When the script is invoked directly (not dot-sourced), kick off the agent.
if ($MyInvocation.InvocationName -ne '.') {
    Start-AutopilotWinPE
}
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/startnet.cmd tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Start-AutopilotWinPE entry point + startnet.cmd"
```

---

## Phase E: WinPE x64 build pipeline

The build script runs on the existing Windows 11 dev VM (UTM ARM64) using ADK. It produces a versioned `winpe-autopilot-amd64-<sha>.{wim,iso,json}` triple. The script is parameterized so amd64 and arm64 paths share the same logic. Tests on the controller verify the script's argument handling and manifest shape; the actual ADK invocations require the build VM to validate.

### Task E1: Build-script skeleton with `-Arch` parameter

**Files:**
- Create: `tools/winpe-build/build-winpe.ps1`
- Create: `tools/winpe-build/README.md`
- Create: `tools/winpe-build/tests/build-winpe.Tests.ps1`

- [ ] **Step 1: Write the failing tests**

Create `tools/winpe-build/tests/build-winpe.Tests.ps1`:

```powershell
BeforeAll {
    $script:BuildScript = (Resolve-Path "$PSScriptRoot/../build-winpe.ps1").Path
}

Describe 'build-winpe.ps1 parameter validation' {
    It 'accepts -Arch amd64' {
        { & $script:BuildScript -Arch amd64 -DryRun } | Should -Not -Throw
    }

    It 'accepts -Arch arm64' {
        { & $script:BuildScript -Arch arm64 -DryRun } | Should -Not -Throw
    }

    It 'rejects unknown -Arch values' {
        { & $script:BuildScript -Arch x86 -DryRun } | Should -Throw
    }

    It 'emits a manifest path to stdout when -DryRun is set' {
        $out = & $script:BuildScript -Arch amd64 -DryRun
        ($out -join "`n") | Should -Match 'winpe-autopilot-amd64-'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/build-winpe.Tests.ps1 -Output Detailed"
```

Expected: 4 FAIL ("file not found").

- [ ] **Step 3: Create the script**

Create `tools/winpe-build/build-winpe.ps1`:

```powershell
<#
.SYNOPSIS
    Build a custom WinPE image for the ProxmoxVEAutopilot phase-0 agent.

.DESCRIPTION
    Wraps Microsoft ADK + DISM. Produces winpe-autopilot-<arch>-<sha>.iso
    plus a sibling .wim and a manifest .json. -DryRun returns the planned
    output paths without invoking ADK.

.PARAMETER Arch
    amd64 | arm64

.PARAMETER OutputDir
    Where to drop the artifacts. Default: F:\BuildRoot\outputs.

.PARAMETER DryRun
    Resolve all inputs and print the planned outputs, do not invoke ADK.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('amd64','arm64')]
    [string] $Arch,

    [string] $OutputDir = 'F:\BuildRoot\outputs',

    [switch] $DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-BuildSha {
    param([string] $Arch)
    $inputs = @(
        $PSScriptRoot,
        (Get-Item "$PSScriptRoot/Invoke-AutopilotWinPE.ps1").LastWriteTimeUtc.Ticks.ToString(),
        (Get-Item "$PSScriptRoot/config.json").LastWriteTimeUtc.Ticks.ToString(),
        (Get-Item "$PSScriptRoot/startnet.cmd").LastWriteTimeUtc.Ticks.ToString(),
        $Arch
    ) -join '|'
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($inputs)
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return ($hash[0..7] | ForEach-Object { $_.ToString('x2') }) -join ''
}

$sha = Get-BuildSha -Arch $Arch
$base = "winpe-autopilot-$Arch-$sha"
$wimPath  = Join-Path $OutputDir "$base.wim"
$isoPath  = Join-Path $OutputDir "$base.iso"
$manifestPath = Join-Path $OutputDir "$base.json"

Write-Output $manifestPath
Write-Output $wimPath
Write-Output $isoPath

if ($DryRun) { return }

# Real build path is implemented in Tasks E2-E5 below.
throw "build-winpe.ps1: real build path not yet implemented; use -DryRun"
```

Create `tools/winpe-build/README.md` (use FOUR backticks for the outer fence so the inner `powershell`/`markdown` triple-backtick code blocks render correctly):

````markdown
# WinPE build pipeline

Builds a custom WinPE image used by the ProxmoxVEAutopilot phase-0 agent.

## Prerequisites (on the build VM)

- Windows 11 (any edition).
- Windows ADK installed (matching the Windows version).
- WinPE add-on for ADK.
- `pwsh` (PowerShell 7) for running tests; PowerShell 5.1 also works.
- A copy of the VirtIO Win drivers ISO mounted at `D:\virtio` or available
  at `F:\BuildRoot\inputs\virtio-win.iso`.

## Building

```powershell
.\build-winpe.ps1 -Arch amd64
```

Outputs are dropped at `F:\BuildRoot\outputs\winpe-autopilot-<arch>-<sha>.{wim,iso,json}`.
The manifest JSON records the input hashes, ADK version, and SHA-256 of the
final WIM.

## Publishing to Proxmox

```powershell
.\build-winpe.ps1 -Arch amd64
# Then upload the .iso to your Proxmox ISO storage as
# winpe-autopilot-amd64-<sha>.iso so Flask can detect it.
```
````

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/build-winpe.Tests.ps1 -Output Detailed"
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/build-winpe.ps1 tools/winpe-build/README.md tools/winpe-build/tests/build-winpe.Tests.ps1
git commit -m "feat(winpe-build): script skeleton + parameter validation"
```

### Task E2: Implement the ADK build steps

**Files:**
- Modify: `tools/winpe-build/build-winpe.ps1`

This task adds the actual ADK + DISM calls. The Pester suite added in E1 stays passing because `-DryRun` short-circuits before this code runs. The build itself is exercised manually on the build VM as part of Phase H.

- [ ] **Step 1: Replace the `throw` at the end of `build-winpe.ps1`**

Delete the line `throw "build-winpe.ps1: real build path not yet implemented; use -DryRun"` and replace with:

```powershell
$adkRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit"
$peRoot = "$adkRoot\Windows Preinstallation Environment"
$copyPe = "$peRoot\copype.cmd"
if (-not (Test-Path -LiteralPath $copyPe)) {
    throw "ADK + WinPE add-on not installed (looked for $copyPe)"
}

$workRoot = Join-Path $env:TEMP "winpe-build-$Arch-$(New-Guid)"
& cmd /c "`"$copyPe`" $Arch `"$workRoot`"" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "copype failed: $LASTEXITCODE" }

$mountDir = Join-Path $workRoot 'mount'
$bootWim = Join-Path $workRoot 'media\sources\boot.wim'

& dism.exe /Mount-Image /ImageFile:$bootWim /Index:1 /MountDir:$mountDir | Out-Null
if ($LASTEXITCODE -ne 0) { throw "dism /Mount-Image failed: $LASTEXITCODE" }

try {
    $optionalPackages = @(
        'WinPE-WMI', 'WinPE-NetFx', 'WinPE-Scripting', 'WinPE-PowerShell',
        'WinPE-StorageWMI', 'WinPE-DismCmdlets', 'WinPE-SecureStartup'
    )
    $pkgRoot = "$peRoot\$Arch\WinPE_OCs"
    foreach ($pkg in $optionalPackages) {
        & dism.exe /Image:$mountDir /Add-Package /PackagePath:"$pkgRoot\$pkg.cab" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Add-Package $pkg failed: $LASTEXITCODE" }
        $langCab = "$pkgRoot\en-us\${pkg}_en-us.cab"
        if (Test-Path -LiteralPath $langCab) {
            & dism.exe /Image:$mountDir /Add-Package /PackagePath:$langCab | Out-Null
        }
    }

    # Bake-in WinPE-time drivers. vioscsi is REQUIRED so WinPE can see
    # the virtio-scsi-pci disk (otherwise diskpart's `select disk 0`
    # will fail). NetKVM is REQUIRED so the agent can phone home.
    # vioserial is included so any phase-0 fallback to QGA still works.
    # Other drivers (Balloon, vioinput, viogpudo) are needed only by the
    # OS post-apply and are injected into V:\ via dism /add-driver in
    # phase 0 from the still-attached VirtIO ISO -- not baked into WinPE.
    $virtioRoot = $null
    foreach ($candidate in @('D:\virtio','F:\BuildRoot\inputs\virtio-win')) {
        if (Test-Path -LiteralPath $candidate) { $virtioRoot = $candidate; break }
    }
    if (-not $virtioRoot) {
        throw "WinPE build needs a VirtIO driver source at D:\virtio or F:\BuildRoot\inputs\virtio-win"
    }
    foreach ($infName in @('vioscsi.inf', 'netkvm.inf', 'vioser.inf')) {
        $inf = Get-ChildItem -Path $virtioRoot -Recurse -Filter $infName |
            Where-Object FullName -match "\\$Arch\\" |
            Select-Object -First 1
        if (-not $inf) {
            throw "WinPE build cannot find $infName under $virtioRoot for $Arch"
        }
        & dism.exe /Image:$mountDir /Add-Driver /Driver:$($inf.FullName) /ForceUnsigned | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Add-Driver $infName failed: $LASTEXITCODE" }
    }

    # Stage agent files.
    $autopilotDir = Join-Path $mountDir 'autopilot'
    New-Item -ItemType Directory -Path $autopilotDir -Force | Out-Null
    Copy-Item "$PSScriptRoot\Invoke-AutopilotWinPE.ps1" -Destination $autopilotDir
    Copy-Item "$PSScriptRoot\config.json" -Destination $autopilotDir
    Copy-Item "$PSScriptRoot\startnet.cmd" -Destination (Join-Path $mountDir 'Windows\System32\startnet.cmd') -Force
} finally {
    & dism.exe /Unmount-Image /MountDir:$mountDir /Commit | Out-Null
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}
Copy-Item $bootWim -Destination $wimPath -Force

$makeIso = "$peRoot\MakeWinPEMedia.cmd"
& cmd /c "`"$makeIso`" /ISO `"$workRoot`" `"$isoPath`"" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "MakeWinPEMedia failed: $LASTEXITCODE" }

$wimSha = (Get-FileHash -LiteralPath $wimPath -Algorithm SHA256).Hash
$manifest = [pscustomobject]@{
    arch = $Arch
    build_sha = $sha
    output_wim = $wimPath
    output_iso = $isoPath
    wim_sha256 = $wimSha
    adk_root = $adkRoot
    optional_packages = $optionalPackages
    built_at = (Get-Date).ToString('o')
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
```

- [ ] **Step 2: Verify dry-run still works**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/build-winpe.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add tools/winpe-build/build-winpe.ps1
git commit -m "feat(winpe-build): wire ADK + DISM real build path"
```

### Task E3: Manual real build (operator runs on the build VM)

This task is operator-driven; there is no automated assertion. The output is what subsequent tasks consume.

- [ ] **Step 1: Open RDP/VNC into the Windows 11 dev build VM (per existing build pipeline location at `F:\BuildRoot`)**

- [ ] **Step 2: Pull the latest repo into the build VM and run**

```powershell
git pull
.\tools\winpe-build\build-winpe.ps1 -Arch amd64
```

- [ ] **Step 3: Verify the three output files exist at `F:\BuildRoot\outputs`**

```powershell
Get-ChildItem F:\BuildRoot\outputs\winpe-autopilot-amd64-*.* | Format-Table Name, Length, LastWriteTime
```

Expected: a `.wim`, `.iso`, and `.json` with the same `<sha>` stem.

- [ ] **Step 4: Upload the ISO to Proxmox storage** (operator-specific; copy via SMB/scp or use the existing publish path). Verify visibility:

```bash
ssh root@192.168.2.200 "pvesh get /nodes/pve2/storage/isos/content --output json | grep -i winpe-autopilot"
```

Expected: the ISO appears in storage listing.

- [ ] **Step 5: Record the SHA in `inventory/group_vars/all/vars.yml`** (set in the next phase).

- [ ] **Step 6: No commit (artifact-only task).**

---

## Phase F: Ansible role + playbooks

The clone role needs one new flag (`_skip_panther_injection`). The new playbook clones the WinPE blank template, POSTs identity to Flask, attaches three ISOs, sets boot order, starts the VM, and waits for the run state to advance.

### Task F1: Add `_skip_panther_injection` flag to `proxmox_vm_clone`

**Files:**
- Modify: `autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml`

- [ ] **Step 1: Locate the existing Panther injection block**

`autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml`, lines 146-153 (the `Inject per-VM unattend.xml into clone Panther` task).

- [ ] **Step 2: Modify the `when` clause to also gate on the new flag**

Replace the existing `when` line:

```yaml
  when: _answer_floppy_path is defined and _answer_floppy_path | default('') | length > 0
```

with:

```yaml
  when:
    - _answer_floppy_path is defined and _answer_floppy_path | default('') | length > 0
    - not (_skip_panther_injection | default(false) | bool)
```

- [ ] **Step 3: Add a default for the flag**

Open `autopilot-proxmox/roles/proxmox_vm_clone/defaults/main.yml` (create the file if it does not exist). Add:

```yaml
# When true, the Panther offline-inject step is skipped. Used by the
# WinPE provisioning path, which serves the unattend over HTTP at boot
# instead of injecting it into the cloned disk's filesystem.
_skip_panther_injection: false
```

If the file already exists, append the variable.

- [ ] **Step 4: Verify role still parses**

```bash
cd autopilot-proxmox && ansible-playbook --syntax-check playbooks/provision_clone.yml
```

Expected: clean output (no syntax errors).

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/roles/proxmox_vm_clone/tasks/main.yml autopilot-proxmox/roles/proxmox_vm_clone/defaults/main.yml
git commit -m "feat(role): add _skip_panther_injection flag to proxmox_vm_clone"
```

### Task F2: Add WinPE inventory variables

**Files:**
- Modify: `autopilot-proxmox/inventory/group_vars/all/vars.yml`
- Modify: `autopilot-proxmox/inventory/group_vars/all/vault.yml.example`

- [ ] **Step 1: Append to `inventory/group_vars/all/vars.yml`**

Add a new section after the existing `Template settings` block:

```yaml
# =============================================================================
# WinPE provisioning (phase-0 deploy path)
# =============================================================================
# VMID of an empty/clean Proxmox template the WinPE path clones from.
# Configure once after running tools/winpe-build/build-winpe.ps1 and
# uploading the ISO to proxmox_iso_storage. Leave null to disable the
# WinPE option in the web UI.
winpe_blank_template_vmid: null

# Path of the most recent WinPE ISO in Proxmox storage notation.
# Example: "isos:iso/winpe-autopilot-amd64-83793f7eb931ecee.iso"
proxmox_winpe_iso: null

# HMAC secret for /winpe/* bearer tokens. Sourced from vault.
autopilot_winpe_token_secret: "{{ vault_autopilot_winpe_token_secret | default('') }}"

# Comma-separated list of hostnames/IPs allowed to call
# /winpe/run/<id>/identity (Ansible controllers).
autopilot_winpe_identity_allowlist: "127.0.0.1,localhost"
```

- [ ] **Step 2: Append to `inventory/group_vars/all/vault.yml.example`**

```yaml
# Bearer-token signing secret for /winpe/* endpoints. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
vault_autopilot_winpe_token_secret: ""
```

- [ ] **Step 3: Verify yamllint passes**

```bash
cd autopilot-proxmox && yamllint inventory/group_vars/all/vars.yml inventory/group_vars/all/vault.yml.example
```

Expected: clean (no errors).

- [ ] **Step 4: Bridge inventory vars into the FastAPI process**

`web/winpe_token.py` reads `AUTOPILOT_WINPE_TOKEN_SECRET` from the env. The repo's pattern is two loaders: `_load_vars()` (line 246) reads `inventory/group_vars/all/vars.yml` literally (no Jinja rendering, no vault merge) and `_load_vault()` (line 258) reads `vault.yml` literally. Because `vars.yml` line 89 reads `autopilot_winpe_token_secret: "{{ vault_autopilot_winpe_token_secret | default('') }}"`, calling `_load_vars()["autopilot_winpe_token_secret"]` returns the literal Jinja string. The bridge must read `vault_autopilot_winpe_token_secret` directly from `_load_vault()` instead.

For `autopilot_config_path` (already in vars.yml, value `"{{ playbook_dir }}/../files/AutopilotConfigurationFile.json"` -- another Jinja string), the bridge cannot evaluate `playbook_dir`. Treat any Jinja-looking value as "use default" and let the endpoint fall back to `<repo>/autopilot-proxmox/files/AutopilotConfigurationFile.json`.

In `web/app.py`, immediately after the `_load_vault()` definition (around line 258) and before any `from web.winpe_endpoints import ...` line, add:

```python
def _looks_like_jinja(value: str) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _bridge_winpe_vars_to_env() -> None:
    """Mirror WinPE-relevant settings into os.environ so the WinPE
    modules (which read env to stay decoupled from _load_vars at import
    time) see real values. Vault-rendered fields come from _load_vault
    directly because _load_vars returns Jinja strings literally."""
    import os
    raw_vars = _load_vars()
    vault = _load_vault()

    # Token secret lives in vault.yml (vars.yml only references it via
    # Jinja). Read straight from _load_vault to skip the unrendered
    # indirection.
    secret = vault.get("vault_autopilot_winpe_token_secret") or ""
    if secret:
        os.environ["AUTOPILOT_WINPE_TOKEN_SECRET"] = secret

    # Plain integer in vars.yml (no Jinja).
    blank = raw_vars.get("winpe_blank_template_vmid")
    if blank not in (None, "", "null"):
        os.environ["AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID"] = str(blank)

    # Plain string ("isos:iso/...") in vars.yml.
    iso = raw_vars.get("proxmox_winpe_iso") or ""
    if iso and not _looks_like_jinja(iso):
        os.environ["AUTOPILOT_WINPE_ISO"] = iso

    # Plain string in vars.yml.
    allow = raw_vars.get("autopilot_winpe_identity_allowlist") or ""
    if allow and not _looks_like_jinja(allow):
        os.environ["AUTOPILOT_WINPE_IDENTITY_ALLOWLIST"] = allow


_bridge_winpe_vars_to_env()
```

Update `_resolve_autopilot_config_path()` in `web/winpe_endpoints.py` (added in C5) to ignore Jinja-looking values:

```python
def _resolve_autopilot_config_path():
    """Resolve AutopilotConfigurationFile.json. Mirrors what
    roles/autopilot_inject reads from autopilot_config_path. _load_vars
    returns the raw YAML, so the typical inventory value
    "{{ playbook_dir }}/../files/AutopilotConfigurationFile.json" is
    a literal Jinja string here; treat that as 'use default'."""
    from pathlib import Path
    from web import app as web_app
    cfg = web_app._load_vars()
    p = cfg.get("autopilot_config_path") or ""
    if p and "{{" not in p and "{%" not in p:
        return Path(p)
    base = Path(__file__).resolve().parent.parent
    return base / "files" / "AutopilotConfigurationFile.json"
```

The order matters: `_bridge_winpe_vars_to_env()` must precede `app.include_router(_winpe_router)` (which already happens in C2). Move the include block down past `_bridge_winpe_vars_to_env()` if necessary.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/inventory/group_vars/all/vars.yml autopilot-proxmox/inventory/group_vars/all/vault.yml.example autopilot-proxmox/web/app.py
git commit -m "feat(inventory): WinPE vars + app.py bridge to env"
```

### Task F3: Create `wait_for_run_state.yml` common task

**Files:**
- Create: `autopilot-proxmox/roles/common/tasks/wait_for_run_state.yml`

- [ ] **Step 1: Create the file**

```yaml
---
# Poll Flask for a provisioning_run state until it matches one of the
# accepted values, or the deadline passes.
#
# Required vars:
#   _wfrs_run_id          int
#   _wfrs_accepted_states list[str]   e.g. ['awaiting_specialize','failed']
#   _wfrs_timeout         int seconds  (default 1800)
#   _wfrs_poll_interval   int seconds  (default 10)
#
# Sets:
#   _wfrs_final_state    str
#   _wfrs_run            dict   the final run row
#
# Fails when the deadline expires without a matching state.

- name: "Wait for run {{ _wfrs_run_id }} state in {{ _wfrs_accepted_states }}"
  block:
    - name: "Poll until accepted state"
      ansible.builtin.uri:
        url: "{{ autopilot_base_url }}/api/runs/{{ _wfrs_run_id }}"
        method: GET
        return_content: true
        status_code: [200]
      register: _wfrs_resp
      until: "(_wfrs_resp.json.state | default('')) in (_wfrs_accepted_states | default([]))"
      retries: "{{ ((_wfrs_timeout | default(1800)) // (_wfrs_poll_interval | default(10))) | int }}"
      delay: "{{ _wfrs_poll_interval | default(10) | int }}"
  rescue:
    # Ansible-side timeout: poll loop ran out of retries. Mark the run
    # failed in Flask so the UI does not show 'awaiting_winpe' forever
    # and the request-time stale sweep does not have to wait for its
    # TTL to fire.
    - name: "Mark run failed (Ansible-side timeout)"
      ansible.builtin.uri:
        url: "{{ autopilot_base_url }}/api/runs/{{ _wfrs_run_id }}/fail"
        method: POST
        body_format: json
        body:
          reason: "controller-side timeout after {{ _wfrs_timeout | default(1800) }}s waiting for {{ _wfrs_accepted_states | join(',') }}"
        status_code: [200]
      ignore_errors: true
    - name: "Re-raise the wait failure"
      ansible.builtin.fail:
        msg: "wait_for_run_state timed out after {{ _wfrs_timeout | default(1800) }}s"

- name: "Capture final run state"
  ansible.builtin.set_fact:
    _wfrs_final_state: "{{ _wfrs_resp.json.state }}"
    _wfrs_run: "{{ _wfrs_resp.json }}"
```

- [ ] **Step 2: Verify yamllint passes**

```bash
cd autopilot-proxmox && yamllint roles/common/tasks/wait_for_run_state.yml
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/roles/common/tasks/wait_for_run_state.yml
git commit -m "feat(role): add wait_for_run_state common task"
```

### Task F4: Add `GET /api/runs/<id>` Flask endpoint

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py` (or `web/app.py` if `/api/*` lives there)
- Modify: `autopilot-proxmox/tests/test_winpe_endpoints.py`

(`wait_for_run_state.yml` calls `/api/runs/<id>`; we expose it from the same router for symmetry.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_winpe_endpoints.py`:

```python
def test_api_run_returns_state_and_steps(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-1", "mac": "aa", "build_sha": "x"},
    )
    r = web_client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == run_id
    assert body["state"] == "awaiting_winpe"
    assert isinstance(body["steps"], list)
    assert body["steps"][0]["kind"] == "partition_disk"


def test_api_run_returns_404_for_unknown(web_client):
    r = web_client.get("/api/runs/99999")
    assert r.status_code == 404


def test_api_run_fail_marks_run_failed(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    r = web_client.post(
        f"/api/runs/{run_id}/fail",
        json={"reason": "controller timeout 1800s"},
    )
    assert r.status_code == 200
    from web import sequences_db
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "failed"
    assert "controller timeout" in (run["last_error"] or "")


def test_api_run_fail_is_idempotent_on_terminal_state(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    from web import sequences_db
    sequences_db.update_provisioning_run_state(
        test_db, run_id=run_id, state="done",
    )
    r = web_client.post(
        f"/api/runs/{run_id}/fail", json={"reason": "x"},
    )
    assert r.status_code == 200
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k api_run
```

Expected: 2 FAIL.

- [ ] **Step 3: Add the endpoint to `web/winpe_endpoints.py`**

Append:

```python
api_router = APIRouter(prefix="/api", tags=["api"])


# Stale-run TTL: if an active run's newest step has been 'running' for
# longer than this, /api/runs/<id> reads (and /winpe/register lookups)
# flip the run to 'failed' inline before returning. 30 min covers the
# worst-case apply_wim + driver inject we have observed; tighten if
# your storage is faster.
_STALE_RUN_TTL_SECONDS = 30 * 60


@api_router.get("/runs/{run_id}")
def get_run(run_id: int):
    db = _db_path()
    sequences_db.sweep_stale_runs(db, ttl_seconds=_STALE_RUN_TTL_SECONDS)
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    steps = sequences_db.list_run_steps(db, run_id=run_id)
    return {**run, "steps": steps}


class _RunFailBody(BaseModel):
    reason: str = "controller-side timeout"


@api_router.post("/runs/{run_id}/fail")
def post_run_fail(run_id: int, body: _RunFailBody):
    """Out-of-band failure marker. Called by the playbook's
    wait_for_run_state rescue block when its poll loop times out, and
    by operators clicking 'fail run' in the UI. Idempotent: terminal
    states stay terminal."""
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["state"] in ("done", "failed"):
        return {"ok": True}
    sequences_db.update_provisioning_run_state(
        db, run_id=run_id, state="failed",
        last_error=body.reason,
    )
    return {"ok": True}
```

In `web/app.py`, mount the second router:

```python
from web.winpe_endpoints import router as _winpe_router, api_router as _winpe_api_router
app.include_router(_winpe_router)
app.include_router(_winpe_api_router)
```

(Adjust the existing import line you added in C2 to import both names.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(api): GET /api/runs/<id> exposes run + steps"
```

### Task F5: Create `_provision_proxmox_winpe_vm.yml` playbook

**Files:**
- Create: `autopilot-proxmox/playbooks/_provision_proxmox_winpe_vm.yml`
- Create: `autopilot-proxmox/playbooks/provision_proxmox_winpe.yml`

- [ ] **Step 1: Create the inner playbook**

Create `autopilot-proxmox/playbooks/_provision_proxmox_winpe_vm.yml`:

```yaml
---
# WinPE provisioning path. Clones a configured-but-empty template
# (winpe_blank_template_vmid), POSTs vmid + vm_uuid to Flask, attaches
# three ISOs (WinPE, Windows source, VirtIO), sets boot order, starts
# the VM, and waits for the run state to advance to awaiting_specialize.
#
# Required extra vars (passed in by web/app.py):
#   run_id            int    pre-created provisioning_runs row id
#   sequence_id       int
#   autopilot_base_url str   e.g. http://192.168.2.4:5000
#
# Optional:
#   vm_oem_profile, vm_disk_size_gb, vm_memory_mb, etc.
#       (inherit from inventory unless overridden)

- name: Provision a Proxmox VM via WinPE
  hosts: localhost
  gather_facts: false
  vars:
    # The clone role reads `proxmox_template_vmid` (set in inventory).
    # Override it for this run so the role clones the WinPE blank
    # template, not the sysprepped Windows template.
    proxmox_template_vmid: "{{ winpe_blank_template_vmid }}"
    # Skip Panther offline-injection: we serve the unattend over HTTP
    # at boot via /winpe/unattend/<run_id>.
    _skip_panther_injection: true
    # Defer the role's "Start VM" task until after we attach all three
    # ISOs. Otherwise the role boots the VM with no media, BIOS finds
    # nothing on scsi0, and we lose the WinPE entry path.
    vm_start_after_create: false

  pre_tasks:
    - name: Validate WinPE path is configured
      ansible.builtin.assert:
        that:
          - winpe_blank_template_vmid is not none
          - proxmox_winpe_iso is not none
          - proxmox_windows_iso is not none and (proxmox_windows_iso | length) > 0
          - proxmox_virtio_iso is not none and (proxmox_virtio_iso | length) > 0
          - autopilot_winpe_token_secret | length > 0
        fail_msg: >-
          WinPE provisioning requires the following inventory values to
          be set: winpe_blank_template_vmid (the empty template), and
          proxmox_winpe_iso, proxmox_windows_iso, proxmox_virtio_iso
          (the three CDROMs the WinPE phase mounts), plus
          vault_autopilot_winpe_token_secret. proxmox_virtio_iso must
          also be set so the clone role attaches it at ide3 (the WinPE
          phase reads viostor/NetKVM/vioserial from there).

  tasks:
    - name: Clone the WinPE blank template (skips Panther injection, no auto-start)
      ansible.builtin.include_role:
        name: proxmox_vm_clone

    - name: POST run identity to Flask
      ansible.builtin.uri:
        url: "{{ autopilot_base_url }}/winpe/run/{{ run_id }}/identity"
        method: POST
        body_format: json
        body:
          vmid: "{{ vm_vmid | int }}"
          vm_uuid: "{{ _vm_identity.uuid }}"
        status_code: [200]

    - name: Attach WinPE ISO at ide2
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/config"
        method: PUT
        body_format: form-urlencoded
        body:
          ide2: "{{ proxmox_winpe_iso }},media=cdrom"
        headers:
          Authorization: "{{ proxmox_api_auth_header }}"
        validate_certs: "{{ proxmox_validate_certs }}"
        status_code: [200]

    - name: Attach Windows source ISO at sata0
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/config"
        method: PUT
        body_format: form-urlencoded
        body:
          sata0: "{{ proxmox_windows_iso }},media=cdrom"
        headers:
          Authorization: "{{ proxmox_api_auth_header }}"
        validate_certs: "{{ proxmox_validate_certs }}"
        status_code: [200]

    # NOTE: VirtIO ISO is already attached at ide3 by proxmox_vm_clone's
    # update_config.yml (line 31, when proxmox_virtio_iso is defined).
    # Don't double-attach here; just make sure the role left it
    # attached. Detach happens after Specialize completes (below).

    - name: Set boot order to ide2 first then scsi0
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/config"
        method: PUT
        body_format: form-urlencoded
        body:
          boot: "order=ide2;scsi0"
        headers:
          Authorization: "{{ proxmox_api_auth_header }}"
        validate_certs: "{{ proxmox_validate_certs }}"
        status_code: [200]

    - name: Start VM
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/status/start"
        method: POST
        headers:
          Authorization: "{{ proxmox_api_auth_header }}"
        validate_certs: "{{ proxmox_validate_certs }}"

    - name: Wait for WinPE phase to finish (state in awaiting_specialize | failed)
      ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/wait_for_run_state.yml"
      vars:
        _wfrs_run_id: "{{ run_id | int }}"
        _wfrs_accepted_states:
          - awaiting_specialize
          - failed
        _wfrs_timeout: 2400
        _wfrs_poll_interval: 10

    - name: Fail if the WinPE run failed
      ansible.builtin.fail:
        msg: "WinPE run {{ run_id }} failed: {{ _wfrs_run.last_error | default('unknown') }}"
      when: _wfrs_final_state == 'failed'

    - name: "Follow guest through {{ _causes_reboot_count | default(0) }} reboot(s) after Specialize"
      ansible.builtin.include_tasks: "{{ playbook_dir }}/../roles/common/tasks/wait_reboot_cycle.yml"
      loop: "{{ range(0, (_causes_reboot_count | default(0) | int) + 1) | list }}"

    # Hash capture is QGA-driven (Push scripts -> Execute -> Retrieve CSV)
    # and lives in roles/hash_capture. The clone path runs this role at
    # the equivalent point post-Specialize. M1 keeps hash capture in
    # OOBE/QGA-time (NOT pre-OS WinPE); M2 moves it into phase 0.
    - name: Capture Autopilot hash via QGA (M1 hash path)
      ansible.builtin.include_role:
        name: hash_capture
      when:
        - capture_hardware_hash | default(true) | bool
        - (sequence_hash_capture_phase | default('oobe')) == 'oobe'

    - name: Detach VirtIO ISO post-Specialize
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/config"
        method: PUT
        body_format: form-urlencoded
        body:
          delete: "ide3"
        headers:
          Authorization: "{{ proxmox_api_auth_header }}"
        validate_certs: "{{ proxmox_validate_certs }}"
        status_code: [200]
```

- [ ] **Step 2: Create the top-level wrapper**

Create `autopilot-proxmox/playbooks/provision_proxmox_winpe.yml`:

```yaml
---
- import_playbook: _provision_proxmox_winpe_vm.yml
```

- [ ] **Step 3: Verify syntax check passes**

```bash
cd autopilot-proxmox && ansible-playbook --syntax-check playbooks/provision_proxmox_winpe.yml
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/playbooks/_provision_proxmox_winpe_vm.yml autopilot-proxmox/playbooks/provision_proxmox_winpe.yml
git commit -m "feat(playbook): _provision_proxmox_winpe_vm.yml + wrapper"
```

---

## Phase G: Web UI

Two small UI additions: a Boot mode toggle on the provision page and a `/runs/<id>` timeline. The Flask side wires a `provision_path` form field to the new playbook; the timeline page renders run state via the `/api/runs/<id>` endpoint added in F4.

### Task G1: Boot-mode toggle on `/provision` and branch in `/api/jobs/provision`

The existing routes are `GET /provision` (renders `provision.html`) and `POST /api/jobs/provision` (handler `start_provision`, uses Form fields `profile`, `count`, `sequence_id`, `serial_prefix`, etc.). We add ONE form field (`boot_mode`) and a branch in the POST handler that, when `boot_mode == "winpe"`, skips the answer-floppy build and launches the WinPE playbook with the appropriate `-e` overrides.

**Files:**
- Modify: `autopilot-proxmox/web/templates/provision.html`
- Modify: `autopilot-proxmox/web/app.py` (`provision_page` GET handler + `start_provision` POST handler)
- Modify: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_winpe_endpoints.py`:

```python
def test_provision_post_with_boot_mode_winpe_creates_run(
    web_client, test_db, monkeypatch,
):
    """POST /api/jobs/provision with boot_mode=winpe creates a
    provisioning_runs row, skips the answer-floppy build, and launches
    provision_proxmox_winpe.yml."""
    monkeypatch.setenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", "9001")
    monkeypatch.setenv("AUTOPILOT_WINPE_ISO", "isos:iso/winpe-test.iso")

    seq_id = _create_seq(web_client)

    launches = []
    from web import app as web_app

    def fake_run_playbook(playbook_path, extra_vars=None, **_):
        launches.append({"playbook": playbook_path,
                         "extra_vars": dict(extra_vars or {})})
        return {"job_id": 1}

    # Match whatever existing helper start_provision dispatches through.
    # The repo wraps Ansible launch in a function on app.py; the test
    # monkeypatches the same seam used by the clone path.
    monkeypatch.setattr(
        web_app, "_launch_provision_job", fake_run_playbook, raising=False,
    )

    r = web_client.post(
        "/api/jobs/provision",
        data={
            "profile": "generic-desktop",
            "count": 1,
            "sequence_id": seq_id,
            "boot_mode": "winpe",
        },
    )
    assert r.status_code == 200, r.text

    import sqlite3
    with sqlite3.connect(test_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM provisioning_runs "
            "WHERE provision_path='winpe' AND state='queued'"
        ).fetchone()[0]
    assert n == 1
    assert any(
        str(l["playbook"]).endswith("provision_proxmox_winpe.yml")
        for l in launches
    )


def test_provision_post_winpe_rejected_when_not_configured(
    web_client, monkeypatch,
):
    monkeypatch.delenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", raising=False)
    monkeypatch.delenv("AUTOPILOT_WINPE_ISO", raising=False)
    seq_id = _create_seq(web_client)
    r = web_client.post(
        "/api/jobs/provision",
        data={
            "profile": "generic-desktop", "count": 1,
            "sequence_id": seq_id, "boot_mode": "winpe",
        },
    )
    assert r.status_code == 400
    assert b"WinPE" in r.content


def test_provision_page_renders_winpe_option_when_configured(
    web_client, monkeypatch,
):
    monkeypatch.setenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", "9001")
    monkeypatch.setenv("AUTOPILOT_WINPE_ISO", "isos:iso/x.iso")
    r = web_client.get("/provision")
    assert r.status_code == 200
    assert b'name="boot_mode"' in r.content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k provision_post
```

Expected: 3 FAIL.

- [ ] **Step 3: Add `_winpe_enabled` helper and pass into the GET context**

In `web/app.py`, add (near the other inventory-derived helpers, e.g. next to `_load_vars`):

```python
def _winpe_enabled() -> bool:
    """The provision UI shows the WinPE option only when the inventory
    has wired up both a blank template VMID AND a WinPE ISO. The bridge
    in _bridge_winpe_vars_to_env() exports these as env vars at startup
    (see Task F2 step 4); we read the env so the test suite can flip
    the flag without monkeypatching the inventory loader."""
    import os
    return bool(
        os.environ.get("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID")
        and os.environ.get("AUTOPILOT_WINPE_ISO")
    )
```

In `provision_page` (around line 1862), pass `winpe_enabled=_winpe_enabled()` into the template context.

- [ ] **Step 4: Add the form field to `provision.html`**

Find the existing form (search for the existing `name="profile"` or `name="count"` field) and add this row inside the same `<form>`:

```html
{% if winpe_enabled %}
<div class="row mb-3">
  <label class="col-sm-3 col-form-label">Boot mode</label>
  <div class="col-sm-9">
    <select class="form-select" name="boot_mode">
      <option value="clone" selected>Clone (default)</option>
      <option value="winpe">WinPE (blank-disk image-apply)</option>
    </select>
    <div class="form-text">
      WinPE boots a custom WinPE ISO, partitions the disk, applies
      install.wim, and hands off to Specialize. Requires
      <code>winpe_blank_template_vmid</code>,
      <code>proxmox_winpe_iso</code>, and
      <code>vault_autopilot_winpe_token_secret</code> in inventory.
    </div>
  </div>
</div>
{% endif %}
```

- [ ] **Step 5: Branch the POST handler at the right point**

The existing `start_provision` does (in order): validate inputs, stage chassis-type binaries, fetch root@pam ticket if needed, compile the sequence + resolve overrides + materialize the answer floppy, build the `-e` token list, launch Ansible. The WinPE branch needs to share the validate/compile/override work but skip the answer-floppy materialization (no per-VM floppy needed; the unattend ships over HTTP) AND the chassis-type SMBIOS file dance is still required (per-VM SMBIOS file is what carries the canonical UUID for `/winpe/register`).

Add to the function signature:

```python
    boot_mode: str = Form("clone"),
```

**Relax the existing root-ticket preflight first.** Around line 2675, today's code reads `if _chassis_types_to_stage or sequence_id:` and demands `vault_proxmox_root_password`. The root ticket is only used for the `args:` PUT, which is needed for chassis-type SMBIOS files (both paths) AND the per-VM answer floppy attachment (clone path only). WinPE skips the floppy entirely, so a WinPE sequence without chassis override does NOT need root@pam. Change the condition to:

```python
    needs_root_ticket = bool(_chassis_types_to_stage) or (
        sequence_id and boot_mode != "winpe"
    )
    if needs_root_ticket:
        ...
```

Update the in-error message to mention the chassis-or-floppy nuance only when relevant.

After `profile = _sanitize_input(profile)`:

```python
    boot_mode = (boot_mode or "clone").lower()
    if boot_mode not in ("clone", "winpe"):
        raise HTTPException(
            status_code=400, detail=f"unknown boot_mode: {boot_mode!r}",
        )
    if boot_mode == "winpe":
        if not _winpe_enabled():
            raise HTTPException(
                status_code=400,
                detail=(
                    "WinPE provisioning is not configured. Set "
                    "winpe_blank_template_vmid, proxmox_winpe_iso, and "
                    "vault_autopilot_winpe_token_secret in inventory and "
                    "restart the container."
                ),
            )
        if not sequence_id:
            raise HTTPException(
                status_code=400,
                detail="WinPE provisioning requires a sequence_id",
            )
        if int(count) != 1:
            # M1 is one VM per run. Multi-VM WinPE means one provisioning_run
            # per VM, identity POST per VM, and ISO-attach per VM. Defer to
            # M2+; reject loudly so the operator sees the limit.
            raise HTTPException(
                status_code=400,
                detail="WinPE provisioning supports count=1 in M1; "
                       "see docs/superpowers/plans/...-winpe-orchestrated-deploy.md",
            )
```

Then locate the existing sequence-resolution block (around line 2732 starting at `if sequence_id:`). Today this block always builds an answer floppy; we want the floppy build to be skipped for WinPE while everything else (compile, resolve_provision_vars, _causes_reboot_count) runs the same.

Wrap the floppy-only steps in `if boot_mode != "winpe":`. Concretely, in the existing block:

```python
    if sequence_id:
        seq = sequences_db.get_sequence(SEQUENCES_DB, int(sequence_id))
        if seq is None:
            raise HTTPException(404, f"sequence {sequence_id} not found")

        def _resolve_cred(cid: int):
            rec = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cid)
            return rec["payload"] if rec else None

        try:
            compiled = sequence_compiler.compile(
                seq, resolve_credential=_resolve_cred,
            )
        except sequence_compiler.CompilerError as e:
            raise HTTPException(400, f"sequence compile failed: {e}")
        form_overrides = {"vm_oem_profile": profile}
        resolved_vars = sequence_compiler.resolve_provision_vars(
            compiled,
            form_overrides=form_overrides,
            vars_yml=_load_vars(),
        )

        if boot_mode != "winpe":
            # Existing answer-floppy materialization stays here unchanged.
            from web import unattend_renderer, answer_floppy_cache
            _unattend_xml = unattend_renderer.render_unattend(compiled)
            # ... (existing _root_user / _ssh_runner / ensure_floppy logic) ...

        _causes_reboot_count = compiled.causes_reboot_count
```

After the existing block (still inside `start_provision`), add the WinPE launch path. The clone-path `-e` overrides (cores, memory, etc.) are computed by the existing `cmd_tokens` block; the WinPE branch wants the same overrides plus run_id + base_url + sequence_hash_capture_phase. Factor that to one helper:

```python
    if boot_mode == "winpe":
        run_id = sequences_db.create_provisioning_run(
            SEQUENCES_DB,
            sequence_id=int(sequence_id),
            provision_path="winpe",
        )
        # Precedence (lowest -> highest):
        #   1. inventory defaults (already in vars.yml; not added here)
        #   2. sequence-derived resolved_vars (from compile + resolve_provision_vars)
        #   3. form values, when explicitly set (non-zero / non-empty)
        # Step 3 wins so an operator's explicit form input overrides
        # whatever the sequence left as a default. This matches the
        # role's `_effective_chassis_type` behavior (form override wins).
        winpe_extra = dict(resolved_vars)
        # Run-level fixed identifiers, not operator-overridable.
        winpe_extra.update({
            "run_id": run_id,
            "sequence_id": int(sequence_id),
            "vm_count": int(count),
            "sequence_hash_capture_phase": seq["hash_capture_phase"],
            "autopilot_base_url": os.environ.get(
                "AUTOPILOT_BASE_URL", "http://127.0.0.1:5000"),
            "_causes_reboot_count": _causes_reboot_count,
        })
        # Form overrides last (highest precedence).
        winpe_extra["vm_oem_profile"] = profile  # always present in form
        for key, val in (
            ("vm_cores", cores), ("vm_memory_mb", memory_mb),
            ("vm_disk_size_gb", disk_size_gb),
            ("vm_serial_prefix", serial_prefix),
            ("vm_group_tag", group_tag),
            ("hostname_pattern", hostname_pattern),
        ):
            if val:
                winpe_extra[key] = val
        if chassis_type_override and int(chassis_type_override) > 0:
            winpe_extra["chassis_type_override"] = int(chassis_type_override)
        return _launch_provision_job(
            playbook_path="playbooks/provision_proxmox_winpe.yml",
            extra_vars=winpe_extra,
        )
    # Existing clone path continues below unchanged.
```

If `_launch_provision_job` does not yet exist as a named seam, factor the existing Ansible-launch block (at the bottom of `start_provision`, the section that builds `tokens` and runs `ansible-playbook`) into a helper with this name. Both branches use it; the clone path passes its existing token list, the WinPE path passes a single dict that the helper unrolls to `-e key=val` pairs.

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add autopilot-proxmox/web/templates/provision.html autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(ui): Boot mode toggle + branch in /api/jobs/provision"
```

### Task G2: `/runs/<run_id>` timeline page

**Files:**
- Create: `autopilot-proxmox/web/templates/run_detail.html`
- Modify: `autopilot-proxmox/web/app.py`
- Modify: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_run_detail_page_renders(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    web_client.post(
        "/winpe/register",
        json={"vm_uuid": "u-1", "mac": "aa", "build_sha": "x"},
    )
    r = web_client.get(f"/runs/{run_id}")
    assert r.status_code == 200
    assert b"partition_disk" in r.content
    assert b"awaiting_winpe" in r.content


def test_run_detail_404_when_unknown(web_client):
    r = web_client.get("/runs/99999")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k run_detail
```

Expected: 2 FAIL.

- [ ] **Step 3: Create the template**

Create `web/templates/run_detail.html`:

```html
{% extends "base.html" %}
{% block content %}
<div class="container">
  <h1>Run {{ run.id }} <small class="text-muted">{{ run.provision_path }}</small></h1>
  <dl class="row">
    <dt class="col-sm-2">VMID</dt><dd class="col-sm-10">{{ run.vmid or '-' }}</dd>
    <dt class="col-sm-2">VM UUID</dt><dd class="col-sm-10"><code>{{ run.vm_uuid or '-' }}</code></dd>
    <dt class="col-sm-2">State</dt><dd class="col-sm-10"><span class="badge bg-info">{{ run.state }}</span></dd>
    <dt class="col-sm-2">Started</dt><dd class="col-sm-10">{{ run.started_at }}</dd>
    {% if run.finished_at %}
    <dt class="col-sm-2">Finished</dt><dd class="col-sm-10">{{ run.finished_at }}</dd>
    {% endif %}
    {% if run.last_error %}
    <dt class="col-sm-2">Last error</dt><dd class="col-sm-10"><pre>{{ run.last_error }}</pre></dd>
    {% endif %}
  </dl>

  <h2>Steps</h2>
  <table class="table table-sm">
    <thead>
      <tr>
        <th>#</th><th>Phase</th><th>Kind</th><th>State</th>
        <th>Started</th><th>Finished</th><th>Error</th>
      </tr>
    </thead>
    <tbody>
      {% for s in steps %}
      <tr>
        <td>{{ s.order_index }}</td>
        <td>{{ s.phase }}</td>
        <td><code>{{ s.kind }}</code></td>
        <td>
          {% if s.state == 'ok' %}<span class="badge bg-success">ok</span>
          {% elif s.state == 'error' %}<span class="badge bg-danger">error</span>
          {% elif s.state == 'running' %}<span class="badge bg-warning">running</span>
          {% else %}<span class="badge bg-secondary">{{ s.state }}</span>{% endif %}
        </td>
        <td>{{ s.started_at or '' }}</td>
        <td>{{ s.finished_at or '' }}</td>
        <td>{% if s.error %}<pre>{{ s.error }}</pre>{% endif %}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

- [ ] **Step 4: Add the route**

In `web/app.py`:

```python
from fastapi.responses import HTMLResponse


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(run_id: int, request: Request):
    run = sequences_db.get_provisioning_run(SEQUENCES_DB, run_id)
    if run is None:
        raise HTTPException(status_code=404)
    steps = sequences_db.list_run_steps(SEQUENCES_DB, run_id=run_id)
    return templates.TemplateResponse(
        "run_detail.html",
        {"request": request, "run": run, "steps": steps},
    )
```

(Reuse whatever templates wiring app.py already uses for other pages.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/templates/run_detail.html autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(ui): /runs/<id> timeline page"
```

---

## Phase H: M1 integration test + e2e runbook

The unit + Pester suites already verify each piece. M1 ships when one real run on pve1 reaches FirstLogon and the FLC sequence completes. This phase wires that gate.

### Task H1: Whole-suite green check

**Files:**
- (no edits)

- [ ] **Step 1: Run the full Python test suite**

```bash
cd autopilot-proxmox && python -m pytest -v
```

Expected: all PASS, no skips beyond the existing integration markers.

- [ ] **Step 2: Run the full Pester suite**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 3: Ansible syntax check**

```bash
cd autopilot-proxmox && \
  ansible-playbook --syntax-check playbooks/provision_proxmox_winpe.yml && \
  ansible-playbook --syntax-check playbooks/provision_clone.yml
```

Expected: both clean.

- [ ] **Step 4: Yamllint everything new**

```bash
cd autopilot-proxmox && yamllint \
  playbooks/_provision_proxmox_winpe_vm.yml \
  playbooks/provision_proxmox_winpe.yml \
  roles/common/tasks/wait_for_run_state.yml \
  inventory/group_vars/all/vars.yml
```

Expected: clean.

- [ ] **Step 5: No commit (verification-only).**

### Task H2: Build and stage WinPE ISO + blank template (manual)

- [ ] **Step 1: Run `tools/winpe-build/build-winpe.ps1 -Arch amd64`** on the build VM (covered by E3).

- [ ] **Step 2: Upload ISO to Proxmox storage** (covered by E3).

- [ ] **Step 3: Create the blank template VM on Proxmox**

```bash
ssh root@192.168.2.200 << 'SH'
qm create 9001 --name winpe-blank-amd64 \
  --memory 4096 --cores 2 --machine q35 --bios ovmf \
  --efidisk0 ssdpool:1,format=raw,efitype=4m,pre-enrolled-keys=1 \
  --scsihw virtio-scsi-pci \
  --scsi0 ssdpool:64 \
  --net0 virtio,bridge=vmbr0 \
  --ostype win11 \
  --tpmstate0 ssdpool:1,version=v2.0
qm template 9001
SH
```

- [ ] **Step 4: Set inventory variables**

In `autopilot-proxmox/inventory/group_vars/all/vars.yml`:

```yaml
winpe_blank_template_vmid: 9001
proxmox_winpe_iso: "isos:iso/winpe-autopilot-amd64-<sha>.iso"
```

In `vault.yml`:

```yaml
vault_autopilot_winpe_token_secret: "<output of: python -c 'import secrets; print(secrets.token_urlsafe(32))'>"
```

- [ ] **Step 5: Restart the Flask app** so it picks up the new env vars.

```bash
cd autopilot-proxmox && docker compose restart
```

- [ ] **Step 6: No commit (config-only).**

### Task H3: pve1 e2e dry-run with hash_capture_phase=oobe

**Files:**
- Create: `docs/WINPE_E2E_RUNBOOK.md`

- [ ] **Step 1: Write the runbook**

Create `docs/WINPE_E2E_RUNBOOK.md`:

```markdown
# WinPE M1 e2e runbook (pve1)

## Goal
One full provisioning run on pve1 using the WinPE path, demonstrating:
- WinPE boots from the attached ISO,
- partition + WIM apply + driver inject + validate succeed,
- Specialize + OOBE + FirstLogon complete,
- QGA registers within the existing wait window,
- Hash capture (OOBE-pass FLC) emits the expected file.

## Prereqs
- Phase E completed: WinPE ISO uploaded to `isos:iso/`.
- Phase H2 completed: blank template VMID = 9001 exists.
- `vault_autopilot_winpe_token_secret` set in vault.yml.
- Web app restarted.

## Steps

1. From the web UI, open `/provision`.
2. Pick a sequence with `produces_autopilot_hash=true` and `hash_capture_phase=oobe`.
3. Set Boot mode = WinPE. Submit.
4. Open `/runs/<id>` (the redirect target).
5. Watch the `winpe` phase steps go pending -> running -> ok in order:
   `partition_disk`, `apply_wim`, `inject_drivers`, `validate_boot_drivers`,
   (`stage_autopilot_config` if Autopilot-enabled), `bake_boot_entry`,
   `stage_unattend`. Run state moves to `awaiting_specialize` once /winpe/done fires.
6. Watch the VM's Proxmox console: VM reboots; Setup Specialize banner
   appears; OOBE flashes through; first logon happens.
7. QGA reports in. Existing hash-capture FLC writes the hash file.
8. Run state advances to `done` once the playbook posts to `/api/runs/<id>/complete` after the post-Specialize reboot cycle (wired in Task H4).

## Failure-mode triage

- **Run state stuck at `queued`**: Ansible never POSTed identity. Check
  `journalctl -u autopilot-flask` and the playbook log for the URL of the
  identity POST.
- **Run state stuck at `awaiting_winpe`** for more than 10 min: WinPE
  agent did not phone home. Open the VM console to see Invoke-AutopilotWinPE
  output. Common causes: NetKVM not loaded (no NIC), Flask host unreachable
  from the VM SDN, build-time `config.json` has the wrong base URL.
- **Run state = `failed`**: read `last_error` on `/runs/<id>`. The failed
  step's `error` column has the dism/diskpart/bcdboot tail.
- **VM never boots Windows after detach**: check `qm config <vmid>` shows
  `boot: order=scsi0` only. If ide2 is still listed, `/winpe/done` did
  not run; agent crashed.

## What "merged" looks like

`/runs/<id>` shows all phase-0 steps `ok`, run state `awaiting_specialize`,
the VM completes Specialize (existing wait_reboot_cycle returns), and the
existing hash-file watcher reports the hash captured.
```

- [ ] **Step 2: Execute the runbook on pve1.** Record any deviations as new tasks at the bottom of this plan.

- [ ] **Step 3: Commit the runbook**

```bash
git add docs/WINPE_E2E_RUNBOOK.md
git commit -m "docs(winpe): M1 e2e runbook on pve1"
```

### Task H4: Final-state advance (run -> done) when reboot-cycle completes

**Files:**
- Modify: `autopilot-proxmox/playbooks/_provision_proxmox_winpe_vm.yml`
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Modify: `autopilot-proxmox/tests/test_winpe_endpoints.py`

The phase-0 loop ends at `awaiting_specialize`. After the playbook's reboot-cycle returns successfully, we advance to `done`. This needs a small endpoint plus the playbook hook.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_winpe_endpoints.py`:

```python
def test_post_run_complete_advances_to_done(web_client, test_db):
    seq_id = _create_seq(web_client)
    run_id = _create_run(test_db, seq_id)
    web_client.post(
        f"/winpe/run/{run_id}/identity",
        json={"vmid": 1234, "vm_uuid": "u-1"},
    )
    from web import sequences_db
    sequences_db.update_provisioning_run_state(
        test_db, run_id=run_id, state="awaiting_specialize",
    )
    r = web_client.post(f"/api/runs/{run_id}/complete")
    assert r.status_code == 200
    run = sequences_db.get_provisioning_run(test_db, run_id)
    assert run["state"] == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k run_complete
```

Expected: 1 FAIL.

- [ ] **Step 3: Add the endpoint**

In `web/winpe_endpoints.py`, append to `api_router`:

```python
@api_router.post("/runs/{run_id}/complete")
def post_run_complete(run_id: int):
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run["state"] in ("done", "failed"):
        return {"ok": True}
    sequences_db.update_provisioning_run_state(
        db, run_id=run_id, state="done",
    )
    return {"ok": True}
```

- [ ] **Step 4: Hook the playbook**

Add at the very end of `_provision_proxmox_winpe_vm.yml` (after the VirtIO detach step):

```yaml
    - name: Mark run done
      ansible.builtin.uri:
        url: "{{ autopilot_base_url }}/api/runs/{{ run_id }}/complete"
        method: POST
        status_code: [200]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/playbooks/_provision_proxmox_winpe_vm.yml autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): /api/runs/<id>/complete advances run to done"
```

---

## Phase I: M2 -- pre-OS hash capture in WinPE

This phase ships once the M1 e2e runbook is green. It adds the `capture_hash` action to the agent, the `/winpe/hash` endpoint, and enables the `Hash capture phase` dropdown.

### Task I1: Bake `Get-WindowsAutopilotInfo.ps1` into the WIM

**Files:**
- Modify: `tools/winpe-build/build-winpe.ps1`

- [ ] **Step 1: Add a download/copy of the script during build**

In the agent-bake-in section (after `Copy-Item` of `Invoke-AutopilotWinPE.ps1`), add:

```powershell
$gwapiPath = Join-Path $autopilotDir 'Get-WindowsAutopilotInfo.ps1'
$gwapiSource = Join-Path $PSScriptRoot 'vendored/Get-WindowsAutopilotInfo.ps1'
if (Test-Path -LiteralPath $gwapiSource) {
    Copy-Item $gwapiSource -Destination $gwapiPath
} else {
    Invoke-WebRequest -Uri 'https://www.powershellgallery.com/api/v2/package/Get-WindowsAutopilotInfo' `
        -OutFile (Join-Path $env:TEMP 'gwapi.nupkg')
    Expand-Archive (Join-Path $env:TEMP 'gwapi.nupkg') -DestinationPath (Join-Path $env:TEMP 'gwapi-extract') -Force
    Get-ChildItem (Join-Path $env:TEMP 'gwapi-extract') -Recurse -Filter 'Get-WindowsAutopilotInfo.ps1' |
        Select-Object -First 1 |
        Copy-Item -Destination $gwapiPath
}
```

- [ ] **Step 2: Re-run the build on the build VM**

```powershell
.\tools\winpe-build\build-winpe.ps1 -Arch amd64
```

Verify the new ISO contains the script:

```powershell
$mount = (New-Item -Type Directory -Path "C:\winpe-verify" -Force).FullName
Mount-DiskImage -ImagePath "F:\BuildRoot\outputs\winpe-autopilot-amd64-<sha>.iso"
# verify Get-WindowsAutopilotInfo.ps1 exists at the autopilot folder
Dismount-DiskImage -ImagePath "F:\BuildRoot\outputs\winpe-autopilot-amd64-<sha>.iso"
```

- [ ] **Step 3: Commit**

```bash
git add tools/winpe-build/build-winpe.ps1
git commit -m "feat(winpe-build): bake Get-WindowsAutopilotInfo.ps1 into WIM"
```

### Task I2: Implement `Invoke-Action-CaptureHash`

**Files:**
- Modify: `tools/winpe-build/Invoke-AutopilotWinPE.ps1`
- Modify: `tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1`

- [ ] **Step 1: Write the failing test**

Append to `tests/Invoke-AutopilotWinPE.Tests.ps1`:

```powershell
Describe 'Invoke-Action-CaptureHash' {
    It 'invokes Get-WindowsAutopilotInfo, parses CSV, and POSTs to /winpe/hash' {
        $tmpCsv = [System.IO.Path]::GetTempFileName()
        @'
Device Serial Number,Windows Product ID,Hardware Hash
TEST-SERIAL-1,XXXXXXX,DEADBEEFCAFE
'@ | Set-Content -LiteralPath $tmpCsv -Encoding UTF8

        $captureRunner = { param($outputPath)
            Copy-Item -Force -LiteralPath $tmpCsv -Destination $outputPath
            return @{ ExitCode = 0 }
        }
        $script:posted = $null
        $invoker = {
            param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            $script:posted = @{ uri = $Uri; body = $Body }
            return [pscustomobject]@{ ok = $true }
        }
        Invoke-Action-CaptureHash -Params @{} `
            -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
            -CaptureRunner $captureRunner -RestInvoker $invoker

        $script:posted.uri | Should -Match '/winpe/hash$'
        $script:posted.body | Should -Match 'TEST-SERIAL-1'
        $script:posted.body | Should -Match 'DEADBEEFCAFE'

        Remove-Item $tmpCsv -Force
    }

    It 'throws when the capture script fails' {
        $captureRunner = { param($outputPath) return @{ ExitCode = 1 } }
        { Invoke-Action-CaptureHash -Params @{} `
            -BaseUrl 'http://x:5000' -RunId 7 -BearerToken 'tok' `
            -CaptureRunner $captureRunner } |
            Should -Throw '*capture*1*'
    }
}
```

- [ ] **Step 2: Run Pester to verify it fails**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: 2 new FAIL.

- [ ] **Step 3: Implement the handler**

Append to `Invoke-AutopilotWinPE.ps1`:

```powershell
function Invoke-Action-CaptureHash {
    param(
        [Parameter(Mandatory)] [hashtable] $Params,
        [Parameter(Mandatory)] [string] $BaseUrl,
        [Parameter(Mandatory)] [int] $RunId,
        [Parameter(Mandatory)] [string] $BearerToken,
        [string] $FallbackBaseUrl,
        [scriptblock] $CaptureRunner = { param($outputPath)
            $script = 'X:\autopilot\Get-WindowsAutopilotInfo.ps1'
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script `
                -OutputFile $outputPath
            return @{ ExitCode = $LASTEXITCODE }
        },
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $tmp = [System.IO.Path]::GetTempFileName() + '.csv'
    try {
        $r = & $CaptureRunner $tmp
        if ($r.ExitCode -ne 0) {
            throw "Get-WindowsAutopilotInfo capture failed (exit $($r.ExitCode))"
        }
        $row = Import-Csv -LiteralPath $tmp | Select-Object -First 1
        $body = @{
            serial_number = $row.'Device Serial Number'
            product_id    = $row.'Windows Product ID'
            hardware_hash = $row.'Hardware Hash'
        }
        $reqArgs = @{
            BaseUrl = $BaseUrl
            Path = "/winpe/hash"
            Method = 'POST'
            Body = $body
            BearerToken = $BearerToken
            RestInvoker = $RestInvoker
        }
        if ($FallbackBaseUrl) { $reqArgs.FallbackBaseUrl = $FallbackBaseUrl }
        Invoke-OrchestratorRequest @reqArgs
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}
```

Also wire the handler in `Start-AutopilotWinPE`'s `$handlers` table:

```powershell
        'capture_hash' = { param($p, $tok)
            $a = @{
                Params = $p; BaseUrl = $cfg.flask_base_url
                RunId = $runId; BearerToken = $tok
            }
            if ($fallbackUrl) { $a.FallbackBaseUrl = $fallbackUrl }
            if ($RestInvoker) { $a.RestInvoker = $RestInvoker }
            Invoke-Action-CaptureHash @a
        }
```

- [ ] **Step 4: Run Pester to verify it passes**

```bash
pwsh -NoProfile -Command "Invoke-Pester -Path tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1 -Output Detailed"
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/winpe-build/Invoke-AutopilotWinPE.ps1 tools/winpe-build/tests/Invoke-AutopilotWinPE.Tests.ps1
git commit -m "feat(winpe-agent): Invoke-Action-CaptureHash"
```

### Task I3: Implement `POST /winpe/hash`

**Files:**
- Modify: `autopilot-proxmox/web/winpe_endpoints.py`
- Modify: `autopilot-proxmox/tests/test_winpe_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_winpe_endpoints.py`:

```python
def test_post_hash_writes_csv_into_hash_dir(
    web_client, test_db, tmp_path, monkeypatch,
):
    """The endpoint persists by writing a CSV file into HASH_DIR using
    the same column shape get_hash_files / hash_capture role produce.
    Existing parser (web.app.get_hash_files at line 1794) picks it up
    without code changes."""
    from web import app as web_app
    monkeypatch.setattr(web_app, "HASH_DIR", tmp_path, raising=True)

    run_id, reg = _register(web_client, test_db)
    r = web_client.post(
        "/winpe/hash",
        json={
            "serial_number": "S1", "product_id": "PK1",
            "hardware_hash": "HH1",
        },
        headers=_bearer(reg["bearer_token"]),
    )
    assert r.status_code == 200

    csvs = list(tmp_path.glob("*.csv"))
    assert len(csvs) == 1
    text = csvs[0].read_text()
    # Match the columns roles/hash_capture/files/Get-WindowsAutopilotInfo
    # emits ("Device Serial Number,Windows Product ID,Hardware Hash"),
    # which app.py's parser already understands.
    assert "Device Serial Number" in text
    assert "Hardware Hash" in text
    assert "S1" in text
    assert "HH1" in text


def test_post_hash_requires_bearer(web_client):
    r = web_client.post(
        "/winpe/hash",
        json={"serial_number": "S", "product_id": "P", "hardware_hash": "H"},
    )
    assert r.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k post_hash
```

Expected: 2 FAIL.

- [ ] **Step 3: Implement the endpoint**

Append to `web/winpe_endpoints.py`:

```python
class HashBody(BaseModel):
    serial_number: str
    product_id: str
    hardware_hash: str


def _persist_autopilot_hash(*, vmid: int, serial: str,
                            product_id: str, hardware_hash: str) -> None:
    """Persist a captured hash by writing a CSV into HASH_DIR matching
    the column shape produced by roles/hash_capture (the QGA-time path).
    web.app.get_hash_files (line 1794) iterates HASH_DIR.glob('*.csv'),
    so this path is the supported persistence surface."""
    import csv
    from datetime import datetime, timezone
    from web import app as web_app
    web_app.HASH_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_serial = "".join(c for c in serial if c.isalnum() or c in ("-", "_"))
    out = web_app.HASH_DIR / f"{ts}-vm{vmid}-{safe_serial}-winpe.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Device Serial Number",
                    "Windows Product ID",
                    "Hardware Hash"])
        w.writerow([serial, product_id, hardware_hash])


@router.post("/hash")
def post_hash(body: HashBody,
              payload: dict = Depends(_require_bearer_token)):
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, int(payload["run_id"]))
    if run is None or run["vmid"] is None:
        raise HTTPException(status_code=404, detail="run not found or no vmid yet")
    _persist_autopilot_hash(
        vmid=int(run["vmid"]),
        serial=body.serial_number,
        product_id=body.product_id,
        hardware_hash=body.hardware_hash,
    )
    return {"ok": True}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(winpe): POST /winpe/hash persists via existing hash store"
```

### Task I4: Enable `hash_capture_phase` dropdown in sequence editor

The sequence editor saves through JS (`async function save()`) calling `POST /api/sequences` (create) or `PUT /api/sequences/<id>` (update) with a JSON body shaped by `_SequenceCreate` / `_SequenceUpdate` (`web/app.py`). There is no form POST handler. To wire the new field through we extend the Pydantic models, the `update_sequence` DB helper, and the JS payload.

**Files:**
- Modify: `autopilot-proxmox/web/templates/sequence_edit.html` (add `<select>` + include in JS body)
- Modify: `autopilot-proxmox/web/app.py` (`_SequenceCreate`, `_SequenceUpdate`, the create/update routes that pass through to the DB)
- Modify: `autopilot-proxmox/web/sequences_db.py` (`create_sequence` + `update_sequence` accept + persist the column)
- Modify: `autopilot-proxmox/tests/test_sequence_compiler_winpe.py`

- [ ] **Step 1: Add the dropdown to the template**

In `web/templates/sequence_edit.html`, find the existing input/checkbox for `produces_autopilot_hash` (search the file for that string) and add a sibling form row right below:

```html
<div class="row mb-3">
  <label class="col-sm-3 col-form-label">Hash capture phase</label>
  <div class="col-sm-9">
    <select class="form-select" id="hash_capture_phase" name="hash_capture_phase">
      <option value="oobe">OOBE (default; QGA / hash_capture role)</option>
      <option value="winpe">WinPE (pre-OS)</option>
    </select>
    <div class="form-text">
      WinPE phase requires a build (M2) with Get-WindowsAutopilotInfo.ps1
      baked in. Default OOBE matches today's clone-path behavior.
    </div>
  </div>
</div>
```

Where the page initializes form values from the sequence dict (the same place that sets `name`, `description`, etc.), select the right option:

```javascript
document.getElementById("hash_capture_phase").value =
    SEQ.hash_capture_phase || "oobe";
```

In the existing `async function save()` body construction (the `body = { ... steps: ... }` block), add the field:

```javascript
const body = {
    name: document.getElementById('name').value.trim(),
    description: document.getElementById('description').value.trim(),
    target_os: document.getElementById('target_os').value,
    is_default: document.getElementById('is_default').checked,
    produces_autopilot_hash: document.getElementById('produces_autopilot_hash').checked,
    hash_capture_phase: document.getElementById('hash_capture_phase').value,
    steps: stepsState.map(s => ({
        step_type: s.step_type, params: s.params, enabled: s.enabled,
    })),
};
```

- [ ] **Step 2: Extend Pydantic models in `web/app.py`**

Find `_SequenceCreate` (around line 5670) and `_SequenceUpdate` and add the new field. After the change:

```python
class _SequenceCreate(BaseModel):
    name: str
    description: str = ""
    target_os: str = "windows"
    is_default: bool = False
    produces_autopilot_hash: bool = False
    hash_capture_phase: str = "oobe"
    steps: list[_StepIn] = []


class _SequenceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    target_os: Optional[str] = None
    is_default: Optional[bool] = None
    produces_autopilot_hash: Optional[bool] = None
    hash_capture_phase: Optional[str] = None
    steps: Optional[list[_StepIn]] = None
```

In `api_sequences_create`, pass the field through to `sequences_db.create_sequence`:

```python
sid = sequences_db.create_sequence(
    SEQUENCES_DB,
    name=body.name, description=body.description,
    is_default=body.is_default,
    produces_autopilot_hash=body.produces_autopilot_hash,
    target_os=body.target_os,
    hash_capture_phase=body.hash_capture_phase,
)
```

In the equivalent `api_sequences_update` route (search for `@app.put("/api/sequences/`), add the field to the kwargs forwarded to `update_sequence`.

- [ ] **Step 3: Extend DB helpers**

In `web/sequences_db.py`, update `create_sequence` (find the existing signature and append the new parameter):

```python
def create_sequence(db_path, *, name: str, description: str,
                    is_default: bool = False,
                    produces_autopilot_hash: bool = False,
                    target_os: str = "windows",
                    hash_capture_phase: str = "oobe") -> int:
    if hash_capture_phase not in ("winpe", "oobe"):
        raise ValueError(f"invalid hash_capture_phase: {hash_capture_phase!r}")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO task_sequences "
            "(name, description, is_default, produces_autopilot_hash, "
            "target_os, hash_capture_phase, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, description,
             1 if is_default else 0,
             1 if produces_autopilot_hash else 0,
             target_os, hash_capture_phase, _now(), _now()),
        )
        return cur.lastrowid
```

(Adapt to the existing `create_sequence` function body if it shapes things differently; the additive change is the new arg + the new column in the INSERT.)

For `update_sequence`, accept `hash_capture_phase: Optional[str] = None` and add it to the partial-UPDATE construction (existing function uses a list of `set_clauses` it appends to; follow that pattern). Validate `in ("winpe","oobe")` only when non-None.

For `duplicate_sequence` (line 366), thread the field through so a duplicated sequence keeps its hash phase rather than silently reverting to `oobe`. The current body calls `create_sequence(...)` with only a subset of fields; add `hash_capture_phase=seq["hash_capture_phase"]`:

```python
def duplicate_sequence(db_path, seq_id: int, *, new_name: str) -> int:
    seq = get_sequence(db_path, seq_id)
    if seq is None:
        raise ValueError(f"sequence {seq_id} not found")
    new_id = create_sequence(
        db_path, name=new_name, description=seq["description"],
        is_default=False,
        produces_autopilot_hash=seq["produces_autopilot_hash"],
        target_os=seq.get("target_os", "windows"),
        hash_capture_phase=seq["hash_capture_phase"],
    )
    set_sequence_steps(db_path, new_id, [
        {"step_type": s["step_type"], "params": s["params"], "enabled": s["enabled"]}
        for s in seq["steps"]
    ])
    return new_id
```

- [ ] **Step 4: Add a test**

Append to `tests/test_sequence_compiler_winpe.py`:

```python
def test_create_sequence_persists_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="winpe-seq", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False, hash_capture_phase="winpe",
    )
    seq = sequences_db.get_sequence(db, sid)
    assert seq["hash_capture_phase"] == "winpe"


def test_update_sequence_changes_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="oobe-seq", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False,
    )
    sequences_db.update_sequence(
        db, seq_id=sid,
        hash_capture_phase="winpe",
    )
    seq = sequences_db.get_sequence(db, sid)
    assert seq["hash_capture_phase"] == "winpe"


def test_create_sequence_rejects_unknown_hash_capture_phase(tmp_path):
    import pytest
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    with pytest.raises(ValueError):
        sequences_db.create_sequence(
            db, name="bad", description="",
            target_os="windows", produces_autopilot_hash=False,
            is_default=False, hash_capture_phase="bogus",
        )


def test_duplicate_sequence_preserves_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="src-winpe", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False, hash_capture_phase="winpe",
    )
    new_id = sequences_db.duplicate_sequence(
        db, sid, new_name="src-winpe-copy",
    )
    assert sequences_db.get_sequence(db, new_id)["hash_capture_phase"] == "winpe"


def test_api_sequences_create_persists_hash_capture_phase(web_client):
    r = web_client.post(
        "/api/sequences",
        json={
            "name": "winpe-via-api",
            "description": "",
            "target_os": "windows",
            "is_default": False,
            "produces_autopilot_hash": True,
            "hash_capture_phase": "winpe",
            "steps": [],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    r2 = web_client.get(f"/api/sequences/{body['id']}")
    assert r2.json()["hash_capture_phase"] == "winpe"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequence_compiler_winpe.py tests/test_sequences_db.py tests/test_sequences_api.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/templates/sequence_edit.html autopilot-proxmox/web/app.py autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequence_compiler_winpe.py
git commit -m "feat(ui): hash_capture_phase dropdown via /api/sequences JSON"
```

### Task I5: M2 e2e validation

**Files:**
- Modify: `docs/WINPE_E2E_RUNBOOK.md`

- [ ] **Step 1: Append an M2 section to the runbook**

```markdown

## M2: pre-OS hash capture validation

1. Edit a test sequence; set Hash capture phase = WinPE; save.
2. Provision with Boot mode = WinPE.
3. On `/runs/<id>` confirm the action list now starts with `capture_hash`.
4. Confirm the captured hash appears in the existing hashes UI.
5. Compare against an OOBE-pass capture for the same VM; the
   `hardware_hash` value must match.

If the capture step fails on the live cluster (likely with
Get-WindowsAutopilotInfo's WMI provider expectations), evaluate the
OA3Tool path or direct-SMBIOS path before re-running. Each gets its
own follow-up task.
```

- [ ] **Step 2: Run on pve1.** Record the result; if hash matches OOBE-captured value, M2 is shipped.

- [ ] **Step 3: Commit**

```bash
git add docs/WINPE_E2E_RUNBOOK.md
git commit -m "docs(winpe): M2 e2e validation steps"
```

---

## Self-review

This plan covers every shipping unit listed in spec section 13:

| Spec line | Plan task |
|---|---|
| `tools/winpe-build/` parameterized for amd64 | E1, E2, E3 |
| `Invoke-AutopilotWinPE.ps1` agent + config.json | D1-D12 |
| Flask `/winpe/*` endpoints (M1) | C1-C8 |
| Flask `/winpe/hash` (M2) | I3 |
| `compile_winpe` + `CompiledWinPEPhase` | B1, B2 |
| `autounattend.post_winpe.xml.j2` + `phase_layout` | B3, B4 |
| Schema migration (provisioning_runs, run_steps, hash_capture_phase) | A1, A2, A3 |
| `_skip_panther_injection` flag | F1 |
| `_provision_proxmox_winpe_vm.yml` + wrapper | F5 |
| `winpe_blank_template_vmid` inventory variable | F2 |
| Boot-mode UI toggle | G1 |
| `/runs/<id>` timeline | G2 |
| Phase-0 actions (partition_disk, apply_wim, inject_drivers, validate_boot_drivers, stage_autopilot_config, bake_boot_entry, stage_unattend) | D5-D11 |
| capture_hash action (M2) | I2 |
| `hash_capture_phase` dropdown enabled (M2) | I4 |
| Hash capture stays in OOBE FLC for M1 | (no change; existing path untouched) |
| M1 merge gate: e2e on pve1 | H1, H2, H3, H4 |
| M2 merge gate: e2e on pve1 | I5 |

Implicit cross-references and consistency:

- Action `kind` strings in the compiler (B2) match the handler names in the agent dispatcher (D5-D11). Both lists: `partition_disk`, `apply_wim`, `inject_drivers`, `validate_boot_drivers`, `stage_autopilot_config`, `bake_boot_entry`, `stage_unattend`, `capture_hash`.
- Token TTL: `_REGISTER_TOKEN_TTL = 60 * 60` is used both at /winpe/register (C3) and on every step-result refresh (C7), matching the spec's "60-minute initial validity, refreshed on every step-result POST."
- Run states: `queued -> awaiting_winpe -> awaiting_specialize -> done|failed`. Tasks A3 (helpers), C2 (identity), C3 (register), C8 (done), F5 (final advance), H4 (complete).
- `vmid` is `INTEGER NULL` (A2) and Ansible owns allocation via `/cluster/nextid` (F5).
- `vm_uuid` flows from `_vm_identity.uuid` (F5) to `provisioning_runs.vm_uuid` (A2/A3) to `/winpe/register` match (C3) to the agent's `Get-VMIdentity` (D2). One canonical source: `_vm_identity.uuid`, never `qm config`.
- The post-WinPE template (B3) drops the windowsPE settings block in full (DiskConfiguration, ImageInstall, PnpCustomizationsWinPE all lived there). It does NOT add a substitute `PnpCustomizationsNonWinPE` block; phase-0 dism /add-driver against the attached VirtIO ISO seeds the driver store directly into V:\ before BCD is written, so post-Specialize Windows already has the drivers.
- `validate_boot_drivers` (D8) requires `vioscsi.inf`, `netkvm.inf`, `vioser.inf` (compiler default in B2). Failure aborts the run; spec's "no continue on partial driver inject" behavior.

### v2 corrections vs v1 (operator review on 2026-05-04)

| v1 mistake | v2 fix |
|---|---|
| Used `_template_vmid_override` (does not exist in clone role) | F5 sets `proxmox_template_vmid` directly (the actual var the role reads) |
| Clone role started VM before media attach | F5 sets `vm_start_after_create: false`; explicit start step after all three ISOs attached |
| M1 hash capture said "stays in OOBE FLC"; actual path is QGA-driven `roles/hash_capture` | F5 invokes the `hash_capture` role post-Specialize; gated on `sequence_hash_capture_phase == 'oobe'` |
| `/winpe/autopilot-config/<run_id>` returned a placeholder JSON | C5 reads `autopilot_config_path` (= `files/AutopilotConfigurationFile.json`) and returns the real bytes; new `_resolve_autopilot_config_path` seam for tests |
| Token + WinPE-enabled flags read from env vars but inventory uses `_load_vars()` | F2 step 4 adds `_bridge_winpe_vars_to_env()` in `web/app.py` to mirror inventory values into the env at startup |
| `/devices/provision-form` and `/devices/provision` (do not exist) | G1 wires into the actual `GET /provision` and `POST /api/jobs/provision` (form fields `profile`, `count`, plus new `boot_mode`) |
| `_create_seq` test helper expected status 200 + `params_json` | Updated to status 201 + `params` (matches `_StepIn` model + `status_code=201`) |
| B3 test asserted `PnpCustomizationsNonWinPE` exists in the source template | B3 test asserts windowsPE-only blocks (DiskConfiguration / ImageInstall / PnpCustomizationsWinPE) are gone from the post_winpe template |
| `/winpe/unattend/<run_id>` called `compile()` without resolver; sequences with `local_admin` / `domain_join` would fail with `CredentialMissing` | C6 builds a resolver matching `start_provision`'s pattern (line 2737-2745) and passes it as `resolve_credential=` |
| C8 referenced nonexistent `web/proxmox_client.py` | C8 uses inline requests against `proxmox_api_base` matching app.py's PUT pattern; one PUT carries `delete=` and `boot=` together |
| I3 referenced nonexistent `hashes_db` module | I3 writes a CSV into `HASH_DIR` matching the column shape `get_hash_files` already parses (line 1794) |
| E2 only baked NetKVM | E2 now bakes vioscsi + NetKVM + vioser (vioscsi is required for WinPE to see the virtio-scsi-pci disk) |

### v2.1 corrections vs v2 (second operator review)

| v2 mistake | v2.1 fix |
|---|---|
| F2 bridge read `_load_vars()["autopilot_winpe_token_secret"]` (returns the literal Jinja string) | F2 bridge reads `_load_vault()["vault_autopilot_winpe_token_secret"]` directly; `_resolve_autopilot_config_path()` falls back to default whenever the inventory value contains `{{` |
| C8 read `proxmox_api_base` / `proxmox_api_auth_header` (Jinja in inventory) | C8 calls `web_app._proxmox_api_put(path, data)` (line 1294) which builds URL + auth from primitive vault fields |
| G1 early-returned for WinPE, skipping the existing sequence compile + form-overrides + chassis-type SMBIOS staging | G1 reuses the existing compile/override path, gates only the answer-floppy materialization, then launches the WinPE playbook with the same overrides; rejects `count > 1` with a clear M1 message |
| Action handlers captured `$token` via closure, so long applies bricked subsequent stage_* GETs | Handlers receive the current token via `param($p, $tok)`; `Invoke-ActionLoop` passes the post-running-refresh token at each call |
| `Start-AutopilotWinPE` passed `-RestInvoker $RestInvoker` even when null (overrode the handler's own default) | Each handler entry uses the conditional-splat pattern (build hashtable, only add overrides when non-null) |
| F5 attached VirtIO ISO at sata1, but `proxmox_vm_clone/update_config.yml` line 31 already attaches it at ide3 | F5 drops the duplicate attach; cleanup detaches `ide3` (the role-attached slot) |
| `_create_seq` used `f"wpe-{id(client)}"` (collides on repeat calls in one test) | `_create_seq` uses `f"wpe-{uuid4().hex[:8]}"` |
| I4 described a sequence-edit POST handler that does not exist (editor saves via JS to `/api/sequences`) | I4 extends `_SequenceCreate` / `_SequenceUpdate` Pydantic models, the JS payload, and `create_sequence` / `update_sequence` DB helpers |

### v2.2 corrections vs v2.1 (third operator review)

| v2.1 mistake | v2.2 fix |
|---|---|
| `compile_winpe` emitted `guest_path = C:\Windows\...` but phase 0 applies Windows to V:\, so the offline write would either fail (no C: in WinPE) or land in the wrong place | Compiler now emits `V:\Windows\Provisioning\Autopilot\AutopilotConfigurationFile.json`; the OS sees this as `C:\Windows\...` after first boot when V: is remapped |
| C5 referenced `Response(...)` but `from fastapi.responses import Response` was added in C6 -> NameError on C5 tests | C5 step 3 now adds the import at the top of `winpe_endpoints.py`; C6 reuses the already-imported name |
| WinPE provision still triggered the root-ticket preflight via `if _chassis_types_to_stage or sequence_id:` (root@pam needed only for chassis-args; WinPE skips the answer-floppy entirely) | G1 also relaxes the preflight: `needs_root_ticket = bool(_chassis_types_to_stage) or (sequence_id and boot_mode != "winpe")` |
| WinPE extra-vars precedence inverted: `winpe_extra.update(resolved_vars)` overwrote form-supplied chassis_type_override with sequence/default | G1 builds `winpe_extra = dict(resolved_vars)` first, then layers form values on top so explicit form input wins (matches role's `_effective_chassis_type` precedence) |
| `duplicate_sequence` did not copy `hash_capture_phase`, silently reverting duplicated sequences to `oobe` | I4 patches `duplicate_sequence` to thread the field through; new test asserts the duplicated sequence keeps the original phase |

### v2.3 changes vs v2.2 (operational-risk feedback)

| v2.2 gap | v2.3 fix |
|---|---|
| No timeout/recovery for runs whose agent crashed mid-step (UI froze in `running` indefinitely) | Task A4 adds `sweep_stale_runs(ttl_seconds)` that flips long-silent active runs to `failed` inline at `/api/runs/<id>` reads (no background thread); `/api/runs/<id>/fail` endpoint added for explicit out-of-band failure; `wait_for_run_state.yml` rescue block POSTs to it on Ansible-side timeout |
| Network-bootstrap dependency chain was not surfaced anywhere in one place | D1 step 4 documents the full bootstrap order (UEFI -> WinPE drivers baked at E2 -> startnet drvload -> agent network wait -> register -> dynamic driver inject); makes the chicken-and-egg explicit |
| `flask_base_url` was IP-only; no fallback if operators want hostnames | `config.json` adds optional `flask_base_url_fallback`; `Invoke-OrchestratorRequest` accepts `FallbackBaseUrl` and retries it after the primary exhausts; `Start-AutopilotWinPE` threads the fallback into the register + done calls (action handlers continue using the primary URL since they only run after register has confirmed connectivity) |

### v2.4 changes vs v2.3 (fifth review pass)

| v2.3 gap | v2.4 fix |
|---|---|
| `_vm_identity.uuid` from Ansible is uppercase; agent lowercases the WMI value before POSTing; `find_run_by_uuid_state` did exact `WHERE vm_uuid=?`; live registration would 404 | `sequences_db._normalize_uuid()` lowercases on every write (`set_provisioning_run_identity`) and lookup (`find_run_by_uuid_state`); new test posts uppercase identity then registers with lowercase and asserts a match |
| `validate_boot_drivers` parsed `dism /Get-Drivers /Format:Table` with a tail-of-line regex that misses on real DISM output (column truncation, padding inconsistency) | `_GetInjectedDriverInfs` now uses `/Format:List` and parses the `Original File Name : <path>` line; new Pester fixture exercises realistic DISM output and asserts vioscsi.inf / netkvm.inf / vioser.inf are extracted |
| `_ResolveVirtioPath` scanned E-I, but Proxmox CDROM letter assignment depends on bus + slot, not config order; if VirtIO landed on D: the agent failed | Resolver now scans D-I (matching the WIM resolver range); the marker checks (`virtio-win_license.txt` + `NetKVM` directory) disambiguate VirtIO from the Windows source ISO |
| F5 preflight asserted only `winpe_blank_template_vmid`, `proxmox_winpe_iso`, and the token; missing `proxmox_windows_iso` / `proxmox_virtio_iso` would slip past and fail mid-clone | F5 preflight now asserts all five vars before cloning |
| `FallbackBaseUrl` was wired only into register + done; step-result POSTs and the staging/hash GETs continued using the primary URL, so a primary-failed/fallback-registered run would die at the first step result | `Invoke-ActionLoop` accepts `-FallbackBaseUrl` and threads it into `_ReportStepState`; `Invoke-Action-StageAutopilotConfig`, `Invoke-Action-StageUnattend`, and `Invoke-Action-CaptureHash` accept the same parameter; `Start-AutopilotWinPE` threads the fallback into all of them |
| README codeblock used triple-backtick fence wrapping triple-backtick code blocks (rendered plan broke at the inner `\`\`\`powershell`) | Outer fence is now four backticks |

No placeholders, TBDs, or "implement later" lines. Every code-bearing step shows the actual code.
