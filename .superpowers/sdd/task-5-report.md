# Task 5 Report: Labs API Router And App Wiring

## What I implemented
- Added `autopilot-proxmox/web/managed_labs_endpoints.py` with the `/api/labs` router.
- Wired the router into `autopilot-proxmox/web/app.py` immediately after the SDN router.
- Added `managed_labs_pg` to app database initialization so the managed-labs tables are created on startup.
- Implemented endpoints for:
  - `GET /api/labs/page`
  - `GET /api/labs`
  - `POST /api/labs`
  - `GET /api/labs/{lab_id}`
  - `POST /api/labs/{lab_id}/reconcile`
  - `POST /api/labs/{lab_id}/fixes/run-pending`
  - `POST /api/labs/{lab_id}/fixes/{fix_id}/run`
- Added endpoint tests covering empty page output, lab creation/list/fetch, reconcile planning, and delegation to the managed-labs network executor.

## What I tested and results
- `python3 -m pytest autopilot-proxmox/tests/test_managed_labs_endpoints.py -q`
  - Result: passed
- `python3 -m pytest autopilot-proxmox/tests/test_managed_labs_pg.py autopilot-proxmox/tests/test_managed_labs_reconciler.py autopilot-proxmox/tests/test_managed_labs_network.py autopilot-proxmox/tests/test_managed_labs_endpoints.py -q`
  - Result: passed

## TDD Evidence
### RED
Command:
```bash
python3 -m pytest autopilot-proxmox/tests/test_managed_labs_endpoints.py -q
```
Expected failure:
- All three tests failed with `404 Not Found` because `/api/labs` routes were missing.

Observed output snippet:
```text
assert 404 == 200
assert 404 == 201
```

### GREEN
Command:
```bash
python3 -m pytest autopilot-proxmox/tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
3 passed
```

## Files changed
- `autopilot-proxmox/web/managed_labs_endpoints.py`
- `autopilot-proxmox/web/app.py`
- `autopilot-proxmox/tests/test_managed_labs_endpoints.py`

## Self-review findings
- The router follows the existing SDN endpoint pattern and keeps DB access behind a local `_conn()` helper.
- Reconcile now starts a run, collects SDN inventory through `_proxmox_api`, plans network reconciliation, and finishes the run with the planned status.
- Fix endpoints delegate to `managed_labs_network` only, which keeps execution logic out of the router layer.
- Startup initialization now includes `managed_labs_pg`, so the new endpoints are backed by the managed-labs schema automatically.

## Concerns
- None beyond the existing test-suite warnings from FastAPI deprecations and authlib deprecation notices.


## Task 5 Fix Implementer Follow-up (2026-06-21)

### Scope handled
- Fixed `POST /api/labs/{lab_id}/fixes/{fix_id}/run` so the router verifies the fix belongs to the route lab before delegating to `managed_labs_network.execute_fix_action()`.
- Fixed `POST /api/labs/{lab_id}/reconcile` so a started reconcile run is always closed as `failed` when inventory/planning raises, and the lab no longer remains in `validating`.
- Left `POST /api/labs` partial-create rollback unchanged and documented the concern below because `create_lab()` commits internally and there is no existing delete helper in the allowed file scope.

### TDD evidence
#### RED
Command:
```bash
python3 -m pytest tests/test_managed_labs_endpoints.py -q
```
Observed failures:
```text
FAILED tests/test_managed_labs_endpoints.py::test_run_fix_rejects_fix_from_another_lab
FAILED tests/test_managed_labs_endpoints.py::test_reconcile_failure_finishes_run_and_blocks_lab
```
Key failure details:
- Cross-lab fix execution returned `200` instead of rejecting the mismatched `fix_id`.
- Reconcile inventory failure bubbled `RuntimeError: inventory exploded` and left the endpoint without a terminal run closure.

#### GREEN
Command:
```bash
python3 -m pytest tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
5 passed, 7 warnings in 1.53s
```

### Regression coverage
Command:
```bash
python3 -m pytest tests/test_managed_labs_pg.py tests/test_managed_labs_reconciler.py tests/test_managed_labs_network.py tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
30 passed, 7 warnings in 3.76s
```

### Files changed
- `autopilot-proxmox/web/managed_labs_endpoints.py`
- `autopilot-proxmox/tests/test_managed_labs_endpoints.py`

### Concern
- `POST /api/labs` can still leave a partially-created lab behind if a reservation step fails after `managed_labs_pg.create_lab()` commits. Fixing that cleanly appears to require either changing `create_lab()` transaction behavior or adding deletion/cleanup support outside the requested file scope, so I did not widen into it here.

## Task 5 Fix Implementer Follow-up (2026-06-22)

### Scope handled
- Added a regression test that forces `POST /api/labs` to fail after `managed_labs_pg.create_lab()` commits but before CIDR reservation completes.
- Added `managed_labs_pg.delete_lab(conn, lab_id)` as a narrow cleanup helper that relies on existing `ON DELETE CASCADE` relationships.
- Updated `web/managed_labs_endpoints.py` so reservation failures during `POST /api/labs` delete the just-created lab before returning an HTTP failure.

### Root cause
- `managed_labs_pg.create_lab()` commits the lab row and initial event immediately.
- `POST /api/labs` then reserves `group_tag` and `cidr` in separate calls.
- If a later reservation raises, the API failed after durable state had already been made visible through `/api/labs/page`.

### TDD evidence
#### RED
Command:
```bash
python3 -m pytest tests/test_managed_labs_endpoints.py -q
```
Observed failure:
```text
FAILED tests/test_managed_labs_endpoints.py::test_create_lab_rolls_back_visible_state_when_reservation_fails
E   ValueError: cidr reservation exploded
```
Meaning:
- The test forced the second reservation to fail.
- The router propagated the exception without cleanup, proving the create flow was not atomic.

#### GREEN
Command:
```bash
python3 -m pytest tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
6 passed, 7 warnings in 2.14s
```
Meaning:
- The new regression passed.
- `/api/labs/page` remained empty after the forced reservation failure.

### Regression coverage
Command:
```bash
python3 -m pytest tests/test_managed_labs_pg.py tests/test_managed_labs_reconciler.py tests/test_managed_labs_network.py tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
31 passed, 7 warnings in 3.81s
```

### Files changed
- `autopilot-proxmox/web/managed_labs_pg.py`
- `autopilot-proxmox/web/managed_labs_endpoints.py`
- `autopilot-proxmox/tests/test_managed_labs_endpoints.py`

### Concerns
- The router now cleans up durable state after reservation failures, but `create_lab()` still commits before the reservation phase begins. This follow-up keeps scope narrow by compensating in the router rather than refactoring the transaction boundaries across existing helpers.

## Task 5 Fix Implementer Follow-up (2026-06-22, transactional create path)

### Scope handled
- Replaced the `POST /api/labs` compensating delete flow with a single transaction that creates the lab, records `lab_created`, reserves `group_tag`, and reserves `cidr` before committing.
- Added optional `commit: bool = True` parameters to `managed_labs_pg.create_lab()`, `managed_labs_pg.reserve_value()`, and `managed_labs_pg.record_event()` so existing call sites keep current behavior while the router can compose them transactionally.
- Kept the new endpoint regression focused on a database-aborted reservation path instead of a plain Python exception.

### Root cause
- `managed_labs_pg.create_lab()` committed the lab row and `lab_created` audit event before either reservation ran.
- When a later reservation failed at the database level, the transaction became aborted.
- The router then tried to reuse that aborted transaction for `delete_lab()`, so the partial lab could survive and the cleanup path also conflicted with append-only audit intent.

### TDD evidence
#### RED
Command:
```bash
python3 -m pytest tests/test_managed_labs_endpoints.py -q
```
Observed failure:
```text
FAILED tests/test_managed_labs_endpoints.py::test_create_lab_rolls_back_visible_state_when_database_reservation_fails
E   AssertionError: assert [{'created_at': ... 'group_tag': 'RBK-Lab', ...}] == []
```
Meaning:
- The test forced a database-side failure during CIDR reservation with `SELECT 1 / 0` inside the reservation path.
- `/api/labs/page` still showed the committed lab afterward, proving the old create flow was not atomic.

#### GREEN
Command:
```bash
python3 -m pytest tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
6 passed, 7 warnings in 2.18s
```
Meaning:
- The router now rolls the transaction back instead of deleting after commit.
- `/api/labs/page` stays empty after the forced database failure.

### Regression coverage
Command:
```bash
python3 -m pytest tests/test_managed_labs_pg.py tests/test_managed_labs_reconciler.py tests/test_managed_labs_network.py tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
31 passed, 7 warnings in 3.87s
```

### Files changed
- `autopilot-proxmox/web/managed_labs_pg.py`
- `autopilot-proxmox/web/managed_labs_endpoints.py`
- `autopilot-proxmox/tests/test_managed_labs_endpoints.py`

### Concerns
- None beyond the existing FastAPI/authlib deprecation warnings already present in the suite.


## Task 5 Fix Implementer Follow-up (2026-06-22, retry threshold)

### Scope handled
- Updated managed-labs reconcile completion mapping so failed runs remain retryable through attempt 4 and only move the lab to `blocked` on attempt 5.
- Tightened PostgreSQL coverage around `finish_reconcile_run()` so attempt-aware state transitions are pinned directly.
- Tightened endpoint coverage so a first reconcile failure still returns HTTP 500, closes the run as `failed`, and leaves the lab in `validating`, while the fifth failure blocks it.

### Root cause
- `managed_labs_pg._lab_status_from_reconcile_status()` mapped every `failed` reconcile run straight to `blocked`.
- `finish_reconcile_run()` persisted that unconditional mapping for every failed attempt, so the first inventory/planning failure exhausted the lab immediately instead of honoring the bounded retry plan.

### TDD evidence
#### RED
Command:
```bash
python3 -m pytest tests/test_managed_labs_pg.py tests/test_managed_labs_endpoints.py -q
```
Observed failures:
```text
FAILED tests/test_managed_labs_pg.py::test_finish_reconcile_run_updates_lab_current_state[failed-1-validating]
FAILED tests/test_managed_labs_endpoints.py::test_reconcile_failure_finishes_run_and_leaves_lab_retryable
E   AssertionError: assert 'blocked' == 'validating'
```
Meaning:
- A first failed reconcile attempt still pushed the lab into `blocked` in both the store-level and API-level flows.
- The new fifth-attempt coverage did not fail, which confirmed the bug was the missing retry threshold rather than run closure or HTTP behavior.

#### GREEN
Command:
```bash
python3 -m pytest tests/test_managed_labs_pg.py tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
20 passed, 7 warnings in 3.07s
```
Meaning:
- First failed reconcile attempts now leave labs in `validating` with `retry_count` set to the finished run attempt.
- Fifth failed attempts still transition labs to `blocked`, while failed runs are closed and no reconcile remains `running`.

### Regression coverage
Command:
```bash
python3 -m pytest tests/test_managed_labs_pg.py tests/test_managed_labs_reconciler.py tests/test_managed_labs_network.py tests/test_managed_labs_endpoints.py -q
```
Observed result:
```text
33 passed, 7 warnings in 3.92s
```

### Files changed
- `autopilot-proxmox/web/managed_labs_pg.py`
- `autopilot-proxmox/tests/test_managed_labs_pg.py`
- `autopilot-proxmox/tests/test_managed_labs_endpoints.py`

### Concerns
- None beyond the existing FastAPI/authlib deprecation warnings already present in the suite.
