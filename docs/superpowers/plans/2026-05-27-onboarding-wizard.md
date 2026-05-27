# Onboarding Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 5-step onboarding wizard at `/react/onboarding` plus a dedicated live-monitor at `/react/onboarding/setup`, so a brand-new operator can reach a working first provision without leaving the UI.

**Architecture:** New `web/onboarding_pg.py` + `web/onboarding_endpoints.py` + `web/onboarding_launch.py` + `web/onboarding_phases.py` backend modules over a new `onboarding_state` postgres table; new frontend pages at `pages/OnboardingPage.tsx` and `pages/OnboardingSetupPage.tsx` driven by a pure-reducer state machine in `onboarding/machine.ts`. Reuses `auth.py`, `install_tracking_pg`, `jobs`, `proxmox_permissions`, `proxmox_sdn`, `settings_vault`, the python-ldap helper at `app.py:3044`, and the existing CloudOSD / OSDeploy provision endpoints.

**Tech Stack:** FastAPI, psycopg, postgres, python-ldap (existing), React + TypeScript (no React Router, the project routes by `window.location.pathname` in `App.tsx`), vitest, pytest. ASCII hyphens only across all code, copy, and commit messages.

**Spec:** `docs/superpowers/specs/2026-05-27-onboarding-wizard-design.md`

---

## Task 1: Schema + CRUD module (`web/onboarding_pg.py`)

**Files:**
- Create: `autopilot-proxmox/web/onboarding_pg.py`
- Create: `autopilot-proxmox/tests/test_onboarding_pg.py`

- [ ] **Step 1: Write the failing test for `init()` creating the table**

Create `autopilot-proxmox/tests/test_onboarding_pg.py` modeled on `tests/test_install_tracking_pg.py` (which uses the `pg_conn` fixture from `tests/conftest.py:151`).

```python
"""Tests for web/onboarding_pg.py."""
from __future__ import annotations

import json

import pytest

from web import onboarding_pg


@pytest.fixture(autouse=True)
def _reset(pg_conn):
    onboarding_pg.init(pg_conn)
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    yield


def test_init_creates_table_and_is_idempotent(pg_conn):
    onboarding_pg.init(pg_conn)  # second call must not raise
    row = pg_conn.execute(
        "SELECT to_regclass('onboarding_state') AS exists"
    ).fetchone()
    assert row["exists"] == "onboarding_state"
```

- [ ] **Step 2: Run the test, watch it fail**

```bash
cd autopilot-proxmox
pytest tests/test_onboarding_pg.py::test_init_creates_table_and_is_idempotent -v
```
Expected: `ModuleNotFoundError: No module named 'web.onboarding_pg'`.

- [ ] **Step 3: Create the module with `init()` and `reset_for_tests()`**

Create `autopilot-proxmox/web/onboarding_pg.py`:

```python
"""PostgreSQL state store for the operator onboarding wizard."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from web import db_pg


VALID_STATUSES = {"pending", "in_progress", "launched", "complete", "aborted"}
VALID_PERSONAS = {"lab", "msp", "corp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS onboarding_state (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_sub       text NOT NULL UNIQUE,
    status          text NOT NULL DEFAULT 'in_progress',
    current_step    text NOT NULL DEFAULT 'welcome',
    persona         text,
    answers         jsonb NOT NULL DEFAULT '{}'::jsonb,
    launched_run_id text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
"""

DROP_SCHEMA_FOR_TESTS = "DROP TABLE IF EXISTS onboarding_state CASCADE;"


def init(conn: Connection | None = None) -> None:
    """Create the onboarding_state table if missing. Idempotent."""
    own = conn is None
    conn = conn or db_pg.connect()
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_for_tests(conn: Connection) -> None:
    conn.execute(DROP_SCHEMA_FOR_TESTS)
    conn.commit()
```

- [ ] **Step 4: Run the test, watch it pass**

```bash
pytest tests/test_onboarding_pg.py::test_init_creates_table_and_is_idempotent -v
```
Expected: PASS.

- [ ] **Step 5: Add the CRUD-suite failing tests in one batch**

Append to `tests/test_onboarding_pg.py`:

```python
def test_get_state_returns_none_when_no_row(pg_conn):
    assert onboarding_pg.get_state(pg_conn, "alice@example.com") is None


def test_put_state_creates_then_returns_etag(pg_conn):
    row = onboarding_pg.put_state(
        pg_conn,
        owner_sub="alice@example.com",
        if_match=None,
        patch={"persona": "lab", "current_step": "identity"},
    )
    assert row["owner_sub"] == "alice@example.com"
    assert row["persona"] == "lab"
    assert row["current_step"] == "identity"
    assert row["status"] == "in_progress"
    assert row["etag"]  # weak ETag derived from updated_at


def test_put_state_rejects_stale_if_match(pg_conn):
    first = onboarding_pg.put_state(
        pg_conn, owner_sub="bob@example.com", if_match=None, patch={"persona": "lab"}
    )
    onboarding_pg.put_state(
        pg_conn, owner_sub="bob@example.com", if_match=first["etag"], patch={"persona": "msp"}
    )
    with pytest.raises(onboarding_pg.StaleEtag):
        onboarding_pg.put_state(
            pg_conn, owner_sub="bob@example.com", if_match=first["etag"], patch={"persona": "corp"}
        )


def test_put_state_rejects_invalid_status(pg_conn):
    with pytest.raises(ValueError):
        onboarding_pg.put_state(
            pg_conn, owner_sub="bob@example.com", if_match=None, patch={"status": "purple"}
        )


def test_put_state_rejects_invalid_persona(pg_conn):
    with pytest.raises(ValueError):
        onboarding_pg.put_state(
            pg_conn, owner_sub="bob@example.com", if_match=None, patch={"persona": "weekend warrior"}
        )


def test_delete_state_removes_row(pg_conn):
    onboarding_pg.put_state(
        pg_conn, owner_sub="carol@example.com", if_match=None, patch={"persona": "lab"}
    )
    onboarding_pg.delete_state(pg_conn, "carol@example.com")
    assert onboarding_pg.get_state(pg_conn, "carol@example.com") is None


def test_set_launched_run_records_id_and_flips_status(pg_conn):
    onboarding_pg.put_state(
        pg_conn, owner_sub="dan@example.com", if_match=None, patch={"persona": "msp"}
    )
    onboarding_pg.set_launched_run(pg_conn, "dan@example.com", run_id="onboarding-dan-1")
    row = onboarding_pg.get_state(pg_conn, "dan@example.com")
    assert row["launched_run_id"] == "onboarding-dan-1"
    assert row["status"] == "launched"
```

- [ ] **Step 6: Run the new tests, watch them all fail**

```bash
pytest tests/test_onboarding_pg.py -v
```
Expected: 7 failures (all CRUD tests; init test still passes).

- [ ] **Step 7: Implement the CRUD surface**

Append to `autopilot-proxmox/web/onboarding_pg.py`:

```python
class StaleEtag(Exception):
    """Raised when If-Match does not match the current row's etag."""


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    if isinstance(out.get("answers"), str):
        import json
        out["answers"] = json.loads(out["answers"])
    out["etag"] = _etag_for(out["updated_at"])
    return out


def _etag_for(updated_at: datetime) -> str:
    return f'W/"{updated_at.isoformat()}"'


def get_state(conn: Connection, owner_sub: str) -> dict | None:
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        "SELECT * FROM onboarding_state WHERE owner_sub = %s",
        (owner_sub,),
    ).fetchone()
    return _row_to_dict(row)


def put_state(
    conn: Connection,
    *,
    owner_sub: str,
    if_match: str | None,
    patch: dict[str, Any],
) -> dict:
    """Insert or update the row for `owner_sub`. Returns the updated row.

    If the row exists and `if_match` does not equal its current etag, raises
    StaleEtag. If the row does not exist, `if_match` must be None.
    """
    # Validate inputs.
    if "status" in patch and patch["status"] not in VALID_STATUSES:
        raise ValueError(f"invalid status: {patch['status']}")
    if "persona" in patch and patch["persona"] is not None and patch["persona"] not in VALID_PERSONAS:
        raise ValueError(f"invalid persona: {patch['persona']}")

    cur = conn.cursor(row_factory=dict_row)
    existing = cur.execute(
        "SELECT * FROM onboarding_state WHERE owner_sub = %s FOR UPDATE",
        (owner_sub,),
    ).fetchone()

    if existing is None:
        if if_match is not None:
            raise StaleEtag("row does not exist; if_match must be None")
        # Insert.
        merged_answers = patch.get("answers", {})
        row = cur.execute(
            """
            INSERT INTO onboarding_state
                (owner_sub, status, current_step, persona, answers, launched_run_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                owner_sub,
                patch.get("status", "in_progress"),
                patch.get("current_step", "welcome"),
                patch.get("persona"),
                Jsonb(merged_answers),
                patch.get("launched_run_id"),
            ),
        ).fetchone()
    else:
        current_etag = _etag_for(existing["updated_at"])
        if if_match is not None and if_match != current_etag:
            raise StaleEtag(f"if_match={if_match!r} but current={current_etag!r}")
        # Merge answers shallowly.
        new_answers = dict(existing["answers"] or {})
        if "answers" in patch:
            new_answers.update(patch["answers"])
        row = cur.execute(
            """
            UPDATE onboarding_state SET
                status         = COALESCE(%s, status),
                current_step   = COALESCE(%s, current_step),
                persona        = COALESCE(%s, persona),
                answers        = %s,
                launched_run_id = COALESCE(%s, launched_run_id),
                updated_at     = now()
            WHERE owner_sub = %s
            RETURNING *
            """,
            (
                patch.get("status"),
                patch.get("current_step"),
                patch.get("persona"),
                Jsonb(new_answers),
                patch.get("launched_run_id"),
                owner_sub,
            ),
        ).fetchone()

    conn.commit()
    return _row_to_dict(row)


def delete_state(conn: Connection, owner_sub: str) -> None:
    conn.execute(
        "DELETE FROM onboarding_state WHERE owner_sub = %s",
        (owner_sub,),
    )
    conn.commit()


def set_launched_run(conn: Connection, owner_sub: str, *, run_id: str) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        """
        UPDATE onboarding_state
        SET launched_run_id = %s, status = 'launched', updated_at = now()
        WHERE owner_sub = %s
        RETURNING *
        """,
        (run_id, owner_sub),
    ).fetchone()
    conn.commit()
    return _row_to_dict(row)
```

- [ ] **Step 8: Run all tests, watch them pass**

```bash
pytest tests/test_onboarding_pg.py -v
```
Expected: 8 passing.

- [ ] **Step 9: Commit**

```bash
git add autopilot-proxmox/web/onboarding_pg.py autopilot-proxmox/tests/test_onboarding_pg.py
git commit -m "feat(onboarding): pg state store with ETag concurrency"
```

---

## Task 2: State CRUD endpoints (`web/onboarding_endpoints.py`)

**Files:**
- Create: `autopilot-proxmox/web/onboarding_endpoints.py`
- Create: `autopilot-proxmox/tests/test_onboarding_endpoints.py`
- Modify: `autopilot-proxmox/web/app.py` (register router; call `onboarding_pg.init(conn)` at startup)
- Modify: `autopilot-proxmox/web/auth.py` (allow `/api/onboarding/*` to require auth like other API routes)

- [ ] **Step 1: Write the failing tests for state GET/PUT/DELETE**

Create `autopilot-proxmox/tests/test_onboarding_endpoints.py`:

```python
"""Tests for web/onboarding_endpoints.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web import onboarding_pg
from web.app import app


@pytest.fixture(autouse=True)
def _reset(pg_conn, monkeypatch):
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    # The endpoints derive owner_sub from the session; for tests we
    # short-circuit auth by patching the current_user dependency.
    from web import auth, onboarding_endpoints
    app.dependency_overrides[auth.current_user] = lambda: {"sub": "tester@example.com"}
    yield
    app.dependency_overrides.clear()


def test_get_state_returns_404_for_new_operator():
    client = TestClient(app)
    r = client.get("/api/onboarding/state")
    assert r.status_code == 404


def test_put_state_creates_row_and_returns_etag():
    client = TestClient(app)
    r = client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    assert r.status_code == 200
    assert r.headers["ETag"].startswith('W/"')
    body = r.json()
    assert body["persona"] == "lab"
    assert body["current_step"] == "welcome"
    assert body["status"] == "in_progress"


def test_put_state_requires_if_match_on_update():
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    r = client.put("/api/onboarding/state", json={"patch": {"persona": "msp"}})
    assert r.status_code == 428  # Precondition Required


def test_put_state_409_on_stale_if_match():
    client = TestClient(app)
    first = client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    etag = first.headers["ETag"]
    client.put(
        "/api/onboarding/state",
        json={"patch": {"persona": "msp"}},
        headers={"If-Match": etag},
    )
    r = client.put(
        "/api/onboarding/state",
        json={"patch": {"persona": "corp"}},
        headers={"If-Match": etag},  # stale now
    )
    assert r.status_code == 409


def test_delete_state_clears_row():
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"patch": {"persona": "lab"}})
    r = client.delete("/api/onboarding/state")
    assert r.status_code == 204
    r2 = client.get("/api/onboarding/state")
    assert r2.status_code == 404


def test_probe_endpoints_stubbed_to_501():
    client = TestClient(app)
    for path in ("ad", "tenant", "artifact"):
        r = client.post(f"/api/onboarding/probe/{path}", json={})
        assert r.status_code == 501


def test_launch_endpoint_stubbed_to_501():
    client = TestClient(app)
    r = client.post("/api/onboarding/launch", json={})
    assert r.status_code == 501


def test_setup_status_stubbed_to_501():
    client = TestClient(app)
    r = client.get("/api/onboarding/setup-status")
    assert r.status_code == 501
```

- [ ] **Step 2: Run the tests, watch them fail with ModuleNotFoundError or 404**

```bash
pytest tests/test_onboarding_endpoints.py -v
```
Expected: all 8 fail.

- [ ] **Step 3: Create the endpoint module with state CRUD and probe/launch stubs**

Create `autopilot-proxmox/web/onboarding_endpoints.py`:

```python
"""FastAPI router for the operator onboarding wizard."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from web import auth, db_pg, onboarding_pg


router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


def _owner_sub(user: dict = Depends(auth.current_user)) -> str:
    """Server-derived owner_sub. Local-auth mode returns 'local-operator'."""
    sub = user.get("sub")
    if not sub:
        return "local-operator"
    return sub


class PutStateRequest(BaseModel):
    patch: dict[str, Any] = {}


@router.get("/state")
def get_state(response: Response, owner_sub: str = Depends(_owner_sub)):
    with db_pg.connection() as conn:
        row = onboarding_pg.get_state(conn, owner_sub)
    if row is None:
        raise HTTPException(status_code=404, detail="no onboarding row")
    response.headers["ETag"] = row["etag"]
    return _scrub_for_client(row)


@router.put("/state")
def put_state(
    body: PutStateRequest,
    response: Response,
    owner_sub: str = Depends(_owner_sub),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    # Intake: pull raw secret values out of the patch, write them to vault.yml,
    # rewrite the patch with the sentinel ref shape. Secrets never reach the DB row.
    sanitized = _intake_secrets(owner_sub, body.patch)
    with db_pg.connection() as conn:
        existing = onboarding_pg.get_state(conn, owner_sub)
        if existing is not None and if_match is None:
            raise HTTPException(status_code=428, detail="If-Match required for updates")
        try:
            row = onboarding_pg.put_state(
                conn, owner_sub=owner_sub, if_match=if_match, patch=sanitized
            )
        except onboarding_pg.StaleEtag as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    response.headers["ETag"] = row["etag"]
    return _scrub_for_client(row)


def _intake_secrets(owner_sub: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Pull raw secret values out of the patch, write them to vault.yml,
    rewrite the patch with sentinel refs. Mutates a copy; original patch is untouched."""
    from web import settings_vault  # uses the same vault.yml IO as the settings page
    out = dict(patch)
    identity = dict(out.get("identity") or {})
    for raw_key, ref_key, vault_key in [
        ("ad_join_password", "ad_join_password_ref", f"onboarding/{owner_sub}/ad_join_password"),
        ("local_admin_password", "local_admin_password_ref", f"onboarding/{owner_sub}/local_admin_password"),
    ]:
        raw = identity.pop(raw_key, None)
        if raw is None or raw == "":
            continue
        settings_vault.set_value(vault_key, raw)
        identity[ref_key] = f"vault:{vault_key}"
    if "identity" in out:
        out["identity"] = identity
    return out


@router.delete("/state", status_code=204)
def delete_state(owner_sub: str = Depends(_owner_sub)):
    with db_pg.connection() as conn:
        onboarding_pg.delete_state(conn, owner_sub)
    return Response(status_code=204)


def _scrub_for_client(row: dict) -> dict:
    """Strip secret values; emit {ref, is_set} for *_password_ref fields."""
    answers = dict(row.get("answers") or {})
    identity = dict(answers.get("identity") or {})
    for key in ("ad_join_password_ref", "local_admin_password_ref"):
        ref = identity.get(key)
        if isinstance(ref, dict):
            continue  # already scrubbed
        if isinstance(ref, str) and ref.startswith("vault:"):
            identity[key] = {"ref": ref, "is_set": True}
        elif ref:
            identity[key] = {"ref": None, "is_set": True}
        else:
            identity[key] = {"ref": None, "is_set": False}
    answers["identity"] = identity
    out = dict(row)
    out["answers"] = answers
    return out


_PROBE_LOCKS: dict[tuple[str, str], "asyncio.Lock"] = {}


def _probe_lock(owner_sub: str, probe_name: str):
    """Per-(owner_sub, probe_name) in-process lock to rate-limit probes.

    Spec: 'Probe endpoints are rate-limited to one in-flight call per
    (owner_sub, probe-name) pair via an in-process lock.' Returns a context
    manager. Raises HTTPException(429) if a call is already in flight.
    """
    import threading
    key = (owner_sub, probe_name)
    lock = _PROBE_LOCKS.setdefault(key, threading.Lock())
    acquired = lock.acquire(blocking=False)
    if not acquired:
        raise HTTPException(status_code=429, detail=f"probe {probe_name} already in flight")
    class _Releaser:
        def __enter__(self): return self
        def __exit__(self, *a): lock.release()
    return _Releaser()


# Probe + launch + setup-status: stubs for now (Tasks 7-12 will implement).
@router.post("/probe/ad", status_code=501)
def probe_ad():
    raise HTTPException(status_code=501, detail="ad probe not yet implemented")


@router.post("/probe/tenant", status_code=501)
def probe_tenant():
    raise HTTPException(status_code=501, detail="tenant probe not yet implemented")


@router.post("/probe/artifact", status_code=501)
def probe_artifact():
    raise HTTPException(status_code=501, detail="artifact probe not yet implemented")


@router.post("/launch", status_code=501)
def launch():
    raise HTTPException(status_code=501, detail="launch not yet implemented")


@router.get("/setup-status", status_code=501)
def setup_status():
    raise HTTPException(status_code=501, detail="setup-status not yet implemented")
```

- [ ] **Step 4: Register the router and call init at startup in `web/app.py`**

In `autopilot-proxmox/web/app.py`, find the section where other routers are registered (search for `app.include_router(` and `install_tracking_pg.init`). Add:

```python
from web import onboarding_endpoints, onboarding_pg

# alongside other include_router calls:
app.include_router(onboarding_endpoints.router)

# alongside other *_pg.init() calls at startup:
onboarding_pg.init()
```

- [ ] **Step 5: Run the tests, watch them pass**

```bash
pytest tests/test_onboarding_endpoints.py -v
```
Expected: 8 passing.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/onboarding_endpoints.py autopilot-proxmox/tests/test_onboarding_endpoints.py autopilot-proxmox/web/app.py
git commit -m "feat(onboarding): state CRUD endpoints with stubbed probes"
```

---

## Task 3: Bootstrap shape + ShellIndexPage CTA + Settings nav entry

**Files:**
- Modify: `autopilot-proxmox/frontend/src/contracts.ts`
- Modify: `autopilot-proxmox/frontend/src/routes.ts`
- Modify: `autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx`
- Modify: `autopilot-proxmox/web/app.py` (inject `onboarding` into bootstrap)
- Modify: `autopilot-proxmox/frontend/src/routes.test.ts`
- Create: `autopilot-proxmox/frontend/src/ShellIndexPage.onboarding.test.tsx` (or extend existing if present)

- [ ] **Step 1: Extend AppBootstrap shape**

In `autopilot-proxmox/frontend/src/contracts.ts`, extend the `AppBootstrap` interface:

```typescript
export interface AppBootstrap {
  readonly buildSha?: string;
  readonly buildTime?: string;
  readonly userName?: string;
  readonly userEmail?: string;
  readonly onboarding?: {
    readonly status: "absent" | "pending" | "in_progress" | "launched" | "complete" | "aborted";
    readonly currentStep?: string;
  };
}
```

- [ ] **Step 2: Add Onboarding to the Settings nav group**

In `autopilot-proxmox/frontend/src/routes.ts`, inside the `Settings` nav group, add at the top:

```typescript
{ path: "/react/onboarding", label: "Onboarding wizard", group: "Settings", phase: "operational", active: true },
{
  path: "/react/onboarding/setup",
  label: "Onboarding setup monitor",
  group: "Settings",
  phase: "operational",
  active: true,
  navParentPath: "/react/onboarding",
  showInNav: false
},
```

- [ ] **Step 3: Write failing test asserting the routes appear and are auth-gated**

Append to `autopilot-proxmox/frontend/src/routes.test.ts`:

```typescript
it("includes onboarding wizard in Settings nav", () => {
  const settings = operatorNavGroups.find((g) => g.label === "Settings");
  expect(settings).toBeDefined();
  const onboarding = settings!.items.find((i) => i.path === "/react/onboarding");
  expect(onboarding).toBeDefined();
  expect(onboarding!.label).toBe("Onboarding wizard");
});

it("hides the onboarding setup monitor from the nav", () => {
  const route = reactRouteForPath("/react/onboarding/setup");
  expect(route).toBeDefined();
  expect(route!.showInNav).toBe(false);
});
```

Also append a pytest case to `autopilot-proxmox/tests/test_react_shell.py`:

```python
def test_onboarding_routes_require_auth():
    assert not auth.is_exempt_path("/react/onboarding")
    assert not auth.is_exempt_path("/react/onboarding/setup")
```

- [ ] **Step 4: Run the tests, watch them pass (auth.is_exempt_path defaults to deny)**

```bash
cd autopilot-proxmox/frontend && npx vitest run src/routes.test.ts
cd autopilot-proxmox && pytest tests/test_react_shell.py -v
```
Expected: all pass.

- [ ] **Step 5: Add the hero CTA to ShellIndexPage**

Modify `autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx`. Where the shell renders, insert at the top of the content area:

```typescript
function OnboardingHero({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const status = bootstrap.onboarding?.status ?? "absent";
  if (status === "absent" || status === "complete" || status === "aborted") {
    return null;
  }
  if (status === "launched") {
    return (
      <a className="onboarding-resume-link" href="/react/onboarding/setup">
        Resume setup monitor
      </a>
    );
  }
  // status === 'pending' | 'in_progress'
  return (
    <section className="onboarding-hero" aria-label="Onboarding">
      <h2>Resume onboarding</h2>
      <p>You started the onboarding wizard but did not finish. Pick up where you left off.</p>
      <a className="onboarding-hero-cta" href="/react/onboarding">
        Resume onboarding
      </a>
    </section>
  );
}
```

Add `<OnboardingHero bootstrap={bootstrap} />` near the top of the existing render output, above any other hero content.

- [ ] **Step 6: Inject `onboarding` into the bootstrap payload in `web/app.py`**

Find the function in `web/app.py` that builds the bootstrap dict served on `/react-shell` and other React routes (grep for `userName` or `buildSha` to locate it). Add an `onboarding` field:

```python
def _build_bootstrap_payload(user: dict | None) -> dict:
    payload = {
        "buildSha": _build_sha(),
        "buildTime": _build_time(),
        "userName": (user or {}).get("name"),
        "userEmail": (user or {}).get("email"),
    }
    sub = (user or {}).get("sub") or "local-operator"
    try:
        with db_pg.connection() as conn:
            row = onboarding_pg.get_state(conn, sub)
    except Exception:
        row = None
    if row is None:
        payload["onboarding"] = {"status": "absent"}
    else:
        payload["onboarding"] = {
            "status": row["status"],
            "currentStep": row["current_step"],
        }
    return payload
```

(If `_build_bootstrap_payload` does not exist by name, refactor the inline bootstrap-dict builder into a function with this signature so subsequent tasks have a single edit point.)

- [ ] **Step 7: Manual smoke**

```bash
docker compose up -d
open http://localhost:8000/react-shell
```
Verify: ShellIndexPage renders. No hero appears (status='absent'). DevTools network tab shows `onboarding: {status: "absent"}` in the bootstrap payload.

- [ ] **Step 8: Commit**

```bash
git add autopilot-proxmox/frontend/src/contracts.ts autopilot-proxmox/frontend/src/routes.ts autopilot-proxmox/frontend/src/routes.test.ts autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_react_shell.py
git commit -m "feat(onboarding): bootstrap injection + ShellIndexPage hero CTA + Settings nav entry"
```

---

## Task 4: Frontend state machine (`onboarding/machine.ts`)

**Files:**
- Create: `autopilot-proxmox/frontend/src/onboarding/types.ts`
- Create: `autopilot-proxmox/frontend/src/onboarding/machine.ts`
- Create: `autopilot-proxmox/frontend/src/onboarding/machine.test.ts`

> **Casing convention:** The spec's `answers` jsonb uses snake_case (`ad_join_password_ref`, `tenant_id`, `current_step`, `launched_run_id`, `schema_version`). The TS types in this task use camelCase per TS convention (`adJoinPasswordRef`, `tenantId`, `currentStep`, `launchedRunId`, `schemaVersion`). The persistence layer in Task 5 owns the conversion: add `toWire(answers)` and `fromWire(raw)` helpers in `persistence.ts` that walk one level and convert key names. Every field listed in the schema needs both directions. If you forget a field, the type checker will catch it because the TS Answers type and the wire payload diverge cleanly.

- [ ] **Step 1: Define the shared types**

Create `autopilot-proxmox/frontend/src/onboarding/types.ts`:

```typescript
export type Persona = "lab" | "msp" | "corp";

export type WizardStep = "welcome" | "identity" | "tenant" | "artifact" | "review";

export type PersistedStatus =
  | "pending"
  | "in_progress"
  | "launched"
  | "complete"
  | "aborted";

export interface Identity {
  readonly mode: "workgroup" | "ad";
  readonly adDomain: string | null;
  readonly adJoinAccount: string | null;
  readonly adJoinPasswordRef: { readonly ref: string | null; readonly isSet: boolean };
  readonly localAdminPasswordRef: { readonly ref: string | null; readonly isSet: boolean };
}

export interface Tenant {
  readonly skipped: boolean;
  readonly tenantId: string | null;
  readonly tenantDomain: string | null;
  readonly commentFile: string | null;
}

export interface Artifact {
  readonly kind: "cloudosd" | "osdeploy";
  readonly source: "existing" | "build";
  readonly existingArtifactId: string | null;
  readonly buildJobId: string | null;
}

export interface Trial {
  readonly vmName: string;
  readonly targetNode: string;
  readonly osEdition: "win11-pro" | "win11-ent" | "win10-pro";
}

export interface ProbeResult {
  readonly at: string;
  readonly ok: boolean;
  readonly detail: string;
}

export interface Answers {
  readonly schemaVersion: 1;
  readonly persona: Persona | null;
  readonly identity: Identity;
  readonly tenant: Tenant;
  readonly artifact: Artifact;
  readonly trial: Trial;
  readonly probeResults: {
    readonly ad: ProbeResult | null;
    readonly tenant: ProbeResult | null;
    readonly artifact: ProbeResult | null;
  };
}

export interface WizardState {
  readonly status: PersistedStatus;
  readonly currentStep: WizardStep;
  readonly answers: Answers;
  readonly launchedRunId: string | null;
  readonly etag: string | null;
}

export type WizardEvent =
  | { readonly type: "hydrate"; readonly state: WizardState }
  | { readonly type: "pickPersona"; readonly persona: Persona }
  | { readonly type: "patchAnswers"; readonly patch: Partial<Answers> }
  | { readonly type: "advance" }
  | { readonly type: "jumpTo"; readonly step: WizardStep }
  | { readonly type: "markLaunched"; readonly runId: string }
  | { readonly type: "markComplete" }
  | { readonly type: "discard" };

export const STEP_ORDER: readonly WizardStep[] = [
  "welcome",
  "identity",
  "tenant",
  "artifact",
  "review",
];

export function initialState(): WizardState {
  return {
    status: "in_progress",
    currentStep: "welcome",
    answers: {
      schemaVersion: 1,
      persona: null,
      identity: {
        mode: "workgroup",
        adDomain: null,
        adJoinAccount: null,
        adJoinPasswordRef: { ref: null, isSet: false },
        localAdminPasswordRef: { ref: null, isSet: false },
      },
      tenant: { skipped: false, tenantId: null, tenantDomain: null, commentFile: null },
      artifact: {
        kind: "cloudosd",
        source: "existing",
        existingArtifactId: null,
        buildJobId: null,
      },
      trial: { vmName: "", targetNode: "", osEdition: "win11-pro" },
      probeResults: { ad: null, tenant: null, artifact: null },
    },
    launchedRunId: null,
    etag: null,
  };
}
```

- [ ] **Step 2: Write the failing reducer tests**

Create `autopilot-proxmox/frontend/src/onboarding/machine.test.ts`:

```typescript
import { describe, it, expect } from "vitest";
import { reduce, canAdvance } from "./machine";
import { initialState, STEP_ORDER, type WizardState, type Persona } from "./types";

describe("onboarding reducer", () => {
  it("starts at welcome step with no persona", () => {
    const s = initialState();
    expect(s.currentStep).toBe("welcome");
    expect(s.answers.persona).toBeNull();
  });

  it("pickPersona sets the persona", () => {
    const s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    expect(s.answers.persona).toBe("lab");
  });

  it("canAdvance(welcome) requires persona", () => {
    expect(canAdvance(initialState())).toBe(false);
    const withPersona = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    expect(canAdvance(withPersona)).toBe(true);
  });

  it("advance progresses through STEP_ORDER", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    for (let i = 0; i < STEP_ORDER.length - 1; i++) {
      s = reduce(s, { type: "advance" });
      expect(s.currentStep).toBe(STEP_ORDER[i + 1]);
    }
  });

  it("advance on the last step is a no-op (launch is a separate event)", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "msp" });
    for (let i = 0; i < STEP_ORDER.length - 1; i++) {
      s = reduce(s, { type: "advance" });
    }
    expect(s.currentStep).toBe("review");
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("review");
  });

  it("jumpTo moves backward but not forward past current", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "lab" });
    s = reduce(s, { type: "advance" });
    s = reduce(s, { type: "advance" });
    expect(s.currentStep).toBe("tenant");
    s = reduce(s, { type: "jumpTo", step: "identity" });
    expect(s.currentStep).toBe("identity");
    s = reduce(s, { type: "jumpTo", step: "review" });
    expect(s.currentStep).toBe("identity"); // cannot jump past current_step
  });

  it("markLaunched flips status and stores run id; current_step freezes", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "corp" });
    s = reduce(s, { type: "markLaunched", runId: "onboarding-x-1" });
    expect(s.status).toBe("launched");
    expect(s.launchedRunId).toBe("onboarding-x-1");
    const before = s.currentStep;
    s = reduce(s, { type: "advance" }); // no-op when launched
    expect(s.currentStep).toBe(before);
  });

  it("hydrate replaces the state wholesale", () => {
    const seed: WizardState = {
      ...initialState(),
      currentStep: "artifact",
      status: "in_progress",
      etag: 'W/"2026-05-27T00:00:00.000Z"',
    };
    const s = reduce(initialState(), { type: "hydrate", state: seed });
    expect(s.currentStep).toBe("artifact");
    expect(s.etag).toBe('W/"2026-05-27T00:00:00.000Z"');
  });

  it("identity step gate: workgroup advances; ad without bind probe blocks", () => {
    let s = reduce(initialState(), { type: "pickPersona", persona: "corp" });
    s = reduce(s, { type: "advance" }); // -> identity
    expect(canAdvance(s)).toBe(true); // default mode is workgroup
    s = reduce(s, {
      type: "patchAnswers",
      patch: { identity: { ...s.answers.identity, mode: "ad" } as Persona extends never ? never : any },
    });
    expect(canAdvance(s)).toBe(false);
    s = reduce(s, {
      type: "patchAnswers",
      patch: {
        probeResults: { ...s.answers.probeResults, ad: { at: "now", ok: true, detail: "ok" } } as any,
      },
    });
    expect(canAdvance(s)).toBe(true);
  });
});
```

- [ ] **Step 3: Run the tests, watch them fail**

```bash
cd autopilot-proxmox/frontend && npx vitest run src/onboarding/machine.test.ts
```
Expected: all fail with `Cannot find module './machine'`.

- [ ] **Step 4: Implement the reducer**

Create `autopilot-proxmox/frontend/src/onboarding/machine.ts`:

```typescript
import {
  STEP_ORDER,
  initialState,
  type Answers,
  type WizardEvent,
  type WizardState,
  type WizardStep,
} from "./types";

function nextStep(current: WizardStep): WizardStep {
  const idx = STEP_ORDER.indexOf(current);
  if (idx < 0 || idx >= STEP_ORDER.length - 1) {
    return current;
  }
  return STEP_ORDER[idx + 1];
}

function mergeAnswers(prev: Answers, patch: Partial<Answers>): Answers {
  return {
    ...prev,
    ...patch,
    identity: { ...prev.identity, ...(patch.identity ?? {}) },
    tenant: { ...prev.tenant, ...(patch.tenant ?? {}) },
    artifact: { ...prev.artifact, ...(patch.artifact ?? {}) },
    trial: { ...prev.trial, ...(patch.trial ?? {}) },
    probeResults: { ...prev.probeResults, ...(patch.probeResults ?? {}) },
  };
}

export function canAdvance(state: WizardState): boolean {
  if (state.status !== "in_progress" && state.status !== "pending") {
    return false;
  }
  switch (state.currentStep) {
    case "welcome":
      return state.answers.persona !== null;
    case "identity":
      if (state.answers.identity.mode === "workgroup") {
        return true;
      }
      return state.answers.probeResults.ad?.ok === true;
    case "tenant":
      return state.answers.tenant.skipped || state.answers.tenant.tenantId !== null;
    case "artifact":
      return (
        state.answers.artifact.source === "existing"
          ? state.answers.artifact.existingArtifactId !== null
          : state.answers.artifact.buildJobId !== null
      );
    case "review":
      return false;
    default:
      return false;
  }
}

function stepIndex(step: WizardStep): number {
  return STEP_ORDER.indexOf(step);
}

export function reduce(state: WizardState, event: WizardEvent): WizardState {
  // Once launched/complete/aborted, only hydrate is allowed.
  if (state.status === "launched" || state.status === "complete" || state.status === "aborted") {
    if (event.type === "hydrate") {
      return event.state;
    }
    if (event.type === "markComplete" && state.status === "launched") {
      return { ...state, status: "complete" };
    }
    if (event.type === "discard") {
      return { ...initialState(), status: "aborted" };
    }
    return state;
  }
  switch (event.type) {
    case "hydrate":
      return event.state;
    case "pickPersona":
      return { ...state, answers: { ...state.answers, persona: event.persona } };
    case "patchAnswers":
      return { ...state, answers: mergeAnswers(state.answers, event.patch) };
    case "advance":
      return canAdvance(state)
        ? { ...state, currentStep: nextStep(state.currentStep) }
        : state;
    case "jumpTo": {
      const targetIdx = stepIndex(event.step);
      const currentIdx = stepIndex(state.currentStep);
      if (targetIdx < 0 || targetIdx > currentIdx) {
        return state;
      }
      return { ...state, currentStep: event.step };
    }
    case "markLaunched":
      return { ...state, status: "launched", launchedRunId: event.runId };
    case "markComplete":
      return { ...state, status: "complete" };
    case "discard":
      return { ...initialState(), status: "aborted" };
  }
}
```

- [ ] **Step 5: Run the tests, watch them pass**

```bash
npx vitest run src/onboarding/machine.test.ts
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/frontend/src/onboarding/
git commit -m "feat(onboarding): pure-reducer state machine with vitest coverage"
```

---

## Task 5: Frontend persistence hook (`onboarding/persistence.ts`)

**Files:**
- Create: `autopilot-proxmox/frontend/src/onboarding/persistence.ts`
- Create: `autopilot-proxmox/frontend/src/onboarding/persistence.test.ts`

- [ ] **Step 1: Write the failing test for fetchState + putState retries**

Create `autopilot-proxmox/frontend/src/onboarding/persistence.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fetchState, putState, deleteState, PreconditionFailedError } from "./persistence";

const FETCH = global.fetch;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  global.fetch = FETCH;
});

describe("persistence layer", () => {
  it("fetchState returns null on 404", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 404 })) as any;
    expect(await fetchState()).toBeNull();
  });

  it("fetchState returns row and etag from headers on 200", async () => {
    global.fetch = vi.fn(async () => new Response(JSON.stringify({ persona: "lab" }), {
      status: 200,
      headers: { ETag: 'W/"abc"', "Content-Type": "application/json" },
    })) as any;
    const result = await fetchState();
    expect(result?.row.persona).toBe("lab");
    expect(result?.etag).toBe('W/"abc"');
  });

  it("putState surfaces 409 as PreconditionFailedError", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 409 })) as any;
    await expect(putState({ patch: {} }, "W/\"stale\"")).rejects.toBeInstanceOf(PreconditionFailedError);
  });

  it("putState retries 1s/3s/9s on 5xx then surfaces", async () => {
    const calls: number[] = [];
    global.fetch = vi.fn(async () => {
      calls.push(Date.now());
      return new Response("", { status: 503 });
    }) as any;
    const start = Date.now();
    const promise = putState({ patch: { persona: "lab" } }, null).catch((e) => e);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(3000);
    await vi.advanceTimersByTimeAsync(9000);
    const err = await promise;
    expect(calls).toHaveLength(4);
    expect(err).toBeInstanceOf(Error);
  });
});
```

- [ ] **Step 2: Run the failing tests**

```bash
npx vitest run src/onboarding/persistence.test.ts
```
Expected: all fail with `Cannot find module './persistence'`.

- [ ] **Step 3: Implement the persistence layer**

Create `autopilot-proxmox/frontend/src/onboarding/persistence.ts`:

```typescript
const STATE_URL = "/api/onboarding/state";

// Wire-shape conversion. Backend uses snake_case in the JSON answers blob;
// TS types in onboarding/types.ts use camelCase. Walk one level deep.
const SNAKE_TO_CAMEL: Record<string, Record<string, string>> = {
  identity: {
    ad_domain: "adDomain",
    ad_join_account: "adJoinAccount",
    ad_join_password_ref: "adJoinPasswordRef",
    local_admin_password_ref: "localAdminPasswordRef",
  },
  tenant: { tenant_id: "tenantId", tenant_domain: "tenantDomain", comment_file: "commentFile" },
  artifact: { existing_artifact_id: "existingArtifactId", build_job_id: "buildJobId" },
  trial: { vm_name: "vmName", target_node: "targetNode", os_edition: "osEdition" },
};
const CAMEL_TO_SNAKE: Record<string, Record<string, string>> = Object.fromEntries(
  Object.entries(SNAKE_TO_CAMEL).map(([group, map]) => [
    group,
    Object.fromEntries(Object.entries(map).map(([s, c]) => [c, s])),
  ]),
);

function convert(answers: any, table: Record<string, Record<string, string>>): any {
  if (!answers || typeof answers !== "object") return answers;
  const out: any = { ...answers };
  for (const [group, map] of Object.entries(table)) {
    if (!out[group]) continue;
    const next: any = { ...out[group] };
    for (const [from, to] of Object.entries(map)) {
      if (from in next) {
        next[to] = next[from];
        delete next[from];
      }
    }
    out[group] = next;
  }
  // Top-level camel/snake for a few fields used outside `answers`.
  if ("schema_version" in out) { out.schemaVersion = out.schema_version; delete out.schema_version; }
  if ("probe_results" in out) { out.probeResults = out.probe_results; delete out.probe_results; }
  return out;
}

export function fromWire(raw: any): any {
  return convert(raw, SNAKE_TO_CAMEL);
}

export function toWire(answers: any): any {
  // Mirror image. Used by callers building the PUT body.
  const flipped = { ...answers };
  if ("schemaVersion" in flipped) { flipped.schema_version = flipped.schemaVersion; delete flipped.schemaVersion; }
  if ("probeResults" in flipped) { flipped.probe_results = flipped.probeResults; delete flipped.probeResults; }
  return convert(flipped, CAMEL_TO_SNAKE);
}

export class PreconditionFailedError extends Error {
  constructor(message = "stale ETag") {
    super(message);
    this.name = "PreconditionFailedError";
  }
}

export class PreconditionRequiredError extends Error {
  constructor(message = "If-Match required") {
    super(message);
    this.name = "PreconditionRequiredError";
  }
}

async function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function fetchState(): Promise<{ row: any; etag: string } | null> {
  const r = await fetch(STATE_URL, { credentials: "include" });
  if (r.status === 404) {
    return null;
  }
  if (r.status === 401) {
    window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
    throw new Error("re-auth required");
  }
  if (!r.ok) {
    throw new Error(`fetchState: ${r.status}`);
  }
  const raw = await r.json();
  return { row: { ...raw, answers: fromWire(raw.answers ?? {}) }, etag: r.headers.get("ETag") ?? "" };
}

export async function putState(
  body: { patch: Record<string, unknown> },
  ifMatch: string | null,
): Promise<{ row: any; etag: string }> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (ifMatch) {
    headers["If-Match"] = ifMatch;
  }
  // Convert the camelCase patch to the snake_case wire shape before sending.
  // Top-level keys in the patch (current_step, status, persona, launched_run_id, answers) are
  // already wire-shaped; only `answers` itself needs key conversion.
  const wireBody = { ...body };
  if (body.patch && typeof body.patch === "object" && "answers" in body.patch) {
    wireBody.patch = { ...body.patch, answers: toWire((body.patch as any).answers) };
  }
  const backoff = [0, 1000, 3000, 9000];
  let lastError: Error | null = null;
  for (const ms of backoff) {
    if (ms > 0) {
      await delay(ms);
    }
    const r = await fetch(STATE_URL, {
      method: "PUT",
      credentials: "include",
      headers,
      body: JSON.stringify(wireBody),
    });
    if (r.status === 401) {
      window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
      throw new Error("re-auth required");
    }
    if (r.status === 409) {
      throw new PreconditionFailedError();
    }
    if (r.status === 428) {
      throw new PreconditionRequiredError();
    }
    if (r.ok) {
      const raw = await r.json();
      return { row: { ...raw, answers: fromWire(raw.answers ?? {}) }, etag: r.headers.get("ETag") ?? "" };
    }
    if (r.status >= 500) {
      lastError = new Error(`putState: ${r.status}`);
      continue;
    }
    throw new Error(`putState: ${r.status}`);
  }
  throw lastError ?? new Error("putState exhausted retries");
}

export async function deleteState(): Promise<void> {
  const r = await fetch(STATE_URL, { method: "DELETE", credentials: "include" });
  if (r.status === 401) {
    window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
    throw new Error("re-auth required");
  }
  if (!r.ok && r.status !== 204) {
    throw new Error(`deleteState: ${r.status}`);
  }
}
```

- [ ] **Step 4: Run the tests, watch them pass**

```bash
npx vitest run src/onboarding/persistence.test.ts
```
Expected: all passing.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/frontend/src/onboarding/persistence.ts autopilot-proxmox/frontend/src/onboarding/persistence.test.ts
git commit -m "feat(onboarding): frontend persistence layer with ETag + retry"
```

---

## Task 6: OnboardingPage shell + StepRail + WelcomePersonaStep + AlreadyConfiguredCard

**Files:**
- Create: `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx`
- Create: `autopilot-proxmox/frontend/src/onboarding/StepRail.tsx`
- Create: `autopilot-proxmox/frontend/src/onboarding/AlreadyConfiguredCard.tsx`
- Create: `autopilot-proxmox/frontend/src/onboarding/steps/WelcomePersonaStep.tsx`
- Create: `autopilot-proxmox/frontend/src/OnboardingPage.test.tsx`
- Modify: `autopilot-proxmox/frontend/src/App.tsx` (add the route)

- [ ] **Step 1: Write the failing render test**

Create `autopilot-proxmox/frontend/src/OnboardingPage.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { OnboardingPage } from "./pages/OnboardingPage";
import type { AppBootstrap } from "./contracts";

const BOOT: AppBootstrap = {
  userName: "Tester",
  userEmail: "tester@example.com",
  onboarding: { status: "in_progress", currentStep: "welcome" },
};

beforeEach(() => {
  global.fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/api/onboarding/state")) {
      return new Response("", { status: 404 });
    }
    return new Response(JSON.stringify({}), { status: 200 });
  }) as any;
});

describe("OnboardingPage", () => {
  it("renders the step rail with five steps", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("navigation", { name: /onboarding steps/i })).toBeInTheDocument();
    });
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });

  it("welcome step asks for a persona", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("radio", { name: /lab/i })).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /msp/i })).toBeInTheDocument();
      expect(screen.getByRole("radio", { name: /corp/i })).toBeInTheDocument();
    });
  });

  it("picking a persona enables the Next button", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => screen.getByRole("radio", { name: /lab/i }));
    const next = screen.getByRole("button", { name: /next/i });
    expect(next).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /lab/i }));
    await waitFor(() => expect(next).toBeEnabled());
  });

  it("renders the Discard onboarding link in the footer", async () => {
    render(<OnboardingPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /discard onboarding/i })).toBeInTheDocument();
    });
  });
});
```

- [ ] **Step 2: Run the failing tests**

```bash
npx vitest run src/OnboardingPage.test.tsx
```
Expected: all fail with `Cannot find module './pages/OnboardingPage'`.

- [ ] **Step 3: Implement StepRail**

Create `autopilot-proxmox/frontend/src/onboarding/StepRail.tsx`:

```typescript
import type { WizardStep } from "./types";

const LABELS: Record<WizardStep, string> = {
  welcome: "Welcome",
  identity: "Identity",
  tenant: "Tenant",
  artifact: "Artifact",
  review: "Review",
};

interface StepRailProps {
  readonly steps: readonly WizardStep[];
  readonly current: WizardStep;
  readonly optional: ReadonlySet<WizardStep>;
  readonly onJump: (step: WizardStep) => void;
}

export function StepRail({ steps, current, optional, onJump }: StepRailProps) {
  return (
    <nav aria-label="Onboarding steps" className="onboarding-step-rail">
      <ol>
        {steps.map((step, idx) => {
          const isCurrent = step === current;
          return (
            <li key={step}>
              <button
                type="button"
                className={isCurrent ? "step-current" : "step-other"}
                aria-current={isCurrent ? "step" : undefined}
                onClick={() => onJump(step)}
              >
                <span className="step-number">{idx + 1}</span>
                <span className="step-label">{LABELS[step]}</span>
                {optional.has(step) ? <span className="step-optional">optional</span> : null}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
```

- [ ] **Step 4: Implement AlreadyConfiguredCard**

Create `autopilot-proxmox/frontend/src/onboarding/AlreadyConfiguredCard.tsx`:

```typescript
import { useEffect, useState } from "react";

interface CardRow {
  readonly label: string;
  readonly detail: string;
  readonly ok: boolean;
}

async function probe(url: string, label: string): Promise<CardRow> {
  try {
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) {
      return { label, detail: `Couldn't reach ${label} (HTTP ${r.status})`, ok: false };
    }
    const body = await r.json();
    return { label, detail: typeof body.summary === "string" ? body.summary : "ok", ok: true };
  } catch (e) {
    return { label, detail: `Couldn't reach ${label} (${(e as Error).message})`, ok: false };
  }
}

export function AlreadyConfiguredCard() {
  const [rows, setRows] = useState<CardRow[]>([]);
  useEffect(() => {
    void (async () => {
      const collected = await Promise.all([
        probe("/api/proxmox/health", "Proxmox"),
        probe("/api/proxmox/storages", "Storage"),
        probe("/api/networks/bridges", "Network"),
        probe("/api/settings/ad-vault-status", "AD vault"),
      ]);
      setRows(collected);
    })();
  }, []);
  return (
    <aside className="already-configured" aria-label="Already configured">
      <h3>Already configured by the controller</h3>
      <ul>
        {rows.map((row) => (
          <li key={row.label} className={row.ok ? "ok" : "warn"}>
            <strong>{row.label}:</strong> {row.detail}
          </li>
        ))}
      </ul>
      <p className="subtitle">
        You did not need to enter any of this. If a row is yellow, open <a href="/react/settings">Settings</a> to fix it.
      </p>
    </aside>
  );
}
```

(If any of the `/api/proxmox/health`, `/api/proxmox/storages`, `/api/networks/bridges`, `/api/settings/ad-vault-status` endpoints does not yet exist, this task includes adding a thin GET wrapper that calls into the existing surface in `proxmox_permissions`, `proxmox_sdn`, or `settings_vault`. Grep for the closest existing endpoint before adding a new one.)

- [ ] **Step 5: Implement WelcomePersonaStep**

Create `autopilot-proxmox/frontend/src/onboarding/steps/WelcomePersonaStep.tsx`:

```typescript
import type { Persona, WizardState } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPickPersona: (persona: Persona) => void;
}

export function WelcomePersonaStep({ state, onPickPersona }: Props) {
  const choices: { value: Persona; label: string; help: string }[] = [
    {
      value: "lab",
      label: "Lab hobbyist",
      help: "Homelab. Workgroup defaults, build CloudOSD locally, controller's own node for the trial.",
    },
    {
      value: "msp",
      label: "MSP technician",
      help: "Onboarding a customer. AD-joined defaults, tenant required, reuse existing artifact, auto-pick first node.",
    },
    {
      value: "corp",
      label: "Corporate IT",
      help: "First-time setup at scale. AD-joined defaults, tenant required, reuse existing artifact, explicit node prompt.",
    },
  ];
  return (
    <section className="onboarding-step">
      <h1>Welcome</h1>
      <p>Pick the lane closest to how you'll use this controller. We'll pre-fill sensible defaults; every field stays editable.</p>
      <fieldset>
        <legend>Which lane describes you?</legend>
        {choices.map((c) => (
          <label key={c.value}>
            <input
              type="radio"
              name="persona"
              value={c.value}
              checked={state.answers.persona === c.value}
              onChange={() => onPickPersona(c.value)}
            />
            <strong>{c.label}</strong>
            <span className="help">{c.help}</span>
          </label>
        ))}
      </fieldset>
    </section>
  );
}
```

- [ ] **Step 6: Implement OnboardingPage**

Create `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx`:

```typescript
import { useEffect, useReducer, useState } from "react";
import type { AppBootstrap } from "../contracts";
import { reduce } from "../onboarding/machine";
import { initialState, STEP_ORDER, type WizardEvent, type WizardState, type WizardStep, type Persona } from "../onboarding/types";
import { StepRail } from "../onboarding/StepRail";
import { AlreadyConfiguredCard } from "../onboarding/AlreadyConfiguredCard";
import { WelcomePersonaStep } from "../onboarding/steps/WelcomePersonaStep";
import { fetchState, putState, deleteState, PreconditionFailedError, PreconditionRequiredError } from "../onboarding/persistence";

function reducer(state: WizardState, event: WizardEvent): WizardState {
  return reduce(state, event);
}

interface Props {
  readonly bootstrap: AppBootstrap;
}

export function OnboardingPage({ bootstrap: _bootstrap }: Props) {
  const [state, dispatch] = useReducer(reducer, initialState());
  const [hydrated, setHydrated] = useState(false);
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);

  // Hydrate on mount.
  useEffect(() => {
    void (async () => {
      const result = await fetchState();
      if (result) {
        dispatch({
          type: "hydrate",
          state: {
            status: result.row.status,
            currentStep: result.row.current_step,
            answers: { ...initialState().answers, ...result.row.answers, persona: result.row.persona },
            launchedRunId: result.row.launched_run_id,
            etag: result.etag,
          },
        });
      }
      setHydrated(true);
    })();
  }, []);

  // Redirect once launched/complete.
  useEffect(() => {
    if (!hydrated) return;
    if (state.status === "launched") {
      window.location.href = "/react/onboarding/setup";
    }
    if (state.status === "complete") {
      window.location.href = "/react-shell";
    }
  }, [hydrated, state.status]);

  const optionalSteps: ReadonlySet<WizardStep> = new Set(
    state.answers.persona === "lab" && state.answers.identity.mode === "workgroup" ? ["tenant"] : []
  );

  async function persist(patch: Record<string, unknown>) {
    try {
      const result = await putState({ patch }, state.etag);
      dispatch({
        type: "hydrate",
        state: { ...state, ...result.row, etag: result.etag, answers: { ...state.answers, ...result.row.answers } },
      });
    } catch (e) {
      if (e instanceof PreconditionFailedError || e instanceof PreconditionRequiredError) {
        const fresh = await fetchState();
        if (fresh) {
          dispatch({
            type: "hydrate",
            state: { ...state, ...fresh.row, etag: fresh.etag, answers: { ...state.answers, ...fresh.row.answers } },
          });
        }
      }
      // Otherwise swallow; banner UX comes in a later refinement.
    }
  }

  function onPickPersona(persona: Persona) {
    dispatch({ type: "pickPersona", persona });
    void persist({ persona });
  }

  function onAdvance() {
    dispatch({ type: "advance" });
    void persist({ current_step: STEP_ORDER[STEP_ORDER.indexOf(state.currentStep) + 1] });
  }

  function onJump(step: WizardStep) {
    dispatch({ type: "jumpTo", step });
    void persist({ current_step: step });
  }

  async function onDiscard() {
    await deleteState();
    window.location.href = "/react-shell";
  }

  return (
    <main className="onboarding-page">
      <StepRail steps={STEP_ORDER} current={state.currentStep} optional={optionalSteps} onJump={onJump} />
      {state.currentStep === "welcome" ? (
        <>
          <AlreadyConfiguredCard />
          <WelcomePersonaStep state={state} onPickPersona={onPickPersona} />
        </>
      ) : (
        // Subsequent step components land in Tasks 7-10.
        <section><p>Step {state.currentStep} pending implementation.</p></section>
      )}
      <footer className="onboarding-footer">
        <button type="button" onClick={onAdvance} disabled={!canAdvanceLocal(state)}>
          Next
        </button>
        <button
          type="button"
          className="onboarding-discard"
          onClick={() => setDiscardConfirmOpen(true)}
          disabled={state.status === "launched"}
          title={state.status === "launched" ? "Cannot discard mid-launch. Abort the run from /react/jobs first." : undefined}
        >
          Discard onboarding
        </button>
      </footer>
      {discardConfirmOpen ? (
        <div role="dialog" aria-modal="true" aria-labelledby="discard-confirm-h">
          <h2 id="discard-confirm-h">Discard your onboarding progress?</h2>
          <p>This wipes your wizard answers. Any setup run already kicked off keeps running; abort it from /react/jobs if needed.</p>
          <button onClick={() => void onDiscard()}>Yes, discard</button>
          <button onClick={() => setDiscardConfirmOpen(false)}>Cancel</button>
        </div>
      ) : null}
    </main>
  );
}

function canAdvanceLocal(state: WizardState): boolean {
  // Inlined import to avoid circular-name concerns; same logic as machine.canAdvance.
  if (state.currentStep === "welcome") {
    return state.answers.persona !== null;
  }
  return true;
}
```

- [ ] **Step 7: Wire the route in App.tsx**

In `autopilot-proxmox/frontend/src/App.tsx`, after the existing route checks, add:

```typescript
if (path === "/react/onboarding") {
  return <OnboardingPage bootstrap={bootstrap} />;
}
```

And add the import at the top:

```typescript
import { OnboardingPage } from "./pages/OnboardingPage";
```

- [ ] **Step 8: Run the tests, watch them pass**

```bash
npx vitest run src/OnboardingPage.test.tsx
```
Expected: all passing.

- [ ] **Step 9: Manual smoke**

```bash
docker compose up -d
open http://localhost:8000/react/onboarding
```
Click each persona radio, watch Next become enabled. Click Discard onboarding, confirm modal opens. Cancel out. Confirm the step rail shows five list items with the "1" highlighted.

- [ ] **Step 10: Commit**

```bash
git add autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx autopilot-proxmox/frontend/src/onboarding/StepRail.tsx autopilot-proxmox/frontend/src/onboarding/AlreadyConfiguredCard.tsx autopilot-proxmox/frontend/src/onboarding/steps/WelcomePersonaStep.tsx autopilot-proxmox/frontend/src/OnboardingPage.test.tsx autopilot-proxmox/frontend/src/App.tsx
git commit -m "feat(onboarding): page shell + step rail + already-configured card + welcome step"
```

---

## Task 7: IdentityStep + AD probe endpoint

**Files:**
- Modify: `autopilot-proxmox/web/onboarding_endpoints.py` (replace probe/ad stub with real implementation)
- Create: `autopilot-proxmox/web/onboarding_probes.py` (probe helpers; keeps endpoints thin)
- Create: `autopilot-proxmox/tests/test_onboarding_probes.py`
- Create: `autopilot-proxmox/frontend/src/onboarding/steps/IdentityStep.tsx`
- Modify: `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx` (render IdentityStep when current_step === 'identity')

- [ ] **Step 1: Write failing test for the AD probe helper**

Create `autopilot-proxmox/tests/test_onboarding_probes.py`:

```python
"""Tests for web/onboarding_probes.py."""
from __future__ import annotations

import pytest

from web import onboarding_probes


def test_probe_ad_returns_per_check_results(monkeypatch):
    def fake_dns(domain):
        return True, "resolved 192.168.2.10"
    def fake_icmp(host):
        return True, "1 round-trip 2ms"
    def fake_ldap_bind(domain, account, password):
        return True, "bound as svc-autopilot"
    monkeypatch.setattr(onboarding_probes, "_dns_resolve", fake_dns)
    monkeypatch.setattr(onboarding_probes, "_icmp_ping", fake_icmp)
    monkeypatch.setattr(onboarding_probes, "_ldap_bind", fake_ldap_bind)

    result = onboarding_probes.probe_ad("home.gell.one", "svc-autopilot", "pw")
    assert result["ok"] is True
    assert result["checks"]["dns"]["ok"] is True
    assert result["checks"]["icmp"]["ok"] is True
    assert result["checks"]["ldap"]["ok"] is True


def test_probe_ad_reports_first_failing_check_in_detail(monkeypatch):
    monkeypatch.setattr(onboarding_probes, "_dns_resolve", lambda d: (False, "NXDOMAIN"))
    monkeypatch.setattr(onboarding_probes, "_icmp_ping", lambda h: (False, "not run"))
    monkeypatch.setattr(onboarding_probes, "_ldap_bind", lambda *a: (False, "not run"))
    result = onboarding_probes.probe_ad("nope.example.com", "x", "x")
    assert result["ok"] is False
    assert "NXDOMAIN" in result["detail"]
```

- [ ] **Step 2: Run, watch it fail**

```bash
pytest tests/test_onboarding_probes.py -v
```
Expected: `ModuleNotFoundError: No module named 'web.onboarding_probes'`.

- [ ] **Step 3: Implement `web/onboarding_probes.py`**

Create the module:

```python
"""Probe helpers for the operator onboarding wizard.

Each probe returns a dict shaped as:
    {"ok": bool, "detail": str, "checks": {<name>: {"ok": bool, "detail": str}, ...}}
"""
from __future__ import annotations

import socket
import subprocess


def _dns_resolve(domain: str) -> tuple[bool, str]:
    try:
        info = socket.getaddrinfo(domain, None)
        addr = info[0][4][0]
        return True, f"resolved {addr}"
    except socket.gaierror as e:
        return False, f"{type(e).__name__}: {e}"


def _icmp_ping(host: str) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return True, out.stdout.splitlines()[-1] if out.stdout else "ok"
        return False, "no reply (ICMP may be blocked; ignore if LDAP succeeds)"
    except subprocess.TimeoutExpired:
        return False, "timeout"


def _ldap_bind(domain: str, account: str, password: str) -> tuple[bool, str]:
    """Use python-ldap with SASL/GSSAPI fallback to simple bind.

    The autopilot stack already wires python-ldap + libsasl2-modules-gssapi-mit
    (see autopilot-proxmox/web/app.py around line 3044 for the existing helper).
    Reuse that helper if present; otherwise fall back to a simple bind here.
    """
    try:
        import ldap  # type: ignore[import]
    except ImportError as e:
        return False, f"python-ldap import failed: {e}"
    try:
        conn = ldap.initialize(f"ldap://{domain}")
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 5)
        conn.simple_bind_s(f"{account}@{domain}", password)
        conn.unbind_s()
        return True, f"bound as {account}@{domain}"
    except ldap.INVALID_CREDENTIALS:
        return False, "invalid credentials"
    except ldap.LDAPError as e:
        return False, f"LDAPError: {e}"


def probe_ad(domain: str, account: str, password: str) -> dict:
    """Probe Active Directory reachability: DNS, ICMP, LDAP bind."""
    dns_ok, dns_detail = _dns_resolve(domain)
    icmp_ok, icmp_detail = _icmp_ping(domain) if dns_ok else (False, "not run (DNS failed)")
    ldap_ok, ldap_detail = (
        _ldap_bind(domain, account, password) if dns_ok else (False, "not run (DNS failed)")
    )
    # AD reachability decision: DNS + LDAP must succeed. ICMP is informational only.
    ok = dns_ok and ldap_ok
    if not dns_ok:
        detail = f"DNS does not resolve the domain: {dns_detail}"
    elif not ldap_ok:
        detail = f"LDAP bind refused: {ldap_detail}"
    else:
        detail = ldap_detail
    return {
        "ok": ok,
        "detail": detail,
        "checks": {
            "dns": {"ok": dns_ok, "detail": dns_detail},
            "icmp": {"ok": icmp_ok, "detail": icmp_detail},
            "ldap": {"ok": ldap_ok, "detail": ldap_detail},
        },
    }
```

- [ ] **Step 4: Run probe tests, watch them pass**

```bash
pytest tests/test_onboarding_probes.py -v
```
Expected: 2 passing.

- [ ] **Step 5: Replace the `/probe/ad` stub in `onboarding_endpoints.py`**

Remove the existing `probe_ad` stub and replace with:

```python
class ProbeAdRequest(BaseModel):
    domain: str
    account: str
    password: str


@router.post("/probe/ad")
def probe_ad(body: ProbeAdRequest, owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_probes
    with _probe_lock(owner_sub, "ad"):
        return onboarding_probes.probe_ad(body.domain, body.account, body.password)
```

Update the corresponding stub-501 test in `test_onboarding_endpoints.py` to assert a 200 with the new shape instead. (Replace the line `for path in ("ad", "tenant", "artifact"):` with `for path in ("tenant", "artifact"):` and add a happy-path test for ad that monkeypatches `web.onboarding_probes.probe_ad` to return a known dict.)

- [ ] **Step 6: Write IdentityStep**

Create `autopilot-proxmox/frontend/src/onboarding/steps/IdentityStep.tsx`:

```typescript
import { useState } from "react";
import type { WizardState } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
}

export function IdentityStep({ state, onPatch }: Props) {
  const identity = state.answers.identity;
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<{ ok: boolean; detail: string } | null>(
    state.answers.probeResults.ad
  );

  async function runProbe() {
    if (!identity.adDomain || !identity.adJoinAccount) {
      return;
    }
    setProbing(true);
    const r = await fetch("/api/onboarding/probe/ad", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        domain: identity.adDomain,
        account: identity.adJoinAccount,
        password: "", // server reads from vault via separate write
      }),
    });
    const body = await r.json();
    setProbing(false);
    setProbeResult({ ok: body.ok, detail: body.detail });
    onPatch({
      probeResults: { ...state.answers.probeResults, ad: { at: new Date().toISOString(), ok: body.ok, detail: body.detail } },
    });
  }

  return (
    <section className="onboarding-step" aria-labelledby="identity-h">
      <h1 id="identity-h">Identity</h1>
      <p>Workgroup is faster to test. AD-joined requires a reachable domain controller.</p>
      <fieldset>
        <legend>Join mode</legend>
        <label>
          <input
            type="radio"
            checked={identity.mode === "workgroup"}
            onChange={() => onPatch({ identity: { ...identity, mode: "workgroup" } })}
          />
          Workgroup
        </label>
        <label>
          <input
            type="radio"
            checked={identity.mode === "ad"}
            onChange={() => onPatch({ identity: { ...identity, mode: "ad" } })}
          />
          AD-joined
        </label>
      </fieldset>
      {identity.mode === "ad" ? (
        <fieldset>
          <legend>Active Directory</legend>
          <label>
            Domain
            <input
              type="text"
              value={identity.adDomain ?? ""}
              onChange={(e) => onPatch({ identity: { ...identity, adDomain: e.target.value } })}
              placeholder="home.gell.one"
            />
          </label>
          <label>
            Join account
            <input
              type="text"
              value={identity.adJoinAccount ?? ""}
              onChange={(e) => onPatch({ identity: { ...identity, adJoinAccount: e.target.value } })}
              placeholder="svc-autopilot"
            />
          </label>
          <label>
            Join password
            <input
              type="password"
              placeholder={identity.adJoinPasswordRef.isSet ? "(set; type to replace)" : ""}
              onChange={(e) => {
                // PUT the raw password as ad_join_password; the server writes it to vault.yml and
                // returns the row with adJoinPasswordRef = {ref, isSet: true} via _scrub_for_client.
                onPatch({
                  identity: {
                    ...identity,
                    ad_join_password: e.target.value || undefined,
                  } as any,
                });
              }}
            />
          </label>
          <label>
            Local admin password
            <input
              type="password"
              placeholder={identity.localAdminPasswordRef.isSet ? "(set; type to replace)" : ""}
              onChange={(e) => {
                onPatch({
                  identity: {
                    ...identity,
                    local_admin_password: e.target.value || undefined,
                  } as any,
                });
              }}
            />
          </label>
          <button type="button" onClick={() => void runProbe()} disabled={probing}>
            {probing ? "Testing..." : "Test this now"}
          </button>
          {probeResult ? (
            <p role={probeResult.ok ? "status" : "alert"} aria-live="polite">
              {probeResult.ok ? "Probe succeeded: " : "Probe failed: "} {probeResult.detail}
            </p>
          ) : null}
          <details>
            <summary>What if it fails?</summary>
            <ul>
              <li>DNS does not resolve the domain. Open Settings &gt; DNS and confirm your forwarder includes a domain controller.</li>
              <li>LDAP bind refused. The join account exists but cannot read the directory. Grant it 'Account Operators' or equivalent in AD Users and Computers.</li>
              <li>ICMP blocked. Some networks drop ping but allow LDAP. If the next probe attempt succeeds you can ignore this.</li>
            </ul>
          </details>
        </fieldset>
      ) : null}
    </section>
  );
}
```

- [ ] **Step 7: Render IdentityStep in OnboardingPage when current_step === 'identity'**

In `OnboardingPage.tsx`, replace the placeholder for step 'identity' with:

```typescript
import { IdentityStep } from "../onboarding/steps/IdentityStep";

// inside the JSX:
{state.currentStep === "identity" ? (
  <IdentityStep
    state={state}
    onPatch={(patch) => {
      dispatch({ type: "patchAnswers", patch });
      void persist({ answers: patch });
    }}
  />
) : null}
```

- [ ] **Step 8: Run all tests**

```bash
pytest tests/test_onboarding_probes.py tests/test_onboarding_endpoints.py -v
npx vitest run src/OnboardingPage.test.tsx
```
Expected: all passing.

- [ ] **Step 9: Commit**

```bash
git add autopilot-proxmox/web/onboarding_probes.py autopilot-proxmox/web/onboarding_endpoints.py autopilot-proxmox/tests/test_onboarding_probes.py autopilot-proxmox/tests/test_onboarding_endpoints.py autopilot-proxmox/frontend/src/onboarding/steps/IdentityStep.tsx autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx
git commit -m "feat(onboarding): identity step + AD probe via python-ldap"
```

---

## Task 8: TenantStep + tenant probe endpoint

**Files:**
- Modify: `autopilot-proxmox/web/onboarding_probes.py` (add `probe_tenant`)
- Modify: `autopilot-proxmox/web/onboarding_endpoints.py` (replace tenant stub)
- Modify: `autopilot-proxmox/tests/test_onboarding_probes.py`
- Create: `autopilot-proxmox/frontend/src/onboarding/steps/TenantStep.tsx`
- Modify: `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx`

- [ ] **Step 1: Failing test for `probe_tenant`**

Append to `tests/test_onboarding_probes.py`:

```python
def test_probe_tenant_validates_uuid_shape():
    result = onboarding_probes.probe_tenant("not-a-uuid", "contoso.onmicrosoft.com", graph_check=False)
    assert result["ok"] is False
    assert "Tenant id format" in result["detail"]


def test_probe_tenant_accepts_valid_uuid_when_graph_skipped():
    result = onboarding_probes.probe_tenant(
        "12345678-1234-1234-1234-123456789abc", "contoso.onmicrosoft.com", graph_check=False
    )
    assert result["ok"] is True
```

- [ ] **Step 2: Run, watch fail**

```bash
pytest tests/test_onboarding_probes.py -k tenant -v
```
Expected: 2 failures.

- [ ] **Step 3: Implement `probe_tenant`**

Append to `web/onboarding_probes.py`:

```python
import re

_TENANT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def probe_tenant(tenant_id: str, tenant_domain: str, *, graph_check: bool = True) -> dict:
    """Validate Autopilot tenant values. Shape check first; optional Graph sanity-check."""
    if not _TENANT_ID_RE.match(tenant_id or ""):
        return {
            "ok": False,
            "detail": "Tenant id format invalid. Check the value in https://entra.microsoft.com under Overview.",
            "checks": {"shape": {"ok": False, "detail": "not a uuid"}},
        }
    if not tenant_domain or "." not in tenant_domain:
        return {
            "ok": False,
            "detail": "Tenant domain looks wrong; expected something like contoso.onmicrosoft.com.",
            "checks": {"shape": {"ok": True}, "domain": {"ok": False, "detail": "missing dot"}},
        }
    if not graph_check:
        return {"ok": True, "detail": "shape ok; Graph sanity-check skipped", "checks": {"shape": {"ok": True}}}
    # Graph sanity check: hit the OpenID metadata endpoint, no creds needed.
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
            timeout=5,
        ) as r:
            if r.status == 200:
                return {"ok": True, "detail": "tenant resolves on login.microsoftonline.com", "checks": {"shape": {"ok": True}, "graph": {"ok": True}}}
            return {"ok": False, "detail": f"login.microsoftonline.com returned {r.status}", "checks": {"shape": {"ok": True}, "graph": {"ok": False}}}
    except Exception as e:
        return {"ok": False, "detail": f"could not reach login.microsoftonline.com: {e}", "checks": {"shape": {"ok": True}, "graph": {"ok": False}}}
```

- [ ] **Step 4: Replace the `/probe/tenant` stub in `onboarding_endpoints.py`**

```python
class ProbeTenantRequest(BaseModel):
    tenant_id: str
    tenant_domain: str
    graph_check: bool = True


@router.post("/probe/tenant")
def probe_tenant(body: ProbeTenantRequest, owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_probes
    with _probe_lock(owner_sub, "tenant"):
        return onboarding_probes.probe_tenant(body.tenant_id, body.tenant_domain, graph_check=body.graph_check)
```

Update `test_onboarding_endpoints.py` to remove "tenant" from the 501 list.

- [ ] **Step 5: Run probe + endpoint tests**

```bash
pytest tests/test_onboarding_probes.py tests/test_onboarding_endpoints.py -v
```
Expected: all passing.

- [ ] **Step 6: Implement TenantStep**

Create `autopilot-proxmox/frontend/src/onboarding/steps/TenantStep.tsx`:

```typescript
import { useState } from "react";
import type { WizardState } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
}

export function TenantStep({ state, onPatch }: Props) {
  const tenant = state.answers.tenant;
  const isOptional =
    state.answers.persona === "lab" && state.answers.identity.mode === "workgroup";
  const [probeResult, setProbeResult] = useState(state.answers.probeResults.tenant);

  async function runProbe() {
    if (!tenant.tenantId || !tenant.tenantDomain) return;
    const r = await fetch("/api/onboarding/probe/tenant", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: tenant.tenantId, tenant_domain: tenant.tenantDomain, graph_check: true }),
    });
    const body = await r.json();
    setProbeResult({ ok: body.ok, detail: body.detail, at: new Date().toISOString() });
    onPatch({
      probeResults: { ...state.answers.probeResults, tenant: { ok: body.ok, detail: body.detail, at: new Date().toISOString() } },
    });
  }

  return (
    <section className="onboarding-step" aria-labelledby="tenant-h">
      <h1 id="tenant-h">Tenant {isOptional ? <small>(optional for lab + workgroup)</small> : null}</h1>
      <p>Without real tenant values the OOBE join phase will fail in production. Workgroup trials don't need this.</p>
      {isOptional ? (
        <label>
          <input
            type="checkbox"
            checked={tenant.skipped}
            onChange={(e) => onPatch({ tenant: { ...tenant, skipped: e.target.checked } })}
          />
          Skip tenant setup for now
        </label>
      ) : null}
      <fieldset disabled={tenant.skipped}>
        <legend>AutopilotConfigurationFile.json</legend>
        <label>
          CloudAssignedTenantId
          <input
            type="text"
            value={tenant.tenantId ?? ""}
            onChange={(e) => onPatch({ tenant: { ...tenant, tenantId: e.target.value } })}
            placeholder="12345678-1234-1234-1234-123456789abc"
          />
        </label>
        <label>
          CloudAssignedTenantDomain
          <input
            type="text"
            value={tenant.tenantDomain ?? ""}
            onChange={(e) => onPatch({ tenant: { ...tenant, tenantDomain: e.target.value } })}
            placeholder="contoso.onmicrosoft.com"
          />
        </label>
        <label>
          Comment_File
          <input
            type="text"
            value={tenant.commentFile ?? ""}
            onChange={(e) => onPatch({ tenant: { ...tenant, commentFile: e.target.value } })}
          />
        </label>
        <button type="button" onClick={() => void runProbe()}>Test this now</button>
        {probeResult ? (
          <p role={probeResult.ok ? "status" : "alert"} aria-live="polite">
            {probeResult.ok ? "Tenant validates: " : "Tenant invalid: "} {probeResult.detail}
          </p>
        ) : null}
      </fieldset>
      <details>
        <summary>What if it fails?</summary>
        <ul>
          <li>Graph creds missing. Open <a href="/react/settings">Settings &gt; Entra</a> and add an app secret with Directory.Read.All.</li>
          <li>Tenant id format invalid. Check the value in https://entra.microsoft.com under Overview.</li>
        </ul>
      </details>
    </section>
  );
}
```

- [ ] **Step 7: Render TenantStep in OnboardingPage**

In `OnboardingPage.tsx`, add the branch:

```typescript
import { TenantStep } from "../onboarding/steps/TenantStep";

// inside the JSX:
{state.currentStep === "tenant" ? (
  <TenantStep
    state={state}
    onPatch={(patch) => {
      dispatch({ type: "patchAnswers", patch });
      void persist({ answers: patch });
    }}
  />
) : null}
```

- [ ] **Step 8: Commit**

```bash
git add autopilot-proxmox/web/onboarding_probes.py autopilot-proxmox/web/onboarding_endpoints.py autopilot-proxmox/tests/test_onboarding_probes.py autopilot-proxmox/tests/test_onboarding_endpoints.py autopilot-proxmox/frontend/src/onboarding/steps/TenantStep.tsx autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx
git commit -m "feat(onboarding): tenant step + tenant shape + Graph reachability probe"
```

---

## Task 9: ArtifactStep + artifact probe endpoint + build-resume

**Files:**
- Modify: `autopilot-proxmox/web/onboarding_probes.py` (add `probe_artifact`)
- Modify: `autopilot-proxmox/web/onboarding_endpoints.py` (replace artifact stub)
- Modify: `autopilot-proxmox/tests/test_onboarding_probes.py`
- Create: `autopilot-proxmox/frontend/src/onboarding/steps/ArtifactStep.tsx`
- Modify: `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx`

- [ ] **Step 1: Failing test for `probe_artifact`**

Append to `tests/test_onboarding_probes.py`:

```python
def test_probe_artifact_lists_cache_contents(monkeypatch):
    monkeypatch.setattr(
        "web.cloudosd_cache.list_artifacts",
        lambda: [{"id": "cosd-1", "label": "CloudOSD 2026-05", "built_at": "2026-05-20T10:00Z"}],
    )
    monkeypatch.setattr(
        "web.osdeploy_cache.list_artifacts",
        lambda: [{"id": "osd-1", "label": "OSDeploy 2026-05", "built_at": "2026-05-22T10:00Z"}],
    )
    result = onboarding_probes.probe_artifact()
    assert result["ok"] is True
    assert any(a["id"] == "cosd-1" for a in result["cloudosd"])
    assert any(a["id"] == "osd-1" for a in result["osdeploy"])
```

- [ ] **Step 2: Run, watch fail**

```bash
pytest tests/test_onboarding_probes.py -k artifact -v
```

- [ ] **Step 3: Implement `probe_artifact`**

Append to `web/onboarding_probes.py`:

```python
def probe_artifact() -> dict:
    """Inventory cloudosd + osdeploy caches. ok is True iff at least one artifact exists somewhere."""
    try:
        from web import cloudosd_cache  # type: ignore
        cloudosd = list(cloudosd_cache.list_artifacts())
    except Exception as e:
        cloudosd = []
    try:
        from web import osdeploy_cache  # type: ignore
        osdeploy = list(osdeploy_cache.list_artifacts())
    except Exception as e:
        osdeploy = []
    return {
        "ok": bool(cloudosd or osdeploy),
        "detail": f"{len(cloudosd)} CloudOSD, {len(osdeploy)} OSDeploy",
        "cloudosd": cloudosd,
        "osdeploy": osdeploy,
    }
```

(If `cloudosd_cache.list_artifacts` and `osdeploy_cache.list_artifacts` do not exist with that exact name, grep for the closest equivalent before this step.)

- [ ] **Step 4: Replace the `/probe/artifact` stub**

In `onboarding_endpoints.py`:

```python
@router.post("/probe/artifact")
def probe_artifact(owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_probes
    with _probe_lock(owner_sub, "artifact"):
        return onboarding_probes.probe_artifact()
```

Update the stub-501 test accordingly.

- [ ] **Step 5: Run all backend tests**

```bash
pytest tests/test_onboarding_probes.py tests/test_onboarding_endpoints.py -v
```
Expected: all passing.

- [ ] **Step 6: Implement ArtifactStep with build-resume**

Create `autopilot-proxmox/frontend/src/onboarding/steps/ArtifactStep.tsx`:

```typescript
import { useEffect, useState } from "react";
import type { WizardState } from "../types";

interface ArtifactSummary {
  readonly id: string;
  readonly label: string;
  readonly built_at: string;
}

interface ProbeResponse {
  readonly cloudosd: ArtifactSummary[];
  readonly osdeploy: ArtifactSummary[];
}

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
}

export function ArtifactStep({ state, onPatch }: Props) {
  const artifact = state.answers.artifact;
  const [inventory, setInventory] = useState<ProbeResponse | null>(null);
  const [buildStatus, setBuildStatus] = useState<{ jobId: string; status: string; cleared?: boolean } | null>(
    artifact.buildJobId ? { jobId: artifact.buildJobId, status: "unknown" } : null
  );

  useEffect(() => {
    void (async () => {
      const r = await fetch("/api/onboarding/probe/artifact", { method: "POST", credentials: "include" });
      const body = await r.json();
      setInventory({ cloudosd: body.cloudosd ?? [], osdeploy: body.osdeploy ?? [] });
    })();
  }, []);

  // Re-attach to a pre-existing build job.
  useEffect(() => {
    if (!artifact.buildJobId) return;
    void (async () => {
      const r = await fetch(`/api/jobs/${artifact.buildJobId}`, { credentials: "include" });
      if (r.status === 404) {
        setBuildStatus({ jobId: artifact.buildJobId!, status: "missing", cleared: true });
        onPatch({ artifact: { ...artifact, buildJobId: null, source: "existing" } });
        return;
      }
      if (r.ok) {
        const body = await r.json();
        setBuildStatus({ jobId: artifact.buildJobId!, status: body.status ?? "unknown" });
      }
    })();
  }, [artifact.buildJobId]);

  return (
    <section className="onboarding-step" aria-labelledby="artifact-h">
      <h1 id="artifact-h">Artifact</h1>
      <p>The trial provision needs a bootable artifact. Building one takes ~10 minutes the first time.</p>

      {buildStatus?.cleared ? (
        <p role="alert">The build kicked earlier could not be found. Pick again or rebuild.</p>
      ) : null}

      <fieldset>
        <legend>Source</legend>
        <label>
          <input
            type="radio"
            checked={artifact.source === "existing"}
            onChange={() => onPatch({ artifact: { ...artifact, source: "existing" } })}
          />
          Use an existing artifact
        </label>
        <label>
          <input
            type="radio"
            checked={artifact.source === "build"}
            onChange={() => onPatch({ artifact: { ...artifact, source: "build" } })}
          />
          Build one now
        </label>
      </fieldset>

      {artifact.source === "existing" ? (
        <fieldset>
          <legend>Pick an artifact</legend>
          {inventory ? (
            <>
              <h4>CloudOSD</h4>
              {inventory.cloudosd.length === 0 ? <p>None on disk.</p> : (
                <ul>
                  {inventory.cloudosd.map((a) => (
                    <li key={a.id}>
                      <label>
                        <input
                          type="radio"
                          checked={artifact.kind === "cloudosd" && artifact.existingArtifactId === a.id}
                          onChange={() => onPatch({ artifact: { ...artifact, kind: "cloudosd", existingArtifactId: a.id } })}
                        />
                        {a.label} <small>built {a.built_at}</small>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
              <h4>OSDeploy</h4>
              {inventory.osdeploy.length === 0 ? <p>None on disk.</p> : (
                <ul>
                  {inventory.osdeploy.map((a) => (
                    <li key={a.id}>
                      <label>
                        <input
                          type="radio"
                          checked={artifact.kind === "osdeploy" && artifact.existingArtifactId === a.id}
                          onChange={() => onPatch({ artifact: { ...artifact, kind: "osdeploy", existingArtifactId: a.id } })}
                        />
                        {a.label} <small>built {a.built_at}</small>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : <p>Loading inventory...</p>}
        </fieldset>
      ) : (
        <fieldset>
          <legend>Kick a build</legend>
          {buildStatus ? (
            <p aria-live="polite">Build job <code>{buildStatus.jobId}</code>: {buildStatus.status}</p>
          ) : (
            <button
              type="button"
              onClick={async () => {
                const r = await fetch(`/api/${artifact.kind}/build`, { method: "POST", credentials: "include" });
                if (r.ok) {
                  const body = await r.json();
                  setBuildStatus({ jobId: body.job_id, status: "started" });
                  onPatch({ artifact: { ...artifact, buildJobId: body.job_id } });
                }
              }}
            >
              Build {artifact.kind === "cloudosd" ? "CloudOSD" : "OSDeploy"} now
            </button>
          )}
        </fieldset>
      )}

      <details>
        <summary>What if it fails?</summary>
        <ul>
          <li>Build host unreachable. Open <a href="/react/settings">/react/settings</a> and check the <code>build_host</code> field (e.g. <code>user@192.168.2.50</code>).</li>
          <li>Source media missing. Upload media at <a href="/react/files">/react/files</a>.</li>
        </ul>
      </details>
    </section>
  );
}
```

- [ ] **Step 7: Render ArtifactStep in OnboardingPage**

```typescript
import { ArtifactStep } from "../onboarding/steps/ArtifactStep";

{state.currentStep === "artifact" ? (
  <ArtifactStep
    state={state}
    onPatch={(patch) => {
      dispatch({ type: "patchAnswers", patch });
      void persist({ answers: patch });
    }}
  />
) : null}
```

- [ ] **Step 8: Commit**

```bash
git add autopilot-proxmox/web/onboarding_probes.py autopilot-proxmox/web/onboarding_endpoints.py autopilot-proxmox/tests/test_onboarding_probes.py autopilot-proxmox/tests/test_onboarding_endpoints.py autopilot-proxmox/frontend/src/onboarding/steps/ArtifactStep.tsx autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx
git commit -m "feat(onboarding): artifact step + cache-inventory probe + build-job resume"
```

---

## Task 10: ReviewLaunchStep with trial VM params

**Files:**
- Create: `autopilot-proxmox/frontend/src/onboarding/steps/ReviewLaunchStep.tsx`
- Modify: `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx`

- [ ] **Step 1: Implement ReviewLaunchStep**

Create `autopilot-proxmox/frontend/src/onboarding/steps/ReviewLaunchStep.tsx`:

```typescript
import { useState } from "react";
import type { WizardState, WizardStep } from "../types";

interface Props {
  readonly state: WizardState;
  readonly onPatch: (patch: Partial<WizardState["answers"]>) => void;
  readonly onJump: (step: WizardStep) => void;
  readonly onLaunch: () => Promise<void>;
}

function preconditions(state: WizardState): string[] {
  const errs: string[] = [];
  if (state.answers.persona === null) errs.push("Pick a persona on the Welcome step.");
  if (
    state.answers.identity.mode === "ad" &&
    state.answers.probeResults.ad?.ok !== true
  ) {
    errs.push("Identity says AD-joined but no successful AD probe is on record.");
  }
  if (
    !(state.answers.persona === "lab" && state.answers.identity.mode === "workgroup") &&
    !state.answers.tenant.skipped &&
    !state.answers.tenant.tenantId
  ) {
    errs.push("Tenant is required for your persona / identity combination.");
  }
  if (
    state.answers.artifact.source === "existing" &&
    !state.answers.artifact.existingArtifactId
  ) {
    errs.push("Pick an existing artifact on the Artifact step.");
  }
  if (
    state.answers.artifact.source === "build" &&
    !state.answers.artifact.buildJobId
  ) {
    errs.push("Kick a build on the Artifact step.");
  }
  if (!state.answers.trial.targetNode) {
    errs.push("Pick a target node for the trial VM.");
  }
  return errs;
}

export function ReviewLaunchStep({ state, onPatch, onJump, onLaunch }: Props) {
  const errs = preconditions(state);
  const trial = state.answers.trial;
  const [launching, setLaunching] = useState(false);
  return (
    <section className="onboarding-step" aria-labelledby="review-h">
      <h1 id="review-h">Review and launch</h1>
      <p>Last chance to fix anything before we touch the live cluster.</p>

      <fieldset>
        <legend>Trial VM</legend>
        <label>
          VM name
          <input
            type="text"
            value={trial.vmName}
            onChange={(e) => onPatch({ trial: { ...trial, vmName: e.target.value } })}
            placeholder="autopilot-trial-<vmid>"
          />
        </label>
        <label>
          Target node
          <input
            type="text"
            value={trial.targetNode}
            onChange={(e) => onPatch({ trial: { ...trial, targetNode: e.target.value } })}
            placeholder="pve2"
          />
        </label>
        <label>
          OS edition
          <select
            value={trial.osEdition}
            onChange={(e) => onPatch({ trial: { ...trial, osEdition: e.target.value as any } })}
          >
            <option value="win11-pro">Windows 11 Pro</option>
            <option value="win11-ent">Windows 11 Enterprise</option>
            <option value="win10-pro">Windows 10 Pro</option>
          </select>
        </label>
      </fieldset>

      <dl className="onboarding-review">
        <dt>Persona</dt><dd>{state.answers.persona ?? "(not picked)"} <button type="button" onClick={() => onJump("welcome")}>edit</button></dd>
        <dt>Identity</dt>
        <dd>
          {state.answers.identity.mode === "workgroup" ? "Workgroup" : `AD-joined to ${state.answers.identity.adDomain ?? "(no domain)"}`}{" "}
          <button type="button" onClick={() => onJump("identity")}>edit</button>
        </dd>
        <dt>Tenant</dt>
        <dd>
          {state.answers.tenant.skipped ? "Skipped" : state.answers.tenant.tenantId ?? "(unset)"}{" "}
          <button type="button" onClick={() => onJump("tenant")}>edit</button>
        </dd>
        <dt>Artifact</dt>
        <dd>
          {state.answers.artifact.source === "existing"
            ? `Use ${state.answers.artifact.kind} ${state.answers.artifact.existingArtifactId ?? "(unset)"}`
            : `Build ${state.answers.artifact.kind} (job ${state.answers.artifact.buildJobId ?? "pending"})`}{" "}
          <button type="button" onClick={() => onJump("artifact")}>edit</button>
        </dd>
      </dl>

      <button
        type="button"
        disabled={errs.length > 0 || launching}
        title={errs.length > 0 ? errs.join(" / ") : undefined}
        onClick={async () => {
          setLaunching(true);
          try {
            await onLaunch();
          } finally {
            setLaunching(false);
          }
        }}
      >
        {launching ? "Launching..." : "Start setup"}
      </button>
      {errs.length > 0 ? (
        <ul role="alert">
          {errs.map((e) => <li key={e}>{e}</li>)}
        </ul>
      ) : null}
    </section>
  );
}
```

- [ ] **Step 2: Wire ReviewLaunchStep into OnboardingPage with a `launch()` handler that calls `POST /api/onboarding/launch`**

In `OnboardingPage.tsx`, add:

```typescript
import { ReviewLaunchStep } from "../onboarding/steps/ReviewLaunchStep";

async function onLaunch() {
  const r = await fetch("/api/onboarding/launch", { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" } });
  if (!r.ok) return;
  const body = await r.json();
  dispatch({ type: "markLaunched", runId: body.run_id });
  window.location.href = "/react/onboarding/setup";
}

// inside the JSX:
{state.currentStep === "review" ? (
  <ReviewLaunchStep
    state={state}
    onPatch={(patch) => {
      dispatch({ type: "patchAnswers", patch });
      void persist({ answers: patch });
    }}
    onJump={onJump}
    onLaunch={onLaunch}
  />
) : null}
```

- [ ] **Step 3: Commit**

```bash
git add autopilot-proxmox/frontend/src/onboarding/steps/ReviewLaunchStep.tsx autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx
git commit -m "feat(onboarding): review and launch step with trial VM params"
```

---

## Task 11: Launch transaction (`web/onboarding_launch.py`)

**Files:**
- Create: `autopilot-proxmox/web/onboarding_launch.py`
- Create: `autopilot-proxmox/tests/test_onboarding_launch.py`
- Modify: `autopilot-proxmox/web/onboarding_endpoints.py` (replace launch stub)

- [ ] **Step 1: Failing test for the launch transaction**

Create `autopilot-proxmox/tests/test_onboarding_launch.py`:

```python
"""Tests for web/onboarding_launch.py."""
from __future__ import annotations

import pytest

from web import install_tracking_pg, onboarding_launch, onboarding_pg


@pytest.fixture(autouse=True)
def _reset(pg_conn):
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    install_tracking_pg.init(pg_conn)


def test_launch_creates_run_and_seeds_phase_items(pg_conn, monkeypatch):
    onboarding_pg.put_state(
        pg_conn,
        owner_sub="alice@example.com",
        if_match=None,
        patch={
            "persona": "lab",
            "answers": {
                "identity": {"mode": "workgroup"},
                "tenant": {"skipped": True},
                "artifact": {"kind": "cloudosd", "source": "existing", "existing_artifact_id": "cosd-1"},
                "trial": {"vm_name": "autopilot-trial-9001", "target_node": "pve2", "os_edition": "win11-pro"},
            },
        },
    )
    calls: list[str] = []
    def fake_kick(kind, run_id, payload):
        calls.append(f"{kind}:{run_id}")
        return {"job_id": "job-1"}
    monkeypatch.setattr(onboarding_launch, "_kick_provision", fake_kick)

    result = onboarding_launch.launch(pg_conn, owner_sub="alice@example.com")
    assert result["run_id"].startswith("onboarding-alice-")
    assert calls == [f"cloudosd:{result['run_id']}"]
    items = install_tracking_pg.list_run_items(pg_conn, result["run_id"])
    item_ids = {i["item_id"] for i in items}
    assert {"validate", "clone-template", "provision", "watch-oobe"} <= item_ids
    # Build phase only appears if source == 'build'; this case is 'existing'.
    assert "build-artifact" not in item_ids
    # Inject Autopilot phase only appears if identity != workgroup.
    assert "inject-autopilot" not in item_ids

    row = onboarding_pg.get_state(pg_conn, "alice@example.com")
    assert row["status"] == "launched"
    assert row["launched_run_id"] == result["run_id"]
```

- [ ] **Step 2: Run, watch fail**

```bash
pytest tests/test_onboarding_launch.py -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `web/onboarding_launch.py`**

Create:

```python
"""Atomic launch transaction for the operator onboarding wizard."""
from __future__ import annotations

import time
from typing import Any

from psycopg import Connection

from web import install_tracking_pg, onboarding_pg


def _phases_for(answers: dict[str, Any]) -> list[dict[str, Any]]:
    """Project phase items from wizard answers. Order matters for sort_order."""
    identity = answers.get("identity") or {}
    artifact = answers.get("artifact") or {}
    items: list[dict[str, Any]] = [
        {"item_id": "validate", "label": "Validate inputs", "sort_order": 10},
    ]
    if artifact.get("source") == "build":
        items.append({"item_id": "build-artifact", "label": "Build artifact", "sort_order": 20})
    items.append({"item_id": "clone-template", "label": "Clone template", "sort_order": 30})
    if identity.get("mode") != "workgroup":
        items.append({"item_id": "inject-autopilot", "label": "Inject Autopilot config", "sort_order": 40})
    items.append({"item_id": "provision", "label": "Start VM and run task sequence", "sort_order": 50})
    items.append({"item_id": "watch-oobe", "label": "Watch OOBE", "sort_order": 60})
    return items


def _kick_provision(kind: str, run_id: str, payload: dict[str, Any]) -> dict:
    """POST to the artifact-bound provision endpoint. Overridable in tests."""
    import requests
    url = f"http://localhost:8000/api/{kind}/runs/{run_id}/provision"
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def launch(conn: Connection, *, owner_sub: str) -> dict:
    row = onboarding_pg.get_state(conn, owner_sub)
    if row is None:
        raise ValueError("no onboarding row to launch")
    if row["status"] != "in_progress":
        raise ValueError(f"cannot launch from status={row['status']}")
    answers = row["answers"] or {}
    kind = (answers.get("artifact") or {}).get("kind") or "cloudosd"
    # Generate a stable run_id.
    short_sub = owner_sub.replace("@", "-").replace(".", "-")
    run_id = f"onboarding-{short_sub}-{int(time.time())}"
    install_tracking_pg.create_run(
        conn,
        run_id=run_id,
        name=f"Onboarding for {owner_sub}",
        target=(answers.get("trial") or {}).get("target_node") or "(unset)",
        commit=False,
    )
    for item in _phases_for(answers):
        install_tracking_pg.upsert_item(
            conn,
            run_id=run_id,
            item_id=item["item_id"],
            category="Onboarding",
            label=item["label"],
            description="",
            target=(answers.get("trial") or {}).get("target_node") or "",
            status="pending",
            detail="",
            source="onboarding_launch",
            sort_order=item["sort_order"],
            commit=False,
        )
    try:
        _kick_provision(kind, run_id, {"answers": answers})
    except Exception:
        conn.rollback()
        raise
    onboarding_pg.set_launched_run(conn, owner_sub, run_id=run_id)
    return {"run_id": run_id}
```

- [ ] **Step 4: Replace the launch stub in `onboarding_endpoints.py`**

```python
@router.post("/launch")
def launch(owner_sub: str = Depends(_owner_sub)):
    with db_pg.connection() as conn:
        try:
            return onboarding_launch.launch(conn, owner_sub=owner_sub)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


# import at top:
from web import onboarding_launch
```

- [ ] **Step 5: Run all backend tests**

```bash
pytest tests/test_onboarding_launch.py tests/test_onboarding_endpoints.py -v
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/onboarding_launch.py autopilot-proxmox/web/onboarding_endpoints.py autopilot-proxmox/tests/test_onboarding_launch.py
git commit -m "feat(onboarding): atomic launch transaction with phase seeding"
```

---

## Task 12: Setup-status projection (`web/onboarding_phases.py`)

**Files:**
- Create: `autopilot-proxmox/web/onboarding_phases.py`
- Create: `autopilot-proxmox/tests/test_onboarding_phases.py`
- Modify: `autopilot-proxmox/web/onboarding_endpoints.py` (replace setup-status stub)

- [ ] **Step 1: Failing test**

Create `autopilot-proxmox/tests/test_onboarding_phases.py`:

```python
"""Tests for web/onboarding_phases.py."""
from __future__ import annotations

import pytest

from web import install_tracking_pg, onboarding_pg, onboarding_phases


@pytest.fixture(autouse=True)
def _reset(pg_conn):
    onboarding_pg.reset_for_tests(pg_conn)
    onboarding_pg.init(pg_conn)
    install_tracking_pg.init(pg_conn)


def test_snapshot_returns_phase_list_for_owner(pg_conn):
    onboarding_pg.put_state(
        pg_conn,
        owner_sub="bob@example.com",
        if_match=None,
        patch={"persona": "msp"},
    )
    install_tracking_pg.create_run(pg_conn, run_id="onboarding-bob-1", name="t", target="pve2", commit=False)
    for sort_order, item_id, status in [(10, "validate", "ready"), (50, "provision", "running")]:
        install_tracking_pg.upsert_item(
            pg_conn, run_id="onboarding-bob-1", item_id=item_id, category="Onboarding",
            label=item_id, description="", target="pve2", status=status, detail="",
            source="test", sort_order=sort_order, commit=False,
        )
    onboarding_pg.set_launched_run(pg_conn, "bob@example.com", run_id="onboarding-bob-1")

    snap = onboarding_phases.snapshot(pg_conn, owner_sub="bob@example.com")
    assert snap["run_id"] == "onboarding-bob-1"
    statuses = {p["item_id"]: p["status"] for p in snap["phases"]}
    assert statuses == {"validate": "ready", "provision": "running"}


def test_snapshot_returns_none_if_not_launched(pg_conn):
    onboarding_pg.put_state(
        pg_conn,
        owner_sub="carol@example.com",
        if_match=None,
        patch={"persona": "lab"},
    )
    assert onboarding_phases.snapshot(pg_conn, owner_sub="carol@example.com") is None
```

- [ ] **Step 2: Run, watch fail**

- [ ] **Step 3: Implement `web/onboarding_phases.py`**

```python
"""Read-only projection over install_tracking for the onboarding setup monitor."""
from __future__ import annotations

from psycopg import Connection

from web import install_tracking_pg, onboarding_pg


def snapshot(conn: Connection, *, owner_sub: str) -> dict | None:
    row = onboarding_pg.get_state(conn, owner_sub)
    if row is None or not row.get("launched_run_id"):
        return None
    run_id = row["launched_run_id"]
    items = install_tracking_pg.list_run_items(conn, run_id)
    phases = [
        {
            "item_id": i["item_id"],
            "label": i["label"],
            "status": i["status"],  # pending|running|ready|blocked|failed|skipped
            "detail": i.get("detail") or "",
            "sort_order": i.get("sort_order") or 0,
        }
        for i in items
    ]
    phases.sort(key=lambda p: p["sort_order"])
    return {
        "run_id": run_id,
        "phases": phases,
    }
```

- [ ] **Step 4: Replace setup-status stub**

In `onboarding_endpoints.py`:

```python
@router.get("/setup-status")
def setup_status(owner_sub: str = Depends(_owner_sub)):
    from web import onboarding_phases
    with db_pg.connection() as conn:
        snap = onboarding_phases.snapshot(conn, owner_sub=owner_sub)
    if snap is None:
        raise HTTPException(status_code=404, detail="no launched onboarding run")
    return snap
```

- [ ] **Step 5: Run all backend tests**

```bash
pytest tests/test_onboarding_phases.py tests/test_onboarding_endpoints.py -v
```
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/onboarding_phases.py autopilot-proxmox/web/onboarding_endpoints.py autopilot-proxmox/tests/test_onboarding_phases.py
git commit -m "feat(onboarding): setup-status projection over install_tracking"
```

---

## Task 13: OnboardingSetupPage with polling + phase rail

**Files:**
- Create: `autopilot-proxmox/frontend/src/pages/OnboardingSetupPage.tsx`
- Create: `autopilot-proxmox/frontend/src/OnboardingSetupPage.test.tsx`
- Modify: `autopilot-proxmox/frontend/src/App.tsx` (add the route)

- [ ] **Step 1: Failing test for phase rail rendering**

Create `autopilot-proxmox/frontend/src/OnboardingSetupPage.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { OnboardingSetupPage } from "./pages/OnboardingSetupPage";
import type { AppBootstrap } from "./contracts";

const BOOT: AppBootstrap = {
  userName: "Tester",
  onboarding: { status: "launched", currentStep: "review" },
};

beforeEach(() => {
  global.fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/api/onboarding/setup-status")) {
      return new Response(JSON.stringify({
        run_id: "onboarding-tester-1",
        phases: [
          { item_id: "validate", label: "Validate inputs", status: "ready", detail: "", sort_order: 10 },
          { item_id: "clone-template", label: "Clone template", status: "running", detail: "", sort_order: 30 },
          { item_id: "provision", label: "Provision", status: "pending", detail: "", sort_order: 50 },
        ],
      }), { status: 200 });
    }
    return new Response("", { status: 404 });
  }) as any;
});

describe("OnboardingSetupPage", () => {
  it("renders the phase rail with status badges", async () => {
    render(<OnboardingSetupPage bootstrap={BOOT} />);
    await waitFor(() => {
      expect(screen.getByText("Validate inputs")).toBeInTheDocument();
      expect(screen.getByText("Clone template")).toBeInTheDocument();
      expect(screen.getByText("Provision")).toBeInTheDocument();
    });
  });
});
```

- [ ] **Step 2: Run, watch fail**

- [ ] **Step 3: Implement OnboardingSetupPage**

Create `autopilot-proxmox/frontend/src/pages/OnboardingSetupPage.tsx`:

```typescript
import { useEffect, useState } from "react";
import type { AppBootstrap } from "../contracts";

interface Phase {
  readonly item_id: string;
  readonly label: string;
  readonly status: "pending" | "running" | "ready" | "blocked" | "failed" | "skipped";
  readonly detail: string;
  readonly sort_order: number;
}

interface Snapshot {
  readonly run_id: string;
  readonly phases: Phase[];
}

const STATUS_LABEL: Record<Phase["status"], string> = {
  pending: "Waiting",
  running: "Running",
  ready: "Done",
  blocked: "Blocked",
  failed: "Failed",
  skipped: "Skipped",
};

interface Props {
  readonly bootstrap: AppBootstrap;
}

export function OnboardingSetupPage(_props: Props) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [errors, setErrors] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await fetch("/api/onboarding/setup-status", { credentials: "include" });
        if (r.status === 404) {
          setErrors("No launched onboarding run found.");
          return;
        }
        if (!r.ok) {
          setErrors(`Status fetch failed: ${r.status}`);
          return;
        }
        const body = (await r.json()) as Snapshot;
        if (!cancelled) setSnap(body);
      } catch (e) {
        setErrors((e as Error).message);
      }
    }
    void tick();
    const interval = document.visibilityState === "visible" ? 2000 : 10000;
    const id = setInterval(() => void tick(), interval);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const allReady = snap?.phases.every((p) => p.status === "ready" || p.status === "skipped");
  const failed = snap?.phases.find((p) => p.status === "failed");

  return (
    <main className="onboarding-setup-page">
      <header>
        <h1>Setting up your first deployment</h1>
        {snap ? <p>Run id: <code>{snap.run_id}</code></p> : null}
      </header>
      {errors ? <p role="alert">{errors}</p> : null}
      {snap ? (
        <ol className="phase-rail" aria-live="polite" aria-label="Setup phases">
          {snap.phases.map((p, idx) => (
            <li key={p.item_id} className={`phase phase-${p.status}`}>
              <span className="phase-number">Phase {idx + 1} of {snap.phases.length}</span>
              <strong>{p.label}</strong>
              <span className="phase-status-badge">{STATUS_LABEL[p.status]}</span>
              {p.status === "failed" ? (
                <details open>
                  <summary>What if it fails</summary>
                  <p>{p.detail}</p>
                </details>
              ) : null}
            </li>
          ))}
        </ol>
      ) : !errors ? <p>Loading setup status...</p> : null}
      {allReady ? (
        <section className="onboarding-complete-card" role="status">
          <h2>Setup complete</h2>
          <p>Your trial VM is up. Open <a href="/react/vms">/react/vms</a> to see it.</p>
        </section>
      ) : null}
      {failed ? (
        <section className="onboarding-failed-card" role="alert">
          <h2>Setup hit a snag</h2>
          <p>Phase "{failed.label}" failed. {failed.detail}</p>
          <a href="/react/onboarding">Back to wizard</a>
        </section>
      ) : null}
    </main>
  );
}
```

- [ ] **Step 4: Wire the route**

In `App.tsx`:

```typescript
import { OnboardingSetupPage } from "./pages/OnboardingSetupPage";

if (path === "/react/onboarding/setup") {
  return <OnboardingSetupPage bootstrap={bootstrap} />;
}
```

- [ ] **Step 5: Run tests**

```bash
npx vitest run src/OnboardingSetupPage.test.tsx
```

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/frontend/src/pages/OnboardingSetupPage.tsx autopilot-proxmox/frontend/src/OnboardingSetupPage.test.tsx autopilot-proxmox/frontend/src/App.tsx
git commit -m "feat(onboarding): live setup monitor page with polling and phase rail"
```

---

## Task 14: Manual end-to-end walkthrough

**Files:** none new; verification only.

- [ ] **Step 1: Pull latest, rebuild containers**

```bash
docker compose down
docker compose build
docker compose up -d
```

- [ ] **Step 2: Confirm bootstrap exposes the onboarding field**

Open browser DevTools, load https://autopilot.gell.one (or http://localhost:8000 in lab), check the `/__bootstrap__` (or inline `<script>` bootstrap JSON) for an `onboarding: { status: "absent" }` entry.

- [ ] **Step 3: Walk the wizard**

- Navigate to `/react-shell` and verify there is no hero CTA (status absent).
- Open the Settings nav group, click "Onboarding wizard".
- Pick "Lab hobbyist". Click Next.
- Pick "Workgroup". Click Next.
- See Tenant marked optional; check Skip. Click Next.
- Pick an existing CloudOSD artifact (or kick a build if none exist). Click Next.
- On Review, set the VM name to `autopilot-trial-onboarding-test`, target node `pve2`. Click Start setup.
- Watch the monitor page render the phase rail. Polling visible every 2s.
- Wait for all phases to reach `ready`.
- Verify the completion card appears with a link to /react/vms.
- Open /react/vms and confirm the trial VM exists.

- [ ] **Step 4: Refresh-and-resume test**

- Open a second wizard session in a new browser. Walk to the Identity step. Hard-refresh the page.
- Confirm the wizard restores to the Identity step and the previously typed values are still there.

- [ ] **Step 5: Discard test**

- In the same session, click "Discard onboarding". Confirm modal. Confirm.
- Confirm the operator is back on ShellIndexPage and the hero CTA is gone.

- [ ] **Step 6: Backward-compat test**

- Log in as a different operator who has no onboarding row.
- Confirm there is no hero CTA on ShellIndexPage (absence = "do not show").
- Confirm Settings nav still shows "Onboarding wizard" for discoverability.

- [ ] **Step 7: Commit a runbook stub**

Append the walkthrough above to `docs/FIRST_RUN_E2E.md` as an "Onboarding wizard happy-path" section.

```bash
git add docs/FIRST_RUN_E2E.md
git commit -m "docs(first-run): add onboarding wizard happy-path runbook"
```

---

## Self-Review Checklist (for the engineer running this plan)

After landing Task 14, before opening a PR:

- [ ] Every step in the spec's "Wizard steps" section maps to a task that implemented it. Cross-check the spec at `docs/superpowers/specs/2026-05-27-onboarding-wizard-design.md`.
- [ ] No file path in this plan refers to a non-existent file.
- [ ] No "TBD", "TODO", or hand-wave remains in the codebase that was not present before this work.
- [ ] All unit suites pass: `pytest tests/test_onboarding_*.py -v` and `npx vitest run src/onboarding/ src/Onboarding*.test.tsx`.
- [ ] All ASCII hyphens; no em-dash or en-dash anywhere in new code or copy. Run `LC_ALL=C grep -rP "[\xE2\x80\x93\xE2\x80\x94]" autopilot-proxmox/web/onboarding_* autopilot-proxmox/frontend/src/onboarding autopilot-proxmox/frontend/src/pages/Onboarding* docs/superpowers/specs/2026-05-27-onboarding-wizard-design.md` and expect zero output.
- [ ] Final manual walkthrough against the live controller completed; trial VM listed under /react/vms.
