# Task 2 Report: Reservations, Naming, Findings, Fix Actions, And Snapshots

## What I Implemented
- Added reservation helpers in `autopilot-proxmox/web/managed_labs_pg.py`:
  - `reserve_value`
  - `reserve_default_names`
  - `find_overlapping_cidr_reservations`
- Added reconcile lifecycle helpers:
  - `start_reconcile_run`
  - `finish_reconcile_run`
- Added findings and fix-action helpers:
  - `record_finding`
  - `create_fix_action`
  - `get_fix_action`
  - `update_fix_action`
  - `list_open_findings`
  - `list_pending_fix_actions`
- Added provider snapshot helper:
  - `record_provider_snapshot`
- Updated `page_payload()` to surface Task 2 data while preserving Task 1 read-path behavior for labs, boundaries, boundary objects, and events.
- Added Task 2 tests covering:
  - reservation uniqueness and generated default names
  - CIDR overlap detection
  - reconcile runs, findings, fix actions, snapshots, and `page_payload()` exposure

## What I Tested And Results
Command run from `autopilot-proxmox`:

```bash
python3 -m pytest tests/test_managed_labs_pg.py -q
```

Result:
- PASS: `7 passed`
- Existing warnings remained from unrelated app startup/auth deprecations

## TDD Evidence
### RED
Command:

```bash
python3 -m pytest tests/test_managed_labs_pg.py -q
```

Expected failure observed:

```text
FAILED tests/test_managed_labs_pg.py::test_reservations_enforce_unique_values_and_generate_safe_names
E   AttributeError: module 'web.managed_labs_pg' has no attribute 'reserve_default_names'

FAILED tests/test_managed_labs_pg.py::test_cidr_overlap_detection_finds_existing_lab_reservation
E   AttributeError: module 'web.managed_labs_pg' has no attribute 'reserve_value'

FAILED tests/test_managed_labs_pg.py::test_reconcile_findings_fix_actions_and_snapshots_are_queryable
E   AttributeError: module 'web.managed_labs_pg' has no attribute 'start_reconcile_run'
```

Summary:
- `3 failed, 4 passed`
- Failures were the expected missing Task 2 entry points

### GREEN
Command:

```bash
python3 -m pytest tests/test_managed_labs_pg.py -q
```

Observed success:

```text
.......                                    [100%]
======================== 7 passed, 7 warnings in 2.16s =========================
```

## Files Changed
- `autopilot-proxmox/web/managed_labs_pg.py`
- `autopilot-proxmox/tests/test_managed_labs_pg.py`

## Self-Review Findings
- Kept scope to Task 2 helper functions and `page_payload()` updates only.
- Preserved Task 1 behavior, including current-state payload reads and the existing `record_event()` commit behavior.
- Adapted `page_payload()` instead of replacing it with the older brief snippet, so Task 1 ordering and selected-lab handling were not regressed.
- Added direct test coverage for all requested Task 2 public functions, including `finish_reconcile_run()` and `get_fix_action()`.

## Concerns, If Any
- The focused test target passes cleanly, but I did not run broader repository test suites because the task specifically requested `tests/test_managed_labs_pg.py`.
- `./skill.sh status` in the clone reported MCP auth failure (`401 Unauthorized`), so implementation relied on the local checkout and the supplied Task 2 brief rather than live MCP docs.

## Reviewer Follow-Up Fixes (2026-06-21)

### What I Changed
- Added regression tests for the reviewer findings in `autopilot-proxmox/tests/test_managed_labs_pg.py`:
  - `finish_reconcile_run()` now has coverage proving it updates the lab current-state row, carries forward `last_reconcile_run_id`, and sets `retry_count` from the run attempt.
  - `reserve_value()` now has coverage proving same-lab duplicates stay idempotent while cross-lab collisions fail loudly.
  - `update_fix_action()` now has coverage proving terminal statuses require a snapshot.
- Updated `autopilot-proxmox/web/managed_labs_pg.py` to:
  - reject cross-lab reservation collisions with `ValueError`
  - keep same-lab duplicate reservations idempotent by returning the existing row
  - project reconcile run completion back into `labs.status` and `labs.retry_count`
  - map reconcile status `failed` to lab status `blocked`
  - require an effective `snapshot_id` before accepting terminal fix-action statuses

### RED
Command run from `autopilot-proxmox`:

```bash
python3 -m pytest tests/test_managed_labs_pg.py -q
```

Observed failure:

```text
FAILED tests/test_managed_labs_pg.py::test_reserve_value_rejects_cross_lab_hostname_collision
E   Failed: DID NOT RAISE <class 'ValueError'>

FAILED tests/test_managed_labs_pg.py::test_finish_reconcile_run_updates_lab_current_state[ready-ready]
E   AssertionError: assert 'validating' == 'ready'

FAILED tests/test_managed_labs_pg.py::test_finish_reconcile_run_updates_lab_current_state[blocked-blocked]
E   AssertionError: assert 'validating' == 'blocked'

FAILED tests/test_managed_labs_pg.py::test_finish_reconcile_run_updates_lab_current_state[failed-blocked]
E   AssertionError: assert 'validating' == 'blocked'

FAILED tests/test_managed_labs_pg.py::test_update_fix_action_requires_snapshot_for_terminal_status
E   Failed: DID NOT RAISE <class 'ValueError'>
```

Summary:
- `5 failed, 7 passed`
- Failures matched the reviewer findings exactly

### GREEN
Command run from `autopilot-proxmox`:

```bash
python3 -m pytest tests/test_managed_labs_pg.py -q
```

Observed success:

```text
............                               [100%]
======================== 12 passed, 7 warnings in 2.54s ========================
```

### Notes
- MCP docs were attempted first, but the repo MCP docs backend returned `HTTP 502: MCP backend unavailable: [Errno 61] Connection refused`, so this follow-up used the local task brief/report and repository code directly.
