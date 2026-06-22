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
