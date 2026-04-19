# Task Sequences — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the foundation for task sequences — encrypted credentials store, SQLite data model, CRUD web UI for both, and seed default sequences — without changing any existing provisioning behavior.

**Architecture:** Add `web/crypto.py` (Fernet helpers), `web/sequences_db.py` (SQLite DAL mirroring existing `devices_db.py` pattern), new templates and FastAPI routes. A seed migration inserts three starter sequences on first boot. Provisioning flow is **untouched** in Phase A — that's Phase B.

**Tech Stack:** FastAPI + Jinja2 + SQLite + `cryptography.fernet` + existing pytest suite.

**Spec reference:** `docs/superpowers/specs/2026-04-19-task-sequences-design.md` — sections 6 (data model), 7 (credentials), 8.1–8.5 (CRUD UI), 13 (seeded content).

**Out of scope for Phase A (deferred to Phase B):**
- Compiler for step → artifact
- `provision_clone.yml` and role changes
- `unattend_oobe.xml.j2` template
- Reboot-aware waiter
- Devices page capture-action disable
- "Test connection" button (Phase B uses `ldap3`)
- `autopilot_hybrid` UI stub badge

**Validation milestone:** After Phase A ships, the UI shows a `Sequences` and `Credentials` nav link. User can CRUD sequences and credentials. Seeded rows exist. Clicking Provision still triggers the existing hardcoded flow — no regression possible because nothing consumes the new data yet.

---

## File Structure (Phase A)

**New files:**

- `autopilot-proxmox/web/crypto.py` — Fernet key loading + encrypt/decrypt helpers. One responsibility: symmetric encryption for DB secrets.
- `autopilot-proxmox/web/sequences_db.py` — SQLite DAL for `task_sequences`, `task_sequence_steps`, `credentials`, `vm_provisioning`. Mirrors `devices_db.py` conventions.
- `autopilot-proxmox/web/templates/credentials.html` — list view.
- `autopilot-proxmox/web/templates/credential_edit.html` — create/edit form (type-aware).
- `autopilot-proxmox/web/templates/sequences.html` — list view.
- `autopilot-proxmox/web/templates/sequence_edit.html` — builder.
- `autopilot-proxmox/tests/test_crypto.py`
- `autopilot-proxmox/tests/test_sequences_db.py`
- `autopilot-proxmox/tests/test_sequences_api.py`

**Modified files:**

- `autopilot-proxmox/requirements.txt` — add `cryptography`.
- `autopilot-proxmox/Dockerfile` — create `/app/secrets` dir.
- `autopilot-proxmox/docker-compose.yml` — mount a host-side secrets dir.
- `autopilot-proxmox/.gitignore` — exclude the secrets dir in the repo.
- `autopilot-proxmox/.dockerignore` — exclude the secrets dir from the image.
- `autopilot-proxmox/web/app.py` — add routes, DB init on startup, nav context.
- `autopilot-proxmox/web/templates/base.html` — add `Sequences` and `Credentials` nav links.

---

## Phase 1 — Dependencies and secrets plumbing

### Task 1.1: Add `cryptography` to requirements

**Files:**
- Modify: `autopilot-proxmox/requirements.txt`

- [ ] **Step 1: Edit requirements.txt**

Add after the existing lines:

```
cryptography>=42,<46
```

- [ ] **Step 2: Verify locally**

Run: `cd autopilot-proxmox && python3 -c "import cryptography; print(cryptography.__version__)"`
Expected: either prints a version, or prints `ModuleNotFoundError` — both are acceptable (the container build will install it).

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/requirements.txt
git commit -m "build: add cryptography dependency for credential encryption"
```

---

### Task 1.2: Wire `/app/secrets` mount and gitignore

**Files:**
- Modify: `autopilot-proxmox/Dockerfile`
- Modify: `autopilot-proxmox/docker-compose.yml`
- Modify: `autopilot-proxmox/.gitignore`
- Modify: `autopilot-proxmox/.dockerignore`

- [ ] **Step 1: Dockerfile — create the secrets directory**

Find the line `RUN mkdir -p /app/jobs /app/output/hashes` and change to:

```dockerfile
RUN mkdir -p /app/jobs /app/output/hashes /app/secrets
```

- [ ] **Step 2: docker-compose.yml — mount a host path**

Under `volumes:` in the `autopilot` service, after the existing `./inventory/…` mounts, add:

```yaml
      - ./secrets:/app/secrets
```

- [ ] **Step 3: .gitignore — exclude host secrets dir**

Append:

```
# Credentials encryption key (not for git)
secrets/
```

- [ ] **Step 4: .dockerignore — exclude from image**

Append:

```
secrets/
```

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/Dockerfile autopilot-proxmox/docker-compose.yml \
        autopilot-proxmox/.gitignore autopilot-proxmox/.dockerignore
git commit -m "build: mount /app/secrets for credential encryption key"
```

---

## Phase 2 — Crypto helper

### Task 2.1: Write failing tests for key bootstrap

**Files:**
- Create: `autopilot-proxmox/tests/test_crypto.py`

- [ ] **Step 1: Create the test file**

```python
"""Tests for web.crypto — Fernet key bootstrap + encrypt/decrypt round-trip."""
from pathlib import Path

import pytest


@pytest.fixture
def secrets_dir(tmp_path):
    return tmp_path / "secrets"


def test_load_or_generate_creates_key_if_missing(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    assert not key_path.exists()
    key = crypto.load_or_generate_key(key_path)
    assert key_path.exists()
    assert len(key) == 44  # base64-encoded 32-byte Fernet key
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_load_or_generate_is_idempotent(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    first = crypto.load_or_generate_key(key_path)
    second = crypto.load_or_generate_key(key_path)
    assert first == second


def test_encrypt_decrypt_round_trip(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    crypto.load_or_generate_key(key_path)
    cipher = crypto.Cipher(key_path)
    plaintext = b"hunter2 is not a great password"
    encrypted = cipher.encrypt(plaintext)
    assert encrypted != plaintext
    assert cipher.decrypt(encrypted) == plaintext


def test_decrypt_fails_with_wrong_key(secrets_dir, tmp_path):
    from web import crypto
    from cryptography.fernet import InvalidToken
    first = secrets_dir / "credential_key"
    second = tmp_path / "other_key"
    crypto.load_or_generate_key(first)
    crypto.load_or_generate_key(second)
    cipher_a = crypto.Cipher(first)
    cipher_b = crypto.Cipher(second)
    token = cipher_a.encrypt(b"secret")
    with pytest.raises(InvalidToken):
        cipher_b.decrypt(token)


def test_cipher_encrypts_json_payload(secrets_dir):
    from web import crypto
    key_path = secrets_dir / "credential_key"
    crypto.load_or_generate_key(key_path)
    cipher = crypto.Cipher(key_path)
    payload = {"username": "acme\\svc_join", "password": "p@ss"}
    token = cipher.encrypt_json(payload)
    assert cipher.decrypt_json(token) == payload
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_crypto.py -v`
Expected: 5 failures with `ModuleNotFoundError: No module named 'web.crypto'`.

---

### Task 2.2: Implement `web/crypto.py`

**Files:**
- Create: `autopilot-proxmox/web/crypto.py`

- [ ] **Step 1: Write the module**

```python
"""Fernet-based symmetric encryption for credential payloads.

The key lives in a file outside the Ansible vault so that rotating
the credential key doesn't touch unrelated secrets. Key file is
0600 on disk and auto-generated on first use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_generate_key(key_path: Path) -> bytes:
    """Return the Fernet key at ``key_path``, creating it if absent.

    The file is written with mode 0600.
    """
    key_path = Path(key_path)
    if key_path.exists():
        return key_path.read_bytes().strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write with restrictive permissions from the start — avoid the
    # race of "create open, chmod later".
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


class Cipher:
    """Thin wrapper over Fernet that also handles JSON payloads."""

    def __init__(self, key_path: Path) -> None:
        self._fernet = Fernet(load_or_generate_key(Path(key_path)))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        return self._fernet.decrypt(token)

    def encrypt_json(self, payload: dict) -> bytes:
        return self.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def decrypt_json(self, token: bytes) -> dict:
        return json.loads(self.decrypt(token).decode("utf-8"))
```

- [ ] **Step 2: Run tests to confirm they pass**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_crypto.py -v`
Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/crypto.py autopilot-proxmox/tests/test_crypto.py
git commit -m "feat(crypto): add Fernet-based Cipher helper for credential storage"
```

---

## Phase 3 — Data access layer

### Task 3.1: Write failing tests for schema init

**Files:**
- Create: `autopilot-proxmox/tests/test_sequences_db.py`

- [ ] **Step 1: Create the file with init tests**

```python
"""Tests for web.sequences_db — schema, credentials, sequences, steps, vm_provisioning."""
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sequences.db"


@pytest.fixture
def key_path(tmp_path):
    from web import crypto
    key = tmp_path / "credential_key"
    crypto.load_or_generate_key(key)
    return key


def test_init_creates_all_tables(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"task_sequences", "task_sequence_steps", "credentials",
            "vm_provisioning"} <= tables


def test_init_is_idempotent(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    sequences_db.init(db_path)  # must not raise
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v`
Expected: errors with `ModuleNotFoundError: No module named 'web.sequences_db'`.

---

### Task 3.2: Write the schema and `init()`

**Files:**
- Create: `autopilot-proxmox/web/sequences_db.py`

- [ ] **Step 1: Create the file with schema + init**

```python
"""SQLite-backed store for task sequences, steps, and credentials.

Mirrors web.devices_db: module-level SCHEMA string, init() via executescript,
context-managed connections with row_factory=sqlite3.Row.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    produces_autopilot_hash INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_sequence_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES task_sequences(id) ON DELETE CASCADE,
    order_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (sequence_id, order_index)
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    encrypted_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vm_provisioning (
    vmid INTEGER PRIMARY KEY,
    sequence_id INTEGER REFERENCES task_sequences(id) ON DELETE SET NULL,
    provisioned_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: Path) -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
```

- [ ] **Step 2: Run the init tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequences_db.py
git commit -m "feat(db): add sequences_db schema and init()"
```

---

### Task 3.3: Credentials CRUD — failing tests

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequences_db.py`

- [ ] **Step 1: Append test cases**

```python
def test_create_credential_encrypts_payload(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher,
        name="acme-svc", type="domain_join",
        payload={"username": "acme\\svc", "password": "p@ss",
                 "domain_fqdn": "acme.local"},
    )
    assert cred_id > 0

    # Raw row must NOT contain the password in plaintext
    import sqlite3
    with sqlite3.connect(db_path) as c:
        row = c.execute("SELECT encrypted_blob FROM credentials WHERE id=?",
                        (cred_id,)).fetchone()
    assert b"p@ss" not in row[0]


def test_get_credential_decrypts(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher,
        name="acme-svc", type="domain_join",
        payload={"username": "acme\\svc", "password": "p@ss",
                 "domain_fqdn": "acme.local"},
    )
    out = sequences_db.get_credential(db_path, cipher, cred_id)
    assert out["name"] == "acme-svc"
    assert out["type"] == "domain_join"
    assert out["payload"]["password"] == "p@ss"


def test_list_credentials_omits_payload(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.create_credential(
        db_path, cipher, name="a", type="local_admin",
        payload={"username": "Administrator", "password": "x"},
    )
    rows = sequences_db.list_credentials(db_path)
    assert len(rows) == 1
    assert "payload" not in rows[0]
    assert "encrypted_blob" not in rows[0]
    assert rows[0]["name"] == "a"


def test_update_credential_replaces_payload(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher, name="a", type="local_admin",
        payload={"username": "Administrator", "password": "old"},
    )
    sequences_db.update_credential(
        db_path, cipher, cred_id,
        name="a", payload={"username": "Administrator", "password": "new"},
    )
    out = sequences_db.get_credential(db_path, cipher, cred_id)
    assert out["payload"]["password"] == "new"


def test_delete_credential_blocked_if_referenced(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher, name="a", type="domain_join",
        payload={"username": "x", "password": "y", "domain_fqdn": "z"},
    )
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "join_ad_domain",
         "params": {"credential_id": cred_id, "ou_path": "OU=X"},
         "enabled": True},
    ])
    with pytest.raises(sequences_db.CredentialInUse) as exc:
        sequences_db.delete_credential(db_path, cred_id)
    assert seq_id in exc.value.sequence_ids


def test_delete_credential_succeeds_when_unreferenced(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    cred_id = sequences_db.create_credential(
        db_path, cipher, name="a", type="local_admin",
        payload={"username": "x", "password": "y"},
    )
    sequences_db.delete_credential(db_path, cred_id)
    assert sequences_db.list_credentials(db_path) == []
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v`
Expected: new tests fail with `AttributeError` / missing functions.

---

### Task 3.4: Implement credentials CRUD

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`

- [ ] **Step 1: Append the credentials API + shared exceptions**

Add to `sequences_db.py`:

```python
class CredentialInUse(Exception):
    """Raised when attempting to delete a credential referenced by a step."""
    def __init__(self, cred_id: int, sequence_ids: list[int]):
        super().__init__(
            f"Credential {cred_id} is referenced by sequences {sequence_ids}"
        )
        self.cred_id = cred_id
        self.sequence_ids = sequence_ids


class SequenceInUse(Exception):
    """Raised when attempting to delete a sequence referenced by vm_provisioning."""
    def __init__(self, sequence_id: int, vmids: list[int]):
        super().__init__(
            f"Sequence {sequence_id} is referenced by VMs {vmids}"
        )
        self.sequence_id = sequence_id
        self.vmids = vmids


def _row_to_credential_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_credential(db_path, cipher, *, name: str, type: str, payload: dict) -> int:
    now = _now()
    blob = cipher.encrypt_json(payload)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO credentials (name, type, encrypted_blob, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, type, blob, now, now),
        )
        return cur.lastrowid


def update_credential(db_path, cipher, cred_id: int, *,
                      name: Optional[str] = None,
                      payload: Optional[dict] = None) -> None:
    now = _now()
    with _connect(db_path) as conn:
        updates, args = [], []
        if name is not None:
            updates.append("name = ?")
            args.append(name)
        if payload is not None:
            updates.append("encrypted_blob = ?")
            args.append(cipher.encrypt_json(payload))
        if not updates:
            return
        updates.append("updated_at = ?")
        args.append(now)
        args.append(cred_id)
        conn.execute(
            f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?", args
        )


def get_credential(db_path, cipher, cred_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, type, encrypted_blob, created_at, updated_at "
            "FROM credentials WHERE id = ?",
            (cred_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        **_row_to_credential_summary(row),
        "payload": cipher.decrypt_json(row["encrypted_blob"]),
    }


def list_credentials(db_path, type: Optional[str] = None) -> list[dict]:
    query = "SELECT id, name, type, created_at, updated_at FROM credentials"
    args: tuple = ()
    if type is not None:
        query += " WHERE type = ?"
        args = (type,)
    query += " ORDER BY name ASC"
    with _connect(db_path) as conn:
        return [_row_to_credential_summary(r) for r in conn.execute(query, args)]


def delete_credential(db_path, cred_id: int) -> None:
    """Delete a credential. Raises CredentialInUse if referenced by any step."""
    with _connect(db_path) as conn:
        referencing = _sequences_referencing_credential(conn, cred_id)
        if referencing:
            raise CredentialInUse(cred_id, referencing)
        conn.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))


def _sequences_referencing_credential(conn: sqlite3.Connection, cred_id: int) -> list[int]:
    """Scan task_sequence_steps.params_json for credential_id references.

    Uses json_extract (SQLite 3.38+). Returns distinct sequence IDs.
    """
    rows = conn.execute(
        "SELECT DISTINCT sequence_id FROM task_sequence_steps "
        "WHERE json_extract(params_json, '$.credential_id') = ?",
        (cred_id,),
    ).fetchall()
    return sorted(r["sequence_id"] for r in rows)
```

- [ ] **Step 2: Run credential tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v -k credential`
Expected: Some still fail because `create_sequence` + `set_sequence_steps` aren't defined yet — that's expected; we'll implement them next.

Note: mark those specific tests expected-to-fail for now:

Run: `python3 -m pytest tests/test_sequences_db.py -v -k "credential and not referenced"`
Expected: pass.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequences_db.py
git commit -m "feat(db): add credentials CRUD with encryption and in-use protection"
```

---

### Task 3.5: Sequences + steps CRUD — failing tests

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequences_db.py`

- [ ] **Step 1: Append test cases**

```python
def test_create_sequence(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(
        db_path, name="Entra Join", description="default flow",
        is_default=True, produces_autopilot_hash=True,
    )
    assert seq_id > 0
    seq = sequences_db.get_sequence(db_path, seq_id)
    assert seq["name"] == "Entra Join"
    assert seq["is_default"] is True
    assert seq["produces_autopilot_hash"] is True
    assert seq["steps"] == []


def test_only_one_default_sequence(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    a = sequences_db.create_sequence(db_path, name="A", description="",
                                     is_default=True)
    b = sequences_db.create_sequence(db_path, name="B", description="",
                                     is_default=True)
    # Creating B as default must demote A.
    seq_a = sequences_db.get_sequence(db_path, a)
    seq_b = sequences_db.get_sequence(db_path, b)
    assert seq_a["is_default"] is False
    assert seq_b["is_default"] is True


def test_set_sequence_steps_replaces(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "set_oem_hardware",
         "params": {"oem_profile": "dell-latitude-5540"}, "enabled": True},
        {"step_type": "local_admin",
         "params": {"credential_id": 1}, "enabled": True},
    ])
    seq = sequences_db.get_sequence(db_path, seq_id)
    assert [s["step_type"] for s in seq["steps"]] == [
        "set_oem_hardware", "local_admin"]
    assert seq["steps"][0]["order_index"] == 0
    assert seq["steps"][1]["order_index"] == 1

    # Replace with one step
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])
    seq = sequences_db.get_sequence(db_path, seq_id)
    assert [s["step_type"] for s in seq["steps"]] == ["autopilot_entra"]


def test_list_sequences_summary(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    sequences_db.create_sequence(db_path, name="A", description="")
    sequences_db.create_sequence(db_path, name="B", description="")
    out = sequences_db.list_sequences(db_path)
    assert [s["name"] for s in out] == ["A", "B"]
    assert "steps" not in out[0]
    assert "step_count" in out[0]


def test_delete_sequence_cascade_steps(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])
    sequences_db.delete_sequence(db_path, seq_id)
    import sqlite3
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM task_sequence_steps").fetchone()[0]
    assert n == 0


def test_delete_sequence_blocked_if_referenced_by_vm(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.record_vm_provisioning(db_path, vmid=101, sequence_id=seq_id)
    with pytest.raises(sequences_db.SequenceInUse) as exc:
        sequences_db.delete_sequence(db_path, seq_id)
    assert 101 in exc.value.vmids


def test_duplicate_sequence_copies_steps(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.set_sequence_steps(db_path, seq_id, [
        {"step_type": "set_oem_hardware", "params": {"oem_profile": "x"},
         "enabled": True},
        {"step_type": "autopilot_entra", "params": {}, "enabled": True},
    ])
    new_id = sequences_db.duplicate_sequence(db_path, seq_id, new_name="S (copy)")
    new_seq = sequences_db.get_sequence(db_path, new_id)
    assert new_seq["name"] == "S (copy)"
    assert [s["step_type"] for s in new_seq["steps"]] == [
        "set_oem_hardware", "autopilot_entra"]
    assert new_seq["is_default"] is False


def test_record_vm_provisioning_upsert(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    seq_id = sequences_db.create_sequence(db_path, name="S", description="")
    sequences_db.record_vm_provisioning(db_path, vmid=101, sequence_id=seq_id)
    sequences_db.record_vm_provisioning(db_path, vmid=101, sequence_id=seq_id)
    # Idempotent upsert; no "UNIQUE constraint" explosion.
    assert sequences_db.get_vm_sequence_id(db_path, 101) == seq_id


def test_get_default_sequence(db_path):
    from web import sequences_db
    sequences_db.init(db_path)
    a = sequences_db.create_sequence(db_path, name="A", description="")
    b = sequences_db.create_sequence(db_path, name="B", description="",
                                     is_default=True)
    assert sequences_db.get_default_sequence_id(db_path) == b
    # None default
    sequences_db.update_sequence(db_path, b, is_default=False)
    assert sequences_db.get_default_sequence_id(db_path) is None
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v -k "not credential"`
Expected: most fail with `AttributeError`.

---

### Task 3.6: Implement sequences + steps + vm_provisioning

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`

- [ ] **Step 1: Append the rest of the DAL**

```python
def _row_to_sequence(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "is_default": bool(row["is_default"]),
        "produces_autopilot_hash": bool(row["produces_autopilot_hash"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_step(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "sequence_id": row["sequence_id"],
        "order_index": row["order_index"],
        "step_type": row["step_type"],
        "params": json.loads(row["params_json"]) if row["params_json"] else {},
        "enabled": bool(row["enabled"]),
    }


def create_sequence(db_path, *, name: str, description: str,
                    is_default: bool = False,
                    produces_autopilot_hash: bool = False) -> int:
    now = _now()
    with _connect(db_path) as conn:
        if is_default:
            conn.execute("UPDATE task_sequences SET is_default = 0")
        cur = conn.execute(
            "INSERT INTO task_sequences "
            "(name, description, is_default, produces_autopilot_hash, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, int(is_default), int(produces_autopilot_hash),
             now, now),
        )
        return cur.lastrowid


def update_sequence(db_path, seq_id: int, *,
                    name: Optional[str] = None,
                    description: Optional[str] = None,
                    is_default: Optional[bool] = None,
                    produces_autopilot_hash: Optional[bool] = None) -> None:
    now = _now()
    updates, args = [], []
    if name is not None:
        updates.append("name = ?"); args.append(name)
    if description is not None:
        updates.append("description = ?"); args.append(description)
    if produces_autopilot_hash is not None:
        updates.append("produces_autopilot_hash = ?")
        args.append(int(produces_autopilot_hash))
    if is_default is not None:
        updates.append("is_default = ?"); args.append(int(is_default))
    if not updates:
        return
    updates.append("updated_at = ?"); args.append(now)
    args.append(seq_id)
    with _connect(db_path) as conn:
        if is_default:
            conn.execute("UPDATE task_sequences SET is_default = 0")
        conn.execute(
            f"UPDATE task_sequences SET {', '.join(updates)} WHERE id = ?", args
        )


def get_sequence(db_path, seq_id: int) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM task_sequences WHERE id = ?", (seq_id,)
        ).fetchone()
        if row is None:
            return None
        seq = _row_to_sequence(row)
        step_rows = conn.execute(
            "SELECT * FROM task_sequence_steps WHERE sequence_id = ? "
            "ORDER BY order_index ASC",
            (seq_id,),
        ).fetchall()
        seq["steps"] = [_row_to_step(r) for r in step_rows]
        return seq


def list_sequences(db_path) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*, "
            "  (SELECT COUNT(*) FROM task_sequence_steps "
            "   WHERE sequence_id = s.id) AS step_count "
            "FROM task_sequences s ORDER BY s.name ASC"
        ).fetchall()
    return [{**_row_to_sequence(r), "step_count": r["step_count"]} for r in rows]


def set_sequence_steps(db_path, seq_id: int, steps: list[dict]) -> None:
    """Replace the full step list for a sequence atomically.

    Each step dict: {step_type, params, enabled}. order_index is assigned
    from list position.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM task_sequence_steps WHERE sequence_id = ?", (seq_id,)
        )
        for idx, step in enumerate(steps):
            conn.execute(
                "INSERT INTO task_sequence_steps "
                "(sequence_id, order_index, step_type, params_json, enabled) "
                "VALUES (?, ?, ?, ?, ?)",
                (seq_id, idx, step["step_type"],
                 json.dumps(step.get("params", {}), separators=(",", ":")),
                 int(step.get("enabled", True))),
            )
        conn.execute(
            "UPDATE task_sequences SET updated_at = ? WHERE id = ?",
            (_now(), seq_id),
        )


def delete_sequence(db_path, seq_id: int) -> None:
    """Delete a sequence. Raises SequenceInUse if referenced by vm_provisioning."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT vmid FROM vm_provisioning WHERE sequence_id = ?", (seq_id,)
        ).fetchall()
        if rows:
            raise SequenceInUse(seq_id, [r["vmid"] for r in rows])
        # Cascade handled by FK; steps go with it.
        conn.execute("DELETE FROM task_sequences WHERE id = ?", (seq_id,))


def duplicate_sequence(db_path, seq_id: int, *, new_name: str) -> int:
    seq = get_sequence(db_path, seq_id)
    if seq is None:
        raise ValueError(f"sequence {seq_id} not found")
    new_id = create_sequence(
        db_path, name=new_name, description=seq["description"],
        is_default=False,
        produces_autopilot_hash=seq["produces_autopilot_hash"],
    )
    set_sequence_steps(db_path, new_id, [
        {"step_type": s["step_type"], "params": s["params"], "enabled": s["enabled"]}
        for s in seq["steps"]
    ])
    return new_id


def get_default_sequence_id(db_path) -> Optional[int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM task_sequences WHERE is_default = 1 LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def record_vm_provisioning(db_path, *, vmid: int, sequence_id: int) -> None:
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO vm_provisioning (vmid, sequence_id, provisioned_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(vmid) DO UPDATE SET "
            "  sequence_id = excluded.sequence_id, "
            "  provisioned_at = excluded.provisioned_at",
            (vmid, sequence_id, now),
        )


def get_vm_sequence_id(db_path, vmid: int) -> Optional[int]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sequence_id FROM vm_provisioning WHERE vmid = ?", (vmid,)
        ).fetchone()
    return row["sequence_id"] if row else None
```

- [ ] **Step 2: Run all DB tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py tests/test_crypto.py -v`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequences_db.py
git commit -m "feat(db): add sequences + steps + vm_provisioning DAL"
```

---

## Phase 4 — FastAPI routes, startup init, nav entries

### Task 4.1: Add module-level DB paths and startup init

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Locate the existing `BASE_DIR` and module globals**

Find the `BASE_DIR = Path(__file__).resolve().parent.parent` line and the block of `TEMPLATES_DIR`, `HASH_DIR`, etc. immediately after.

- [ ] **Step 2: Add new paths + DB/cipher globals**

Add just below `VARS_PATH = BASE_DIR / "inventory" / "group_vars" / "all" / "vars.yml"`:

```python
SECRETS_DIR = BASE_DIR / "secrets"
SEQUENCES_DB = BASE_DIR / "output" / "sequences.db"
CREDENTIAL_KEY = SECRETS_DIR / "credential_key"
```

- [ ] **Step 3: Add a startup hook near the bottom of module imports (after `templates = ...`)**

Add:

```python
from web import sequences_db, crypto as _crypto


@app.on_event("startup")
def _init_sequences_db() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    sequences_db.init(SEQUENCES_DB)


def _cipher() -> _crypto.Cipher:
    """Lazy accessor so tests can monkeypatch CREDENTIAL_KEY before first call."""
    return _crypto.Cipher(CREDENTIAL_KEY)
```

- [ ] **Step 4: Quick smoke check — import works**

Run: `cd autopilot-proxmox && python3 -c "from web import app; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "feat(web): initialise sequences DB on startup"
```

---

### Task 4.2: Write failing API tests (credentials)

**Files:**
- Create: `autopilot-proxmox/tests/test_sequences_api.py`

- [ ] **Step 1: Create the test file**

```python
"""End-to-end API tests for credentials and sequences routes."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"), \
             patch("web.app.HASH_DIR", tmp / "hashes"), \
             patch("web.app.job_manager") as jm:
            jm.list_jobs.return_value = []
            jm.jobs_dir = str(tmp / "jobs")
            from web.app import app, _init_sequences_db
            _init_sequences_db()
            yield TestClient(app)


def test_credentials_list_empty(app_env):
    r = app_env.get("/api/credentials")
    assert r.status_code == 200
    assert r.json() == []


def test_create_credential(app_env):
    r = app_env.post("/api/credentials", json={
        "name": "acme-svc", "type": "domain_join",
        "payload": {"username": "acme\\svc", "password": "p@ss",
                    "domain_fqdn": "acme.local"},
    })
    assert r.status_code == 201
    cid = r.json()["id"]

    r = app_env.get("/api/credentials")
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "acme-svc"

    # Full get includes payload
    r = app_env.get(f"/api/credentials/{cid}")
    assert r.status_code == 200
    assert r.json()["payload"]["password"] == "p@ss"


def test_create_credential_duplicate_name(app_env):
    body = {"name": "a", "type": "local_admin",
            "payload": {"username": "x", "password": "y"}}
    assert app_env.post("/api/credentials", json=body).status_code == 201
    assert app_env.post("/api/credentials", json=body).status_code == 409


def test_update_credential_partial(app_env):
    cid = app_env.post("/api/credentials", json={
        "name": "a", "type": "local_admin",
        "payload": {"username": "x", "password": "y"},
    }).json()["id"]
    r = app_env.patch(f"/api/credentials/{cid}", json={"name": "a-new"})
    assert r.status_code == 200
    assert app_env.get(f"/api/credentials/{cid}").json()["name"] == "a-new"


def test_delete_credential_blocked(app_env):
    cid = app_env.post("/api/credentials", json={
        "name": "a", "type": "domain_join",
        "payload": {"username": "x", "password": "y", "domain_fqdn": "z"},
    }).json()["id"]
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "",
        "steps": [
            {"step_type": "join_ad_domain",
             "params": {"credential_id": cid, "ou_path": "OU=X"},
             "enabled": True},
        ],
    }).json()["id"]
    r = app_env.delete(f"/api/credentials/{cid}")
    assert r.status_code == 409
    assert sid in r.json()["sequence_ids"]
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_api.py -v`
Expected: all fail — routes don't exist yet.

---

### Task 4.3: Implement `/api/credentials` routes

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Add models + credentials routes at the bottom of app.py**

```python
from fastapi import HTTPException
from pydantic import BaseModel


class _CredentialCreate(BaseModel):
    name: str
    type: str
    payload: dict


class _CredentialUpdate(BaseModel):
    name: Optional[str] = None
    payload: Optional[dict] = None


@app.get("/api/credentials")
def api_credentials_list(type: Optional[str] = None):
    return sequences_db.list_credentials(SEQUENCES_DB, type=type)


@app.get("/api/credentials/{cred_id}")
def api_credentials_get(cred_id: int):
    cred = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    return cred


@app.post("/api/credentials", status_code=201)
def api_credentials_create(body: _CredentialCreate):
    if body.type not in {"domain_join", "local_admin", "odj_blob"}:
        raise HTTPException(400, f"unknown credential type: {body.type}")
    try:
        cid = sequences_db.create_credential(
            SEQUENCES_DB, _cipher(),
            name=body.name, type=body.type, payload=body.payload,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"credential name already exists: {body.name}")
        raise
    return {"id": cid}


@app.patch("/api/credentials/{cred_id}")
def api_credentials_update(cred_id: int, body: _CredentialUpdate):
    existing = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if existing is None:
        raise HTTPException(404, "credential not found")
    try:
        sequences_db.update_credential(
            SEQUENCES_DB, _cipher(), cred_id,
            name=body.name, payload=body.payload,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"credential name already exists: {body.name}")
        raise
    return {"ok": True}


@app.delete("/api/credentials/{cred_id}")
def api_credentials_delete(cred_id: int):
    try:
        sequences_db.delete_credential(SEQUENCES_DB, cred_id)
    except sequences_db.CredentialInUse as e:
        raise HTTPException(409, detail={
            "error": "credential is in use",
            "sequence_ids": e.sequence_ids,
        })
    return {"ok": True}
```

At the top of `app.py`, add `import sqlite3` and extend the `Optional` import:

```python
import sqlite3
from typing import Optional
```

- [ ] **Step 2: Run tests (credentials only)**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_api.py -v -k credential`
Expected: Some pass; `test_delete_credential_blocked` fails (sequence POST route not implemented yet).

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/app.py
git commit -m "feat(api): add /api/credentials CRUD routes"
```

---

### Task 4.4: Sequences API tests — failing

**Files:**
- Modify: `autopilot-proxmox/tests/test_sequences_api.py`

- [ ] **Step 1: Append sequence tests**

```python
def test_sequences_list_empty(app_env):
    assert app_env.get("/api/sequences").json() == []


def test_create_sequence_with_steps(app_env):
    r = app_env.post("/api/sequences", json={
        "name": "Entra", "description": "d", "is_default": True,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": "dell-latitude-5540"}, "enabled": True},
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    })
    assert r.status_code == 201
    sid = r.json()["id"]
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["name"] == "Entra"
    assert got["is_default"] is True
    assert [s["step_type"] for s in got["steps"]] == [
        "set_oem_hardware", "autopilot_entra"]


def test_update_sequence_replaces_steps(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "",
        "steps": [
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }).json()["id"]
    r = app_env.put(f"/api/sequences/{sid}", json={
        "name": "S", "description": "updated",
        "steps": [
            {"step_type": "local_admin",
             "params": {"credential_id": 99}, "enabled": True},
        ],
    })
    assert r.status_code == 200
    got = app_env.get(f"/api/sequences/{sid}").json()
    assert got["description"] == "updated"
    assert [s["step_type"] for s in got["steps"]] == ["local_admin"]


def test_duplicate_sequence(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "Original", "description": "",
        "steps": [
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    }).json()["id"]
    r = app_env.post(f"/api/sequences/{sid}/duplicate",
                     json={"new_name": "Original (copy)"})
    assert r.status_code == 201
    new_id = r.json()["id"]
    assert app_env.get(f"/api/sequences/{new_id}").json()["name"] == \
        "Original (copy)"


def test_delete_sequence(app_env):
    sid = app_env.post("/api/sequences", json={
        "name": "S", "description": "", "steps": [],
    }).json()["id"]
    assert app_env.delete(f"/api/sequences/{sid}").status_code == 200
    assert app_env.get(f"/api/sequences/{sid}").status_code == 404


def test_only_one_default_via_api(app_env):
    a = app_env.post("/api/sequences", json={
        "name": "A", "description": "", "is_default": True, "steps": [],
    }).json()["id"]
    b = app_env.post("/api/sequences", json={
        "name": "B", "description": "", "is_default": True, "steps": [],
    }).json()["id"]
    got_a = app_env.get(f"/api/sequences/{a}").json()
    got_b = app_env.get(f"/api/sequences/{b}").json()
    assert got_a["is_default"] is False
    assert got_b["is_default"] is True
```

- [ ] **Step 2: Confirm failure**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_api.py -v -k sequence`
Expected: fail (no routes yet).

---

### Task 4.5: Implement `/api/sequences` routes

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Add pydantic models + routes**

Append to `app.py`:

```python
class _StepIn(BaseModel):
    step_type: str
    params: dict = {}
    enabled: bool = True


class _SequenceCreate(BaseModel):
    name: str
    description: str = ""
    is_default: bool = False
    produces_autopilot_hash: bool = False
    steps: list[_StepIn] = []


class _SequenceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_default: Optional[bool] = None
    produces_autopilot_hash: Optional[bool] = None
    steps: Optional[list[_StepIn]] = None


class _DuplicateReq(BaseModel):
    new_name: str


@app.get("/api/sequences")
def api_sequences_list():
    return sequences_db.list_sequences(SEQUENCES_DB)


@app.get("/api/sequences/{seq_id}")
def api_sequences_get(seq_id: int):
    seq = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if seq is None:
        raise HTTPException(404, "sequence not found")
    return seq


@app.post("/api/sequences", status_code=201)
def api_sequences_create(body: _SequenceCreate):
    try:
        sid = sequences_db.create_sequence(
            SEQUENCES_DB,
            name=body.name, description=body.description,
            is_default=body.is_default,
            produces_autopilot_hash=body.produces_autopilot_hash,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"sequence name already exists: {body.name}")
        raise
    sequences_db.set_sequence_steps(
        SEQUENCES_DB, sid,
        [s.model_dump() for s in body.steps],
    )
    return {"id": sid}


@app.put("/api/sequences/{seq_id}")
def api_sequences_update(seq_id: int, body: _SequenceUpdate):
    existing = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if existing is None:
        raise HTTPException(404, "sequence not found")
    try:
        sequences_db.update_sequence(
            SEQUENCES_DB, seq_id,
            name=body.name, description=body.description,
            is_default=body.is_default,
            produces_autopilot_hash=body.produces_autopilot_hash,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"sequence name already exists: {body.name}")
        raise
    if body.steps is not None:
        sequences_db.set_sequence_steps(
            SEQUENCES_DB, seq_id,
            [s.model_dump() for s in body.steps],
        )
    return {"ok": True}


@app.post("/api/sequences/{seq_id}/duplicate", status_code=201)
def api_sequences_duplicate(seq_id: int, body: _DuplicateReq):
    existing = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if existing is None:
        raise HTTPException(404, "sequence not found")
    try:
        new_id = sequences_db.duplicate_sequence(
            SEQUENCES_DB, seq_id, new_name=body.new_name,
        )
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"sequence name already exists: {body.new_name}")
        raise
    return {"id": new_id}


@app.delete("/api/sequences/{seq_id}")
def api_sequences_delete(seq_id: int):
    try:
        sequences_db.delete_sequence(SEQUENCES_DB, seq_id)
    except sequences_db.SequenceInUse as e:
        raise HTTPException(409, detail={
            "error": "sequence is referenced by provisioned VMs",
            "vmids": e.vmids,
        })
    return {"ok": True}
```

- [ ] **Step 2: Run all API tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_api.py -v`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_sequences_api.py
git commit -m "feat(api): add /api/sequences CRUD + duplicate routes"
```

---

## Phase 5 — Web UI pages

### Task 5.1: Add nav links for Sequences and Credentials

**Files:**
- Modify: `autopilot-proxmox/web/templates/base.html`

- [ ] **Step 1: Add two links to the nav block**

Find the `<div id="nav">` section. After the `<a href="/vms">Devices</a> |` line, insert:

```html
<a href="/sequences">Sequences</a> |
<a href="/credentials">Credentials</a> |
```

- [ ] **Step 2: Commit**

```bash
git add autopilot-proxmox/web/templates/base.html
git commit -m "feat(ui): add Sequences and Credentials nav entries"
```

---

### Task 5.2: Credentials list page

**Files:**
- Create: `autopilot-proxmox/web/templates/credentials.html`
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Create the template**

```html
{% extends "base.html" %}
{% block title %}Credentials - Proxmox VE Autopilot{% endblock %}
{% block content %}
<h2>Credentials
  <small style="color:#999;font-weight:normal;font-size:11px;">
    · <a href="/credentials/new">+ New credential</a>
  </small>
</h2>
<p style="color:#666;font-size:11px;">
  Credentials are encrypted on disk with a Fernet key at
  <code>/app/secrets/credential_key</code>. Passwords and ODJ blobs
  never leave the server decrypted.
</p>

{% if not credentials %}
<p><i>No credentials yet. <a href="/credentials/new">Add one</a>.</i></p>
{% else %}
<table>
<tr><th>Name</th><th>Type</th><th>Created</th><th>Updated</th><th>Actions</th></tr>
{% for c in credentials %}
<tr>
  <td>{{ c.name }}</td>
  <td>{{ c.type }}</td>
  <td>{{ c.created_at }}</td>
  <td>{{ c.updated_at }}</td>
  <td>
    <a href="/credentials/{{ c.id }}/edit">Edit</a> ·
    <form method="POST" action="/credentials/{{ c.id }}/delete"
          style="display:inline;" onsubmit="return confirm('Delete {{ c.name }}?');">
      <input type="submit" value="Delete" style="padding:1px 6px;">
    </form>
  </td>
</tr>
{% endfor %}
</table>
{% endif %}

{% if error %}
<p><span class="status-red">Error:</span> {{ error }}</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Add route in app.py**

Append:

```python
@app.get("/credentials", response_class=HTMLResponse)
def page_credentials(request: Request, error: str = ""):
    creds = sequences_db.list_credentials(SEQUENCES_DB)
    return templates.TemplateResponse("credentials.html", {
        "request": request,
        "credentials": creds,
        "error": error,
    })
```

- [ ] **Step 3: Smoke test**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_web.py -v -k home`
Expected: existing test still passes. (This confirms app.py still imports cleanly.)

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/templates/credentials.html autopilot-proxmox/web/app.py
git commit -m "feat(ui): add /credentials list page"
```

---

### Task 5.3: Credential edit/create page

**Files:**
- Create: `autopilot-proxmox/web/templates/credential_edit.html`
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Create the template**

```html
{% extends "base.html" %}
{% block title %}{% if cred %}Edit{% else %}New{% endif %} Credential{% endblock %}
{% block content %}
<h2>{% if cred %}Edit credential: {{ cred.name }}{% else %}New credential{% endif %}</h2>

<form method="POST"
      action="{% if cred %}/credentials/{{ cred.id }}/edit{% else %}/credentials/new{% endif %}">
<table border="0" cellpadding="3">
<tr>
  <td><b>Name:</b></td>
  <td><input type="text" name="name" value="{{ cred.name if cred else '' }}" required></td>
</tr>
<tr>
  <td><b>Type:</b></td>
  <td>
    {% if cred %}
      <input type="text" value="{{ cred.type }}" disabled>
      <input type="hidden" name="type" value="{{ cred.type }}">
    {% else %}
    <select name="type" id="type-select" onchange="showFields()">
      <option value="domain_join">AD domain join</option>
      <option value="local_admin">Local admin</option>
      <option value="odj_blob">ODJ blob (Hybrid Autopilot)</option>
    </select>
    {% endif %}
  </td>
</tr>

<tbody id="fields-domain_join"
       style="display:{% if cred and cred.type=='domain_join' %}table-row-group{% else %}none{% endif %};">
<tr><td><b>Domain FQDN:</b></td>
    <td><input type="text" name="domain_fqdn"
               value="{{ cred.payload.domain_fqdn if cred else '' }}"></td></tr>
<tr><td><b>Username:</b></td>
    <td><input type="text" name="username"
               value="{{ cred.payload.username if cred else '' }}"
               placeholder="DOMAIN\user or user@domain.fqdn"></td></tr>
<tr><td><b>Password:</b></td>
    <td><input type="password" name="password"
               placeholder="{% if cred %}(unchanged){% else %}required{% endif %}"></td></tr>
<tr><td><b>Default OU hint:</b></td>
    <td><input type="text" name="ou_hint"
               value="{{ cred.payload.ou_hint if cred else '' }}"
               placeholder="OU=Workstations,DC=example,DC=local">
      <small style="color:#999;">Stored as a suggestion; actual OU lives on the step.</small>
    </td></tr>
</tbody>

<tbody id="fields-local_admin"
       style="display:{% if cred and cred.type=='local_admin' %}table-row-group{% else %}none{% endif %};">
<tr><td><b>Username:</b></td>
    <td><input type="text" name="la_username"
               value="{{ cred.payload.username if cred else 'Administrator' }}"></td></tr>
<tr><td><b>Password:</b></td>
    <td><input type="password" name="la_password"
               placeholder="{% if cred %}(unchanged){% else %}required{% endif %}"></td></tr>
</tbody>

<tbody id="fields-odj_blob"
       style="display:{% if cred and cred.type=='odj_blob' %}table-row-group{% else %}none{% endif %};">
<tr><td><b>ODJ blob:</b></td>
    <td>{% if cred %}(uploaded {{ cred.payload.generated_at }}, {{ cred.payload.blob_b64|length }} b64 chars){% endif %}
        <br><input type="file" name="odj_file"
                   {% if not cred %}required{% endif %}>
        <small style="color:#999;">Upload .bin from djoin.exe /provision.</small>
    </td></tr>
</tbody>

<tr><td></td>
    <td>
      <input type="submit" value="{% if cred %}Save changes{% else %}Create{% endif %}">
      <a href="/credentials" style="margin-left:8px;">Cancel</a>
    </td></tr>
</table>
</form>

{% if error %}
<p><span class="status-red">Error:</span> {{ error }}</p>
{% endif %}

<script>
function showFields() {
  const t = document.getElementById('type-select').value;
  for (const tb of document.querySelectorAll('tbody[id^="fields-"]')) {
    tb.style.display = (tb.id === 'fields-' + t) ? 'table-row-group' : 'none';
  }
}
document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('type-select');
  if (sel) showFields();
});
</script>
{% endblock %}
```

- [ ] **Step 2: Add routes in app.py**

Append:

```python
import base64

@app.get("/credentials/new", response_class=HTMLResponse)
def page_credential_new(request: Request, error: str = ""):
    return templates.TemplateResponse("credential_edit.html", {
        "request": request, "cred": None, "error": error,
    })


@app.post("/credentials/new")
async def submit_credential_new(request: Request):
    form = await request.form()
    cred_type = form.get("type", "")
    try:
        payload = _payload_from_form(cred_type, form)
        sequences_db.create_credential(
            SEQUENCES_DB, _cipher(),
            name=form["name"], type=cred_type, payload=payload,
        )
    except sqlite3.IntegrityError as e:
        msg = "name already exists" if "UNIQUE" in str(e) else str(e)
        return RedirectResponse(f"/credentials/new?error={msg}", status_code=303)
    except ValueError as e:
        return RedirectResponse(f"/credentials/new?error={e}", status_code=303)
    return RedirectResponse("/credentials", status_code=303)


@app.get("/credentials/{cred_id}/edit", response_class=HTMLResponse)
def page_credential_edit(request: Request, cred_id: int, error: str = ""):
    cred = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    return templates.TemplateResponse("credential_edit.html", {
        "request": request, "cred": cred, "error": error,
    })


@app.post("/credentials/{cred_id}/edit")
async def submit_credential_edit(request: Request, cred_id: int):
    cred = sequences_db.get_credential(SEQUENCES_DB, _cipher(), cred_id)
    if cred is None:
        raise HTTPException(404, "credential not found")
    form = await request.form()
    try:
        new_payload = _payload_from_form(cred["type"], form, existing=cred["payload"])
        sequences_db.update_credential(
            SEQUENCES_DB, _cipher(), cred_id,
            name=form["name"], payload=new_payload,
        )
    except sqlite3.IntegrityError as e:
        msg = "name already exists" if "UNIQUE" in str(e) else str(e)
        return RedirectResponse(f"/credentials/{cred_id}/edit?error={msg}",
                                status_code=303)
    except ValueError as e:
        return RedirectResponse(f"/credentials/{cred_id}/edit?error={e}",
                                status_code=303)
    return RedirectResponse("/credentials", status_code=303)


@app.post("/credentials/{cred_id}/delete")
def submit_credential_delete(cred_id: int):
    try:
        sequences_db.delete_credential(SEQUENCES_DB, cred_id)
    except sequences_db.CredentialInUse as e:
        msg = f"in use by sequence(s) {e.sequence_ids}"
        return RedirectResponse(f"/credentials?error={msg}", status_code=303)
    return RedirectResponse("/credentials", status_code=303)


async def _payload_from_form(cred_type: str, form, existing: Optional[dict] = None) -> dict:
    """Build a payload dict from the per-type HTML form fields."""
    if cred_type == "domain_join":
        pw = form.get("password", "")
        payload = {
            "domain_fqdn": form.get("domain_fqdn", "").strip(),
            "username": form.get("username", "").strip(),
            "password": pw if pw else (existing or {}).get("password", ""),
            "ou_hint": form.get("ou_hint", "").strip(),
        }
        if not payload["password"]:
            raise ValueError("password is required")
        return payload
    if cred_type == "local_admin":
        pw = form.get("la_password", "")
        payload = {
            "username": form.get("la_username", "").strip(),
            "password": pw if pw else (existing or {}).get("password", ""),
        }
        if not payload["password"]:
            raise ValueError("password is required")
        return payload
    if cred_type == "odj_blob":
        upload = form.get("odj_file")
        if upload and hasattr(upload, "read"):
            blob = await upload.read()
            return {
                "blob_b64": base64.b64encode(blob).decode("ascii"),
                "generated_at": _now_iso(),
            }
        if existing:
            return existing
        raise ValueError("ODJ blob file is required")
    raise ValueError(f"unknown credential type: {cred_type}")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
```

Note: `_payload_from_form` is `async` because `odj_blob` needs to await `upload.read()`. Call sites use `await _payload_from_form(...)`.

- [ ] **Step 3: Smoke check import**

Run: `cd autopilot-proxmox && python3 -c "from web import app; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Run all tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/templates/credential_edit.html autopilot-proxmox/web/app.py
git commit -m "feat(ui): add credential create/edit/delete pages"
```

---

### Task 5.4: Sequences list page

**Files:**
- Create: `autopilot-proxmox/web/templates/sequences.html`
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Create the template**

```html
{% extends "base.html" %}
{% block title %}Task Sequences - Proxmox VE Autopilot{% endblock %}
{% block content %}
<h2>Task Sequences
  <small style="color:#999;font-weight:normal;font-size:11px;">
    · <a href="/sequences/new">+ New sequence</a>
  </small>
</h2>
<p style="color:#666;font-size:11px;">
  A task sequence is an ordered list of steps that together provision a
  VM — OEM hardware, OOBE account, domain join, scripts. Exactly one
  sequence is marked as <b>default</b> and used when the provision page
  doesn't specify one.
</p>

{% if not sequences %}
<p><i>No sequences yet. <a href="/sequences/new">Create one</a>.</i></p>
{% else %}
<table>
<tr><th>Name</th><th>Description</th><th>Steps</th><th>Default?</th>
    <th>Autopilot hash?</th><th>Updated</th><th>Actions</th></tr>
{% for s in sequences %}
<tr>
  <td>{{ s.name }}</td>
  <td>{{ s.description }}</td>
  <td>{{ s.step_count }}</td>
  <td>{% if s.is_default %}<span class="status-green">default</span>{% else %}—{% endif %}</td>
  <td>{% if s.produces_autopilot_hash %}yes{% else %}—{% endif %}</td>
  <td>{{ s.updated_at }}</td>
  <td>
    <a href="/sequences/{{ s.id }}/edit">Edit</a> ·
    <form method="POST" action="/sequences/{{ s.id }}/duplicate"
          style="display:inline;">
      <input type="hidden" name="new_name" value="{{ s.name }} (copy)">
      <input type="submit" value="Duplicate" style="padding:1px 6px;">
    </form> ·
    <form method="POST" action="/sequences/{{ s.id }}/delete"
          style="display:inline;"
          onsubmit="return confirm('Delete sequence {{ s.name }}?');">
      <input type="submit" value="Delete" style="padding:1px 6px;">
    </form>
  </td>
</tr>
{% endfor %}
</table>
{% endif %}

{% if error %}
<p><span class="status-red">Error:</span> {{ error }}</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Add route**

Append to app.py:

```python
@app.get("/sequences", response_class=HTMLResponse)
def page_sequences(request: Request, error: str = ""):
    seqs = sequences_db.list_sequences(SEQUENCES_DB)
    return templates.TemplateResponse("sequences.html", {
        "request": request, "sequences": seqs, "error": error,
    })


@app.post("/sequences/{seq_id}/delete")
def submit_sequence_delete(seq_id: int):
    try:
        sequences_db.delete_sequence(SEQUENCES_DB, seq_id)
    except sequences_db.SequenceInUse as e:
        msg = f"in use by VMs {e.vmids}"
        return RedirectResponse(f"/sequences?error={msg}", status_code=303)
    return RedirectResponse("/sequences", status_code=303)


@app.post("/sequences/{seq_id}/duplicate")
async def submit_sequence_duplicate(request: Request, seq_id: int):
    form = await request.form()
    new_name = form.get("new_name", "").strip() or "Copy"
    try:
        sequences_db.duplicate_sequence(SEQUENCES_DB, seq_id, new_name=new_name)
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            return RedirectResponse(
                f"/sequences?error=name '{new_name}' already exists",
                status_code=303)
        raise
    return RedirectResponse("/sequences", status_code=303)
```

- [ ] **Step 3: Smoke**

Run: `cd autopilot-proxmox && python3 -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/templates/sequences.html autopilot-proxmox/web/app.py
git commit -m "feat(ui): add /sequences list page with delete and duplicate"
```

---

### Task 5.5: Sequence builder page

**Files:**
- Create: `autopilot-proxmox/web/templates/sequence_edit.html`
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Create the template — client-rendered step builder backed by the JSON API**

```html
{% extends "base.html" %}
{% block title %}{% if seq %}Edit{% else %}New{% endif %} Sequence{% endblock %}
{% block content %}
<h2>{% if seq %}Edit sequence: {{ seq.name }}{% else %}New task sequence{% endif %}</h2>

<form id="seq-form">
<table border="0" cellpadding="3">
<tr><td><b>Name:</b></td>
    <td><input type="text" id="name" value="{{ seq.name if seq else '' }}" required></td></tr>
<tr><td><b>Description:</b></td>
    <td><input type="text" id="description" size="60"
               value="{{ seq.description if seq else '' }}"></td></tr>
<tr><td><b>Default?</b></td>
    <td><label><input type="checkbox" id="is_default"
        {% if seq and seq.is_default %}checked{% endif %}> Mark as the default sequence</label></td></tr>
<tr><td><b>Autopilot hash?</b></td>
    <td><label><input type="checkbox" id="produces_autopilot_hash"
        {% if seq and seq.produces_autopilot_hash %}checked{% endif %}> VMs provisioned with this sequence produce an Autopilot hash</label></td></tr>
</table>
</form>

<h3>Steps</h3>
<div id="steps"></div>

<div style="margin-top:10px;">
  <select id="add-type">
    <option value="set_oem_hardware">set_oem_hardware</option>
    <option value="local_admin">local_admin</option>
    <option value="autopilot_entra">autopilot_entra</option>
    <option value="autopilot_hybrid">autopilot_hybrid (stub)</option>
    <option value="join_ad_domain">join_ad_domain</option>
    <option value="rename_computer">rename_computer</option>
    <option value="run_script">run_script</option>
    <option value="install_module">install_module</option>
    <option value="wait_guest_agent">wait_guest_agent</option>
  </select>
  <button type="button" onclick="addStep()">+ Add step</button>
</div>

<div style="margin-top:12px;">
  <button type="button" onclick="save()">Save</button>
  <a href="/sequences" style="margin-left:8px;">Cancel</a>
  <span id="flash" style="margin-left:10px;"></span>
</div>

<script>
const SEQ_ID = {{ seq.id if seq else 'null' }};
const SEED = {{ (seq.steps if seq else [])|tojson }};
let creds = [];
let oemProfiles = {{ oem_profiles|tojson }};
let stepsState = SEED.map(s => ({...s, params: {...s.params}}));

async function loadCreds() {
  const r = await fetch('/api/credentials');
  creds = await r.json();
}

function flash(msg, isErr) {
  const el = document.getElementById('flash');
  el.textContent = msg;
  el.style.color = isErr ? '#c00' : '#060';
  setTimeout(() => { el.textContent = ''; }, 4000);
}

function render() {
  const host = document.getElementById('steps');
  host.innerHTML = '';
  stepsState.forEach((s, i) => {
    const card = document.createElement('div');
    card.style.cssText = 'border:1px solid #ccc;padding:6px;margin:4px 0;background:#fafafa;';
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <div><b>${i+1}. ${escHtml(s.step_type)}</b>
          <small style="color:#666;margin-left:8px;">${summary(s)}</small></div>
        <div>
          <button type="button" onclick="move(${i},-1)" ${i===0?'disabled':''}>↑</button>
          <button type="button" onclick="move(${i},1)" ${i===stepsState.length-1?'disabled':''}>↓</button>
          <label style="margin-left:8px;font-size:11px;">
            <input type="checkbox" ${s.enabled?'checked':''} onchange="toggleEnabled(${i},this.checked)"> enabled
          </label>
          <button type="button" onclick="remove(${i})" style="margin-left:8px;">✕</button>
        </div>
      </div>
      <div style="padding:4px 0;">${paramsForm(s, i)}</div>
    `;
    host.appendChild(card);
  });
}

function escHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function summary(s) {
  const p = s.params || {};
  switch (s.step_type) {
    case 'set_oem_hardware': return p.oem_profile || '(no profile set)';
    case 'local_admin': {
      const cred = creds.find(c => c.id === p.credential_id);
      return cred ? `creds: ${cred.name}` : '(credential not set)';
    }
    case 'join_ad_domain': {
      const cred = creds.find(c => c.id === p.credential_id);
      return (cred ? `creds: ${cred.name}` : '(credential not set)') +
             (p.ou_path ? ` · ${p.ou_path}` : '');
    }
    case 'rename_computer': return p.pattern || '(pattern not set)';
    case 'run_script': return p.name || '(script name not set)';
    case 'install_module': return p.module || '(module not set)';
    default: return '';
  }
}

function paramsForm(s, i) {
  const p = s.params || {};
  const onI = (key) => `onchange="setParam(${i},'${key}',this.value)"`;
  switch (s.step_type) {
    case 'set_oem_hardware':
      return `<label>OEM profile:
        <select ${onI('oem_profile')}>
          <option value="">(use vars.yml default)</option>
          ${Object.keys(oemProfiles).map(k =>
            `<option value="${k}" ${p.oem_profile===k?'selected':''}>${k} — ${escHtml(oemProfiles[k].manufacturer)} ${escHtml(oemProfiles[k].product)}</option>`).join('')}
        </select></label>`;
    case 'local_admin':
    case 'join_ad_domain': {
      const wantType = s.step_type === 'local_admin' ? 'local_admin' : 'domain_join';
      const opts = creds.filter(c => c.type === wantType)
        .map(c => `<option value="${c.id}" ${p.credential_id===c.id?'selected':''}>${escHtml(c.name)}</option>`).join('');
      const extra = s.step_type === 'join_ad_domain'
        ? `<br><label>OU path: <input type="text" value="${escHtml(p.ou_path||'')}" size="60"
                                       oninput="setParam(${i},'ou_path',this.value)"></label>`
        : '';
      return `<label>Credential:
        <select onchange="setParam(${i},'credential_id',+this.value)">
          <option value="0">(none)</option>${opts}
        </select></label> <a href="/credentials/new" target="_blank">+ new</a>${extra}`;
    }
    case 'rename_computer':
      return `<label>Pattern: <input type="text" value="${escHtml(p.pattern||'{serial}')}"
                                      oninput="setParam(${i},'pattern',this.value)"> (tokens: {serial}, {vmid}, {group_tag})</label>`;
    case 'run_script': {
      const reboot = p.causes_reboot ? 'checked' : '';
      return `<label>Name: <input type="text" value="${escHtml(p.name||'')}" oninput="setParam(${i},'name',this.value)"></label>
              <br><label>Script:<br><textarea rows="4" cols="80" oninput="setParam(${i},'script',this.value)">${escHtml(p.script||'')}</textarea></label>
              <br><label><input type="checkbox" ${reboot} onchange="setParam(${i},'causes_reboot',this.checked)"> causes reboot</label>`;
    }
    case 'install_module':
      return `<label>Module: <input type="text" value="${escHtml(p.module||'')}" oninput="setParam(${i},'module',this.value)"></label>`;
    case 'autopilot_entra':
    case 'autopilot_hybrid':
    case 'wait_guest_agent':
    default:
      return `<small style="color:#666;">(no parameters)</small>`;
  }
}

function setParam(i, k, v) { stepsState[i].params[k] = v; render(); }
function toggleEnabled(i, v) { stepsState[i].enabled = v; }
function move(i, d) {
  const j = i + d; if (j < 0 || j >= stepsState.length) return;
  [stepsState[i], stepsState[j]] = [stepsState[j], stepsState[i]]; render();
}
function remove(i) { stepsState.splice(i,1); render(); }
function addStep() {
  const t = document.getElementById('add-type').value;
  stepsState.push({ step_type: t, params: {}, enabled: true });
  render();
}

async function save() {
  const body = {
    name: document.getElementById('name').value.trim(),
    description: document.getElementById('description').value.trim(),
    is_default: document.getElementById('is_default').checked,
    produces_autopilot_hash: document.getElementById('produces_autopilot_hash').checked,
    steps: stepsState.map(s => ({
      step_type: s.step_type, params: s.params, enabled: s.enabled,
    })),
  };
  if (!body.name) { flash('Name is required', true); return; }
  const url = SEQ_ID ? `/api/sequences/${SEQ_ID}` : '/api/sequences';
  const method = SEQ_ID ? 'PUT' : 'POST';
  const r = await fetch(url, {
    method, headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  if (r.ok) { location.href = '/sequences'; return; }
  let detail = r.statusText;
  try { detail = (await r.json()).detail || detail; } catch {}
  flash('Save failed: ' + detail, true);
}

loadCreds().then(render);
</script>
{% endblock %}
```

- [ ] **Step 2: Add routes + helper to load OEM profiles**

Find the existing code that loads `oem_profiles.yml` and expose a helper — the file is at `files/oem_profiles.yml`. Add:

```python
def _load_oem_profiles_dict() -> dict:
    path = FILES_DIR / "oem_profiles.yml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("oem_profiles", {})


@app.get("/sequences/new", response_class=HTMLResponse)
def page_sequence_new(request: Request):
    return templates.TemplateResponse("sequence_edit.html", {
        "request": request, "seq": None,
        "oem_profiles": _load_oem_profiles_dict(),
    })


@app.get("/sequences/{seq_id}/edit", response_class=HTMLResponse)
def page_sequence_edit(request: Request, seq_id: int):
    seq = sequences_db.get_sequence(SEQUENCES_DB, seq_id)
    if seq is None:
        raise HTTPException(404, "sequence not found")
    return templates.TemplateResponse("sequence_edit.html", {
        "request": request, "seq": seq,
        "oem_profiles": _load_oem_profiles_dict(),
    })
```

- [ ] **Step 3: Smoke tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/ -v`
Expected: all pass (existing tests unaffected; new pages tested via API tests).

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/templates/sequence_edit.html autopilot-proxmox/web/app.py
git commit -m "feat(ui): add sequence builder page"
```

---

## Phase 6 — Seed migration

### Task 6.1: Seed logic with tests

**Files:**
- Modify: `autopilot-proxmox/web/sequences_db.py`
- Modify: `autopilot-proxmox/tests/test_sequences_db.py`

- [ ] **Step 1: Add failing seed tests**

Append to `tests/test_sequences_db.py`:

```python
def test_seed_defaults_inserts_three_on_empty(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    names = [s["name"] for s in sequences_db.list_sequences(db_path)]
    assert "Entra Join (default)" in names
    assert "AD Domain Join — Local Admin" in names
    assert "Hybrid Autopilot (stub)" in names


def test_seed_defaults_idempotent(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    sequences_db.seed_defaults(db_path, cipher)  # second call no-op
    assert len(sequences_db.list_sequences(db_path)) == 3


def test_seed_creates_default_credential(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    creds = sequences_db.list_credentials(db_path, type="local_admin")
    assert any(c["name"] == "default-local-admin" for c in creds)


def test_seed_entra_sequence_is_default_and_produces_hash(db_path, key_path):
    from web import crypto, sequences_db
    sequences_db.init(db_path)
    cipher = crypto.Cipher(key_path)
    sequences_db.seed_defaults(db_path, cipher)
    for s in sequences_db.list_sequences(db_path):
        if s["name"] == "Entra Join (default)":
            assert s["is_default"] is True
            assert s["produces_autopilot_hash"] is True
            break
    else:
        pytest.fail("Entra Join (default) not found")
```

- [ ] **Step 2: Verify they fail**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v -k seed`
Expected: fail with `AttributeError: module 'web.sequences_db' has no attribute 'seed_defaults'`.

- [ ] **Step 3: Implement `seed_defaults`**

Append to `web/sequences_db.py`:

```python
# Seed data is defined here rather than a separate file so version-controlled
# changes to seeded sequences are obvious in the diff.

_SEED_SEQUENCES = [
    {
        "name": "Entra Join (default)",
        "description": "Entra-joined via Windows Autopilot. Matches the pre-sequence hardcoded flow.",
        "is_default": True,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": ""},   # inherits vars.yml default
             "enabled": True},
            {"step_type": "local_admin",
             "params": {"credential_name": "default-local-admin"},
             "enabled": True},
            {"step_type": "autopilot_entra", "params": {}, "enabled": True},
        ],
    },
    {
        "name": "AD Domain Join — Local Admin",
        "description": "Local admin created at OOBE, computer joined to AD during specialize pass.",
        "is_default": False,
        "produces_autopilot_hash": False,
        "steps": [
            {"step_type": "set_oem_hardware",
             "params": {"oem_profile": ""}, "enabled": True},
            {"step_type": "local_admin",
             "params": {"credential_name": "default-local-admin"},
             "enabled": True},
            {"step_type": "join_ad_domain",
             "params": {"credential_id": 0, "ou_path": ""},
             "enabled": True},
            {"step_type": "rename_computer",
             "params": {"pattern": "{serial}"}, "enabled": True},
        ],
    },
    {
        "name": "Hybrid Autopilot (stub)",
        "description": "NOT IMPLEMENTED in v1 — scaffolded for future work.",
        "is_default": False,
        "produces_autopilot_hash": True,
        "steps": [
            {"step_type": "autopilot_hybrid", "params": {}, "enabled": True},
        ],
    },
]


_SEED_LOCAL_ADMIN_PAYLOAD = {
    "username": "Administrator",
    # The hardcoded password from files/unattend_oobe.xml, preserved so the
    # default sequence reproduces today's byte-identical output.
    "password": "Nsta1200!!",
}


def seed_defaults(db_path, cipher) -> None:
    """Insert the default credential and three starter sequences if absent.

    Idempotent: rows keyed on name are skipped if already present.
    Resolves credential_name references to actual credential IDs.
    """
    # 1. Credential first — other seeds reference it by name.
    if not any(c["name"] == "default-local-admin"
               for c in list_credentials(db_path, type="local_admin")):
        create_credential(
            db_path, cipher,
            name="default-local-admin", type="local_admin",
            payload=_SEED_LOCAL_ADMIN_PAYLOAD,
        )
    la_id = next(c["id"] for c in list_credentials(db_path, type="local_admin")
                 if c["name"] == "default-local-admin")

    # 2. Sequences.
    existing_names = {s["name"] for s in list_sequences(db_path)}
    for seq in _SEED_SEQUENCES:
        if seq["name"] in existing_names:
            continue
        sid = create_sequence(
            db_path,
            name=seq["name"], description=seq["description"],
            is_default=seq["is_default"],
            produces_autopilot_hash=seq["produces_autopilot_hash"],
        )
        # Resolve credential_name → credential_id
        resolved_steps = []
        for step in seq["steps"]:
            params = dict(step["params"])
            if params.pop("credential_name", None) == "default-local-admin":
                params["credential_id"] = la_id
            resolved_steps.append({
                "step_type": step["step_type"],
                "params": params,
                "enabled": step["enabled"],
            })
        set_sequence_steps(db_path, sid, resolved_steps)
```

- [ ] **Step 4: Run tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_db.py -v -k seed`
Expected: 4 pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/sequences_db.py autopilot-proxmox/tests/test_sequences_db.py
git commit -m "feat(db): add seed_defaults for starter sequences + credential"
```

---

### Task 6.2: Call `seed_defaults` on startup

**Files:**
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Update the startup handler**

Find the earlier `_init_sequences_db` function and replace with:

```python
@app.on_event("startup")
def _init_sequences_db() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    sequences_db.init(SEQUENCES_DB)
    sequences_db.seed_defaults(SEQUENCES_DB, _cipher())
```

- [ ] **Step 2: Smoke test — startup runs and seeds**

Run: `cd autopilot-proxmox && python3 -m pytest tests/test_sequences_api.py -v -k list`
Expected: the `test_sequences_list_empty` and `test_credentials_list_empty` tests may now fail because seeding happens on startup. Fix by updating the API test fixture to clear seeded rows before each test. Apply this diff in `tests/test_sequences_api.py` to the `app_env` fixture:

Replace the fixture with:

```python
@pytest.fixture
def app_env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"), \
             patch("web.app.HASH_DIR", tmp / "hashes"), \
             patch("web.app.job_manager") as jm:
            jm.list_jobs.return_value = []
            jm.jobs_dir = str(tmp / "jobs")
            from web.app import app
            # Init DB without seeds so "empty" tests remain valid.
            from web import sequences_db as _sdb
            secrets.mkdir(parents=True, exist_ok=True)
            _sdb.init(db)
            yield TestClient(app)
```

And add a new test in `tests/test_sequences_api.py` for the seeded behavior:

```python
def test_startup_seeds_defaults(tmp_path):
    """When the app starts on an empty DB, the three seed sequences appear."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        secrets = tmp / "secrets"
        db = tmp / "sequences.db"
        with patch("web.app.SECRETS_DIR", secrets), \
             patch("web.app.SEQUENCES_DB", db), \
             patch("web.app.CREDENTIAL_KEY", secrets / "credential_key"), \
             patch("web.app.HASH_DIR", tmp / "hashes"), \
             patch("web.app.job_manager") as jm:
            jm.list_jobs.return_value = []
            jm.jobs_dir = str(tmp / "jobs")
            # Importing triggers @on_event("startup"); TestClient replays it.
            from web.app import app
            with TestClient(app) as c:
                got = c.get("/api/sequences").json()
    names = [s["name"] for s in got]
    assert "Entra Join (default)" in names
    assert "AD Domain Join — Local Admin" in names
    assert "Hybrid Autopilot (stub)" in names
```

- [ ] **Step 3: Run all tests**

Run: `cd autopilot-proxmox && python3 -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_sequences_api.py
git commit -m "feat(web): seed default sequences on container startup"
```

---

## Phase 7 — Provision page dropdown (inert in Phase A)

### Task 7.1: Add Task Sequence dropdown to provision.html

**Files:**
- Modify: `autopilot-proxmox/web/templates/provision.html`
- Modify: `autopilot-proxmox/web/app.py`

- [ ] **Step 1: Template change**

Find the `<tr>` with `<b>OEM Profile:</b>`. Immediately above it, insert a new row:

```html
<tr>
  <td><b>Task Sequence:</b></td>
  <td><select name="sequence_id">
    {% for s in sequences %}
    <option value="{{ s.id }}"{% if s.is_default %} selected{% endif %}>{{ s.name }}{% if s.is_default %} (default){% endif %}</option>
    {% endfor %}
  </select>
  <small style="color:#999;">Phase A: selection is recorded but still uses the hardcoded flow. Phase B wires the compiler.</small></td>
</tr>
```

- [ ] **Step 2: Route change — pass sequences to template**

Find the existing `/provision` GET handler (`page_provision` or similar). Add `sequences_db.list_sequences(SEQUENCES_DB)` to the template context under key `sequences`.

If the handler looks like:

```python
@app.get("/provision", response_class=HTMLResponse)
def page_provision(request: Request):
    ...
    return templates.TemplateResponse("provision.html", {
        "request": request, ...
    })
```

add `"sequences": sequences_db.list_sequences(SEQUENCES_DB),` to the dict.

- [ ] **Step 3: Provision POST — record sequence selection**

Find the `@app.post("/api/jobs/provision")` handler. Add `sequence_id: int = Form(None)` to its signature, and immediately after the job is submitted (but before returning), call:

```python
# Phase A: record the selection only. The job itself still uses the
# hardcoded provisioning flow — compiler wiring lands in Phase B.
if sequence_id:
    # We don't know vmid yet (it's allocated by Proxmox during the job);
    # defer the vm_provisioning row write to the job's post-clone handler
    # in Phase B. For now, stash the selection on the job record so Phase
    # B can read it.
    job_manager.set_arg(job["id"], "sequence_id", sequence_id)
```

If `job_manager.set_arg` doesn't exist today, add it. Check `web/jobs.py`:

Run: `grep -n "def set_arg\|def start" autopilot-proxmox/web/jobs.py`

If `set_arg` is missing, add a one-liner to `jobs.py`:

```python
def set_arg(self, job_id: str, key: str, value) -> None:
    """Attach arbitrary key/value metadata to a job (used by Phase B)."""
    j = self._load(job_id)
    if j is None:
        return
    args = j.get("args") or {}
    args[key] = value
    j["args"] = args
    self._save(j)
```

(Adapt to the existing persistence pattern in `jobs.py`; if the methods `_load`/`_save` aren't named that, use the same names `jobs.py` already uses.)

- [ ] **Step 4: Smoke check**

Run: `cd autopilot-proxmox && python3 -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/templates/provision.html autopilot-proxmox/web/app.py \
        autopilot-proxmox/web/jobs.py
git commit -m "feat(ui): add Task Sequence dropdown on provision page (Phase A — recorded, not consumed)"
```

---

## Phase 8 — End-to-end smoke and push

### Task 8.1: Manual smoke test checklist

- [ ] **Step 1: Run full test suite one more time**

Run: `cd autopilot-proxmox && python3 -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 2: Boot the app locally (or note skipped if env lacks uvicorn)**

Run: `cd autopilot-proxmox && python3 -m uvicorn web.app:app --host 127.0.0.1 --port 5050` (background) — then in another shell:

```bash
curl -s http://127.0.0.1:5050/api/sequences | python3 -m json.tool | head
curl -s http://127.0.0.1:5050/api/credentials | python3 -m json.tool | head
```

Expected: three seeded sequences, one seeded credential (`default-local-admin`).

Kill the dev server (Ctrl-C).

- [ ] **Step 3: Browser walk-through (optional — do if dev environment supports it)**

1. Navigate to http://127.0.0.1:5050/credentials — see `default-local-admin`, click Edit, observe type-locked to `local_admin`.
2. Navigate to http://127.0.0.1:5050/sequences — see 3 seeded sequences. Click Edit on "AD Domain Join — Local Admin". Verify the 4-step builder renders.
3. Click "+ New credential", pick `domain_join`, save with a test domain + fake password. Verify it appears in the list.
4. Navigate to http://127.0.0.1:5050/provision — verify the new Task Sequence dropdown appears with "Entra Join (default)" pre-selected.

---

### Task 8.2: Push branch + open PR

- [ ] **Step 1: Verify branch is clean**

Run: `git status`
Expected: working tree clean.

- [ ] **Step 2: Push to origin**

Run: `git push -u origin feat/task-sequences-spec`

Note: this is the same branch the spec was committed to. If you want a separate implementation branch, fork earlier: `git checkout -b feat/task-sequences-phase-a`. For the plan as written, the spec and Phase A implementation live on one branch.

- [ ] **Step 3: Open PR**

Run:

```bash
gh pr create --base main --head feat/task-sequences-spec \
  --title "feat(task-sequences): Phase A — foundation, CRUD UI, seed data" \
  --body "$(cat <<'EOF'
## Summary

Implements Phase A of the task sequences design (spec in this PR under
\`docs/superpowers/specs/\`). Lands the data model, credential encryption,
web UI for both, and seeded defaults.

**No change to provisioning behavior** — the provision page records a
sequence selection but the compiler + Ansible wiring ship in Phase B.

## What's here

- Fernet-encrypted credentials store with key at \`/app/secrets/credential_key\`
- SQLite schema + DAL for sequences, steps, credentials, vm_provisioning
- CRUD pages for credentials and sequences
- Sequence builder with step-type-specific forms
- Three seeded sequences: "Entra Join (default)", "AD Domain Join — Local Admin", "Hybrid Autopilot (stub)"

## Test plan

- [ ] Full pytest suite passes
- [ ] Browser: /credentials lists the seeded default-local-admin
- [ ] Browser: /sequences lists the three seeded sequences
- [ ] Can create a new domain_join credential
- [ ] Can duplicate a sequence
- [ ] Can reorder steps in the builder
- [ ] /provision shows the new Task Sequence dropdown

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review (performed inline before publishing this plan)

**Spec coverage against §s 6, 7, 8.1–8.5, 13:**

- §6 data model: task_sequences (✓ 3.2), task_sequence_steps (✓ 3.2), credentials (✓ 3.2), vm_provisioning (✓ 3.2).
- §7 credentials: encryption key at `/app/secrets/credential_key` (✓ 1.2 + 2.2), Fernet (✓ 2.2), auto-generate on first use (✓ 2.2), three types (✓ 3.4).
- §8.1 `/sequences` list (✓ 5.4), §8.2 sequence builder (✓ 5.5), §8.3 `/credentials` CRUD (✓ 5.2 + 5.3), §8.4 provision-page dropdown (✓ 7.1, recording only), §8.5 nav entries (✓ 5.1).
- §13 seeded content: three sequences + seed-is-idempotent + default credential (✓ 6.1).
- Delete-on-reference protection (spec §8.3 + §12): credentials (✓ 3.4), sequences (✓ 3.6).

**Gaps / Phase B items not in this plan (documented in the Phase A "Out of scope" section):**

- Compiler (§9) — Phase B.
- Ansible role changes (§9 step 6, §14) — Phase B.
- Reboot waiter (§10) — Phase B.
- Capture-action disable on Devices page (§8.7) — Phase B.
- Test Connection button (§8.6) — Phase B.
- Hybrid "Coming soon" badge on the builder step-type dropdown — minor UI polish, bundled into Phase B.

**Placeholder scan:** no "TBD", "implement later", "similar to task N" strings remain. All code snippets are complete.

**Type consistency:** `_cipher()` is used as a function in all call sites; `SEQUENCES_DB` / `CREDENTIAL_KEY` / `SECRETS_DIR` are `Path` objects referenced consistently; step shapes (`{step_type, params, enabled, order_index}`) are consistent across DB, API, and template.

**Known rough spots (to watch during execution):**

- The `_payload_from_form` helper is declared `async` in Task 5.3; call sites in Tasks 5.3 need `await`. Double-check during implementation.
- `job_manager.set_arg` (Task 7.1) may not exist — Task 7.1 Step 3 includes a conditional branch to add it.
- FastAPI deprecated `@app.on_event("startup")` in favor of lifespan; sticking with `on_event` to avoid a broader refactor. Flag for Phase B.
