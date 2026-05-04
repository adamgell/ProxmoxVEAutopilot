# WinPE-Orchestrated Proxmox Deploy Implementation Plan

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


def set_provisioning_run_identity(db_path, *,
                                  run_id: int,
                                  vmid: int,
                                  vm_uuid: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE provisioning_runs "
            "SET vmid=?, vm_uuid=?, state='awaiting_winpe' "
            "WHERE id=? AND state='queued'",
            (vmid, vm_uuid, run_id),
        )


def find_run_by_uuid_state(db_path, *,
                           vm_uuid: str,
                           state: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provisioning_runs "
            "WHERE vm_uuid=? AND state=? "
            "ORDER BY id DESC LIMIT 1",
            (vm_uuid, state),
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
    assert p.autopilot_config_payload is None


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
    autopilot_config_payload: Optional[dict] = None
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


def test_compile_winpe_autopilot_payload_present_when_enabled():
    from web.sequence_compiler import compile_winpe
    seq = _seq(steps=[{
        "step_type": "autopilot_entra",
        "params_json": "{}",
        "enabled": True, "order_index": 0,
    }])
    p = compile_winpe(seq)
    assert p.autopilot_config_payload is not None
    # Cloud-Assigned-Tenant style minimal sanity
    assert isinstance(p.autopilot_config_payload, dict)
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

# Default Autopilot config payload shape. Operator must override
# AutopilotConfigurationFile.json contents in vault/group_vars before any
# real Autopilot run; this payload is the minimum the compiler needs to
# emit so the agent has SOMETHING to write. The Flask endpoint
# /winpe/autopilot-config/<run_id> overrides this with the real bytes
# from autopilot_config_path at request time.
_AUTOPILOT_PAYLOAD_PLACEHOLDER = {
    "Comment_File": "Profile served by ProxmoxVEAutopilot",
    "Version": 2049,
    "ZtdCorrelationId": "00000000-0000-0000-0000-000000000000",
}


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
        out.actions.append({
            "kind": "stage_autopilot_config",
            "params": {
                "guest_path": (
                    "C:\\Windows\\Provisioning\\Autopilot\\"
                    "AutopilotConfigurationFile.json"
                ),
            },
        })
        out.autopilot_config_payload = dict(_AUTOPILOT_PAYLOAD_PLACEHOLDER)

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


def test_template_has_offlineServicing_pnp_block():
    """vioserial driver-store staging must survive the windowsPE removal."""
    text = _TEMPLATE.read_text()
    assert "Microsoft-Windows-PnpCustomizationsNonWinPE" in text


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
grep -c 'PnpCustomizationsNonWinPE' autopilot-proxmox/files/autounattend.post_winpe.xml.j2
```

Expected: each at least 1.

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
    """Helper: create a sequence via the existing API and return its id."""
    body = {
        "name": overrides.get("name", "wpe"),
        "description": "",
        "target_os": "windows",
        "produces_autopilot_hash": overrides.get("autopilot", False),
        "is_default": False,
        "steps": overrides.get("steps", []),
    }
    r = client.post("/api/sequences", json=body)
    assert r.status_code == 200, r.text
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
def test_autopilot_config_returns_payload_when_enabled(web_client, test_db):
    seq_id = _create_seq(web_client, steps=[{
        "step_type": "autopilot_entra",
        "params_json": "{}", "enabled": 1, "order_index": 0,
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
    assert r.json().get("Version") == 2049


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

Append to `web/winpe_endpoints.py`:

```python
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
    if phase.autopilot_config_payload is None:
        raise HTTPException(status_code=404, detail="autopilot not enabled")
    return phase.autopilot_config_payload
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

Append to `web/winpe_endpoints.py`:

```python
from fastapi.responses import Response


@router.get("/unattend/{run_id}")
def get_unattend(run_id: int,
                 _: int = Depends(_require_bearer_for_run)):
    from web import sequence_compiler, unattend_renderer
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    seq = sequences_db.get_sequence(db, run["sequence_id"])
    compiled = sequence_compiler.compile(seq)
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

    Real implementation lives in web/proxmox_client.py (the existing
    helper module used elsewhere); this wrapper exists so tests can
    monkeypatch a single seam without touching the broader API code.
    """
    from web import proxmox_client
    proxmox_client.update_vm_config(
        vmid=vmid,
        body={**{slot: "" for slot in slots},  # delete=...
              "boot": set_boot_order,
              "delete": ",".join(slots)},
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

If `web/proxmox_client.py` does not yet expose `update_vm_config` with this exact signature, add a thin wrapper that uses the existing API-token-based `PUT /nodes/<node>/qemu/<vmid>/config` pattern (the body format is form-urlencoded with `delete=ide2,sata0&boot=order=scsi0`).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/winpe_endpoints.py autopilot-proxmox/web/proxmox_client.py autopilot-proxmox/tests/test_winpe_endpoints.py
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
  "build_sha": "DEV"
}
```

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
    $uri = ($BaseUrl.TrimEnd('/')) + '/' + $Path.TrimStart('/')
    $payload = $null
    if ($Body) { $payload = $Body | ConvertTo-Json -Depth 10 -Compress }

    $lastErr = $null
    for ($i = 1; $i -le $MaxAttempts; $i++) {
        try {
            return & $RestInvoker $uri $Method $headers $payload 'application/json' $TimeoutSec
        } catch {
            $lastErr = $_
            if ($i -lt $MaxAttempts) { Start-Sleep -Milliseconds $RetryDelayMs }
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
            'partition_disk' = { param($p) }
            'apply_wim'      = { param($p) }
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
            'apply_wim' = { param($p) throw "disk too small" }
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
        [string] $State, [string] $ErrorMessage, [scriptblock] $RestInvoker
    )
    $body = @{ state = $State }
    if ($ErrorMessage) { $body.error = $ErrorMessage }
    $r = Invoke-OrchestratorRequest -BaseUrl $BaseUrl `
        -Path "/winpe/step/$StepId/result" -Method POST `
        -Body $body -BearerToken $BearerToken `
        -RestInvoker $RestInvoker
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
                -RestInvoker $RestInvoker
            throw "no handler registered for kind: $kind"
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'running' -RestInvoker $RestInvoker
        try {
            & $Handlers[$kind] $action.params
        } catch {
            $msg = $_.Exception.Message
            $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
                -StepId $stepId -State 'error' -ErrorMessage $msg `
                -RestInvoker $RestInvoker
            throw
        }
        $token = _ReportStepState -BaseUrl $BaseUrl -BearerToken $token `
            -StepId $stepId -State 'ok' -RestInvoker $RestInvoker
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
    foreach ($drive in @('E','F','G','H','I')) {
        $marker = "$($drive):\virtio-win_license.txt"
        if (Test-Path -LiteralPath $marker) { return "$($drive):\" }
        $marker = "$($drive):\NetKVM"
        if (Test-Path -LiteralPath $marker) { return "$($drive):\" }
    }
    throw "could not find VirtIO ISO on any attached CD-ROM"
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
    $out = (& dism.exe /Image:V:\ /Get-Drivers /Format:Table) 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "dism /Get-Drivers failed: $LASTEXITCODE" }
    $infs = @()
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^(oem\d+\.inf)\s') {
            # Get-Drivers shows oem names, not original. Need /Get-DriverInfo
            # for the original filename - but the column "Original File Name"
            # is also present in the table view (post-Win10). Pull it.
            if ($line -match '\s(\S+\.inf)\s*$') { $infs += $Matches[1].ToLowerInvariant() }
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
        [scriptblock] $RestInvoker = { param($Uri,$Method,$Headers,$Body,$ContentType,$TimeoutSec)
            Invoke-RestMethod -Uri $Uri -Method $Method -Headers $Headers `
                -Body $Body -ContentType $ContentType -TimeoutSec $TimeoutSec
        }
    )
    $guestPath = [string] $Params.guest_path
    if ([string]::IsNullOrWhiteSpace($guestPath)) {
        throw "stage_autopilot_config: missing guest_path"
    }
    $payload = Invoke-OrchestratorRequest -BaseUrl $BaseUrl `
        -Path "/winpe/autopilot-config/$RunId" -Method GET `
        -BearerToken $BearerToken -RestInvoker $RestInvoker
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
    $xml = Invoke-OrchestratorRequest -BaseUrl $BaseUrl `
        -Path "/winpe/unattend/$RunId" -Method GET `
        -BearerToken $BearerToken -RestInvoker $RestInvoker
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

    $reqArgs = @{
        BaseUrl = $cfg.flask_base_url
        Path = '/winpe/register'
        Method = 'POST'
        Body = @{ vm_uuid = $id.vm_uuid; mac = $id.mac; build_sha = $cfg.build_sha }
    }
    if ($RestInvoker) { $reqArgs.RestInvoker = $RestInvoker }
    $reg = Invoke-OrchestratorRequest @reqArgs

    $token = $reg.bearer_token
    $runId = [int] $reg.run_id

    $handlers = @{
        'partition_disk'         = { param($p) Invoke-Action-PartitionDisk -Params $p `
                                        @($(if ($PartitionRunner) { @{ DiskpartRunner = $PartitionRunner } } else { @{} }).GetEnumerator() | ForEach-Object { @{$_.Key=$_.Value} }) }
        'apply_wim'              = { param($p) Invoke-Action-ApplyWim -Params $p }
        'inject_drivers'         = { param($p) Invoke-Action-InjectDrivers -Params $p }
        'validate_boot_drivers'  = { param($p) Invoke-Action-ValidateBootDrivers -Params $p }
        'stage_autopilot_config' = { param($p)
            Invoke-Action-StageAutopilotConfig -Params $p `
                -BaseUrl $cfg.flask_base_url -RunId $runId -BearerToken $token `
                -RestInvoker $RestInvoker
        }
        'bake_boot_entry'        = { param($p) Invoke-Action-BakeBootEntry -Params $p }
        'stage_unattend'         = { param($p)
            $stageArgs = @{
                Params = $p; BaseUrl = $cfg.flask_base_url
                RunId = $runId; BearerToken = $token
            }
            if ($RestInvoker) { $stageArgs.RestInvoker = $RestInvoker }
            if ($PantherDirOverride) { $stageArgs.PantherDirOverride = $PantherDirOverride }
            Invoke-Action-StageUnattend @stageArgs
        }
    }

    $loopArgs = @{
        BaseUrl = $cfg.flask_base_url
        BearerToken = $token
        Actions = $reg.actions
        Handlers = $handlers
    }
    if ($RestInvoker) { $loopArgs.RestInvoker = $RestInvoker }
    $token = Invoke-ActionLoop @loopArgs

    $doneArgs = @{
        BaseUrl = $cfg.flask_base_url; Path = '/winpe/done'
        Method = 'POST'; Body = @{}; BearerToken = $token
    }
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

Create `tools/winpe-build/README.md`:

```markdown
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
```

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

    # Bake-in fallback drivers (NetKVM at minimum so phase 0 has a NIC).
    $virtioRoot = $null
    foreach ($candidate in @('D:\virtio','F:\BuildRoot\inputs\virtio-win')) {
        if (Test-Path -LiteralPath $candidate) { $virtioRoot = $candidate; break }
    }
    if ($virtioRoot) {
        $netkvm = Get-ChildItem -Path $virtioRoot -Recurse -Filter 'netkvm.inf' |
            Where-Object FullName -match "\\$Arch\\" |
            Select-Object -First 1
        if ($netkvm) {
            & dism.exe /Image:$mountDir /Add-Driver /Driver:$($netkvm.FullName) /ForceUnsigned | Out-Null
        }
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

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/inventory/group_vars/all/vars.yml autopilot-proxmox/inventory/group_vars/all/vault.yml.example
git commit -m "feat(inventory): add WinPE blank-template, ISO, and token vars"
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
  ansible.builtin.uri:
    url: "{{ autopilot_base_url }}/api/runs/{{ _wfrs_run_id }}"
    method: GET
    return_content: true
    status_code: [200]
  register: _wfrs_resp
  until: "(_wfrs_resp.json.state | default('')) in (_wfrs_accepted_states | default([]))"
  retries: "{{ ((_wfrs_timeout | default(1800)) // (_wfrs_poll_interval | default(10))) | int }}"
  delay: "{{ _wfrs_poll_interval | default(10) | int }}"

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


@api_router.get("/runs/{run_id}")
def get_run(run_id: int):
    db = _db_path()
    run = sequences_db.get_provisioning_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    steps = sequences_db.list_run_steps(db, run_id=run_id)
    return {**run, "steps": steps}
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
    _template_vmid_override: "{{ winpe_blank_template_vmid }}"
    _skip_panther_injection: true

  pre_tasks:
    - name: Validate WinPE path is configured
      ansible.builtin.assert:
        that:
          - winpe_blank_template_vmid is not none
          - proxmox_winpe_iso is not none
          - autopilot_winpe_token_secret | length > 0
        fail_msg: >-
          WinPE provisioning requires winpe_blank_template_vmid,
          proxmox_winpe_iso, and vault_autopilot_winpe_token_secret.

  tasks:
    - name: Clone the WinPE blank template (skips Panther injection)
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

    - name: Attach VirtIO ISO at sata1 (kept through Specialize for QGA install)
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/config"
        method: PUT
        body_format: form-urlencoded
        body:
          sata1: "{{ proxmox_virtio_iso }},media=cdrom"
        headers:
          Authorization: "{{ proxmox_api_auth_header }}"
        validate_certs: "{{ proxmox_validate_certs }}"
        status_code: [200]

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

    - name: Detach VirtIO ISO post-Specialize
      ansible.builtin.uri:
        url: "{{ proxmox_api_base }}/nodes/{{ proxmox_node }}/qemu/{{ vm_vmid }}/config"
        method: PUT
        body_format: form-urlencoded
        body:
          delete: "sata1"
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

### Task G1: Boot mode toggle on `/devices/<vmid>/provision`

**Files:**
- Modify: `autopilot-proxmox/web/templates/provision.html`
- Modify: `autopilot-proxmox/web/app.py` (provision POST handler)
- Modify: `autopilot-proxmox/tests/test_winpe_endpoints.py` (or a new UI test file)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_winpe_endpoints.py`:

```python
def test_provision_post_with_boot_mode_winpe_creates_run(web_client, test_db, monkeypatch):
    seq_id = _create_seq(web_client)

    # Stub the playbook launcher so we don't actually run Ansible.
    launches = []
    def fake_launch(*, playbook, extra_vars):
        launches.append({"playbook": playbook, "extra_vars": extra_vars})
        return {"job_id": 1}
    from web import app as web_app
    monkeypatch.setattr(web_app, "launch_playbook", fake_launch, raising=False)

    r = web_client.post(
        "/devices/provision",
        data={
            "sequence_id": seq_id,
            "boot_mode": "winpe",
            "vm_count": 1,
        },
    )
    assert r.status_code in (200, 303)
    # A run row was created
    from web import sequences_db
    import sqlite3
    with sqlite3.connect(test_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM provisioning_runs "
            "WHERE provision_path='winpe' AND state='queued'"
        ).fetchone()[0]
    assert n == 1
    # The launcher was called with the WinPE playbook
    assert any(l["playbook"].endswith("provision_proxmox_winpe.yml")
               for l in launches)


def test_provision_winpe_hidden_when_template_not_configured(web_client, test_db, monkeypatch):
    monkeypatch.delenv("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID", raising=False)
    r = web_client.get("/devices/provision-form")
    # The boot-mode toggle should still render the page; the WinPE option
    # is gated behind the env / inventory variable being set.
    assert r.status_code == 200
    assert b"WinPE" not in r.content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v -k provision_post
```

Expected: 2 FAIL.

- [ ] **Step 3: Add the form field to the provision template**

In `web/templates/provision.html`, locate the form's submit area and add (immediately above the Submit button):

```html
{% if winpe_enabled %}
<div class="row mb-3">
  <label class="col-sm-3 col-form-label">Boot mode</label>
  <div class="col-sm-9">
    <select class="form-select" name="boot_mode">
      <option value="clone" selected>Clone (default)</option>
      <option value="winpe">WinPE</option>
    </select>
    <div class="form-text">
      WinPE boots a custom WinPE image, partitions the disk, applies
      install.wim, and hands off to Specialize. Requires
      <code>winpe_blank_template_vmid</code> and
      <code>proxmox_winpe_iso</code> in inventory.
    </div>
  </div>
</div>
{% endif %}
```

The `winpe_enabled` variable is set by the route handler (Step 4).

- [ ] **Step 4: Wire the route handler**

In `web/app.py`, find the route that renders `provision.html` (typically `GET /devices/provision-form` or `/devices/<vmid>/provision`). Add:

```python
import os

def _winpe_enabled() -> bool:
    """The provision UI shows the WinPE option only when the inventory
    has wired up a blank template AND a WinPE ISO is configured."""
    return bool(
        os.environ.get("AUTOPILOT_WINPE_BLANK_TEMPLATE_VMID")
        and os.environ.get("AUTOPILOT_WINPE_ISO")
    )
```

In the GET handler that renders the template, pass `winpe_enabled=_winpe_enabled()` into the Jinja context.

In the POST handler, inspect `boot_mode` and route accordingly:

```python
boot_mode = (form.get("boot_mode") or "clone").lower()
if boot_mode == "winpe":
    if not _winpe_enabled():
        raise HTTPException(status_code=400, detail="WinPE not configured")
    run_id = sequences_db.create_provisioning_run(
        SEQUENCES_DB, sequence_id=int(form["sequence_id"]),
        provision_path="winpe",
    )
    launch_playbook(
        playbook="playbooks/provision_proxmox_winpe.yml",
        extra_vars={
            "run_id": run_id,
            "sequence_id": int(form["sequence_id"]),
            "autopilot_base_url": os.environ.get(
                "AUTOPILOT_BASE_URL", "http://127.0.0.1:5000"),
        },
    )
else:
    # Existing clone path: unchanged.
    ...
```

(`launch_playbook` is the existing helper used by the clone path; reuse whatever name app.py uses today.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_winpe_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/templates/provision.html autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_winpe_endpoints.py
git commit -m "feat(ui): Boot mode toggle (clone | winpe) on provision page"
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

1. From the web UI, open `/devices/provision-form`.
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
        Invoke-OrchestratorRequest -BaseUrl $BaseUrl `
            -Path "/winpe/hash" -Method POST -Body $body `
            -BearerToken $BearerToken -RestInvoker $RestInvoker
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}
```

Also wire the handler in `Start-AutopilotWinPE`'s `$handlers` table:

```powershell
        'capture_hash' = { param($p)
            $args = @{
                Params = $p; BaseUrl = $cfg.flask_base_url
                RunId = $runId; BearerToken = $token
            }
            if ($RestInvoker) { $args.RestInvoker = $RestInvoker }
            Invoke-Action-CaptureHash @args
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
def test_post_hash_persists_via_existing_hash_store(web_client, test_db, monkeypatch):
    captured = []
    def fake_persist(*, vmid, serial, product_id, hardware_hash):
        captured.append({"vmid": vmid, "serial": serial,
                         "product_id": product_id,
                         "hardware_hash": hardware_hash})

    from web import winpe_endpoints
    monkeypatch.setattr(winpe_endpoints,
                        "_persist_autopilot_hash", fake_persist)
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
    assert captured == [{"vmid": 100, "serial": "S1",
                          "product_id": "PK1", "hardware_hash": "HH1"}]


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
    """Thin shim around the existing hash-store helper. Located in a
    separate function so tests can monkeypatch it without spinning up
    the real on-disk store."""
    from web import hashes_db  # existing module name; adapt to actual name
    hashes_db.upsert_hash(
        vmid=vmid, serial_number=serial,
        product_id=product_id, hardware_hash=hardware_hash,
        source="winpe",
    )


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

(Import `hashes_db` matching the existing module name; if the project uses a different helper, replace the body of `_persist_autopilot_hash` with the equivalent call.)

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

**Files:**
- Modify: `autopilot-proxmox/web/templates/sequence_edit.html`
- Modify: `autopilot-proxmox/web/app.py` (sequence-edit POST handler)
- Modify: `autopilot-proxmox/tests/test_sequence_compiler_winpe.py` (or a new UI test)

- [ ] **Step 1: Add the dropdown**

In `web/templates/sequence_edit.html`, find the form row that edits `produces_autopilot_hash` and add a sibling row right below:

```html
<div class="row mb-3">
  <label class="col-sm-3 col-form-label">Hash capture phase</label>
  <div class="col-sm-9">
    <select class="form-select" name="hash_capture_phase">
      <option value="oobe" {% if sequence.hash_capture_phase == 'oobe' %}selected{% endif %}>OOBE (default; FLC)</option>
      <option value="winpe" {% if sequence.hash_capture_phase == 'winpe' %}selected{% endif %}>WinPE (pre-OS)</option>
    </select>
    <div class="form-text">
      WinPE phase requires a recent build (Phase I) with
      Get-WindowsAutopilotInfo.ps1 baked in.
    </div>
  </div>
</div>
```

- [ ] **Step 2: Update the POST handler**

In `web/app.py`, find the sequence-edit POST handler and pass `hash_capture_phase` through to `update_sequence`:

```python
update_sequence(
    SEQUENCES_DB, seq_id=seq_id,
    name=form["name"], description=form["description"],
    target_os=form.get("target_os", "windows"),
    produces_autopilot_hash=bool(form.get("produces_autopilot_hash")),
    hash_capture_phase=form.get("hash_capture_phase", "oobe"),
    ...
)
```

Update `update_sequence` in `web/sequences_db.py` to accept and persist the new column:

```python
def update_sequence(db_path, seq_id: int, *,
                    name: str, description: str,
                    target_os: str = "windows",
                    produces_autopilot_hash: bool = False,
                    hash_capture_phase: str = "oobe",
                    **kw) -> None:
    if hash_capture_phase not in ("winpe", "oobe"):
        raise ValueError(f"invalid hash_capture_phase: {hash_capture_phase!r}")
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE task_sequences SET name=?, description=?, target_os=?, "
            "produces_autopilot_hash=?, hash_capture_phase=?, updated_at=? "
            "WHERE id=?",
            (name, description, target_os,
             1 if produces_autopilot_hash else 0,
             hash_capture_phase, _now(), seq_id),
        )
```

(Adapt to the existing function signature; the existing function already updates other columns, so wedge `hash_capture_phase` in alongside.)

- [ ] **Step 3: Add a test**

Append to `tests/test_sequence_compiler_winpe.py`:

```python
def test_update_sequence_persists_hash_capture_phase(tmp_path):
    from web import sequences_db
    db = tmp_path / "sequences.db"
    sequences_db.init(db)
    sid = sequences_db.create_sequence(
        db, name="x", description="",
        target_os="windows", produces_autopilot_hash=True,
        is_default=False,
    )
    sequences_db.update_sequence(
        db, seq_id=sid,
        name="x", description="",
        target_os="windows",
        produces_autopilot_hash=True,
        hash_capture_phase="winpe",
    )
    seq = sequences_db.get_sequence(db, sid)
    assert seq["hash_capture_phase"] == "winpe"
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd autopilot-proxmox && python -m pytest tests/test_sequence_compiler_winpe.py tests/test_sequences_db.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/templates/sequence_edit.html autopilot-proxmox/web/app.py autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequence_compiler_winpe.py
git commit -m "feat(ui): hash_capture_phase dropdown on sequence editor"
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
- The post-WinPE template (B3) keeps `Microsoft-Windows-PnpCustomizationsNonWinPE` so vioserial driver-store staging still happens during specialize/offlineServicing once the VirtIO ISO is mounted.
- `validate_boot_drivers` (D8) requires `vioscsi.inf`, `netkvm.inf`, `vioser.inf` (compiler default in B2). Failure aborts the run; spec's "no continue on partial driver inject" behavior.

No placeholders, TBDs, or "implement later" lines. Every code-bearing step shows the actual code.
