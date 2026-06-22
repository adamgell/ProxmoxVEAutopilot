# Task 1 Report: Managed Lab Repository Foundation

## What you implemented
- Added `autopilot-proxmox/web/managed_labs_pg.py` as the PostgreSQL repository foundation for managed labs.
- Added schema setup and test reset helpers for labs, boundaries, boundary objects, reservations, reconcile runs, findings, fix actions, approval requests, provider snapshots, events, and secret refs.
- Implemented the Task 1 repository interfaces only:
  - `init`
  - `reset_for_tests`
  - `create_lab`
  - `get_lab`
  - `list_labs`
  - `create_boundary`
  - `create_boundary_object`
  - `record_event`
  - `page_payload`
- Added `autopilot-proxmox/tests/test_managed_labs_pg.py` with the three Task 1 repository tests from the approved brief.

## What you tested and results
- Ran `python3 -m pytest tests/test_managed_labs_pg.py -q` from `autopilot-proxmox`.
- Result: `3 passed`.
- Observed unrelated existing deprecation warnings from app/auth startup imports during test setup.

## TDD Evidence
### RED command/output and expected failure
Command:
```bash
cd autopilot-proxmox
python3 -m pytest tests/test_managed_labs_pg.py -q
```
Output:
```text
============================= test session starts ==============================
platform darwin -- Python 3.14.5, pytest-9.0.3, pluggy-1.6.0
rootdir: /private/tmp/ProxmoxVEAutopilot-managed-lab-reconciler/autopilot-proxmox
configfile: pyproject.toml
plugins: anyio-4.13.0
collected 0 items / 1 error

==================================== ERRORS ====================================
________________ ERROR collecting tests/test_managed_labs_pg.py ________________
ImportError while importing test module '/private/tmp/ProxmoxVEAutopilot-managed-lab-reconciler/autopilot-proxmox/tests/test_managed_labs_pg.py'.
...
tests/test_managed_labs_pg.py:1: in <module>
    from web import managed_labs_pg
E   ImportError: cannot import name 'managed_labs_pg' from 'web' (/private/tmp/ProxmoxVEAutopilot-managed-lab-reconciler/autopilot-proxmox/web/__init__.py)
=========================== short test summary info ============================
ERROR tests/test_managed_labs_pg.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
=============================== 1 error in 0.08s ===============================
```
Expected failure confirmed: missing `managed_labs_pg` import/module.

### GREEN command/output
Command:
```bash
cd autopilot-proxmox
python3 -m pytest tests/test_managed_labs_pg.py -q
```
Output:
```text
============================= test session starts ==============================
platform darwin -- Python 3.14.5, pytest-9.0.3, pluggy-1.6.0
rootdir: /private/tmp/ProxmoxVEAutopilot-managed-lab-reconciler/autopilot-proxmox
configfile: pyproject.toml
plugins: anyio-4.13.0
collected 3 items

tests/test_managed_labs_pg.py ...                                        [100%]

======================== 3 passed, 7 warnings in 1.86s =========================
```

## Files changed
- `autopilot-proxmox/web/managed_labs_pg.py`
- `autopilot-proxmox/tests/test_managed_labs_pg.py`

## Self-review findings
- Stayed within Task 1 scope and did not add reservation, findings, fix action, snapshot, API, or UI behavior beyond the repository foundation and placeholder payload arrays required by the tests.
- Matched existing PostgreSQL repository patterns used by `lab_bubbles_pg.py` and `sdn_labs_pg.py`.
- Used the exact RED -> GREEN TDD flow requested by the brief.
- MCP docs helper was checked first via `./skill.sh status`, but local docs fallback was required because the MCP tool listing failed with `401 Unauthorized` in this isolated clone.

## Concerns, if any
- `./skill.sh status` could not complete MCP docs discovery in this clone because the tool listing returned `401 Unauthorized`; implementation proceeded from the local approved brief as instructed fallback.
- The focused Task 1 test file passes, but I did not broaden the run into the known cross-file backend isolation issue involving duplicate `ACME Lab` because the task instructions explicitly said not to expand scope there.


## Fix Implementer Follow-up: reviewer findings resolved

### Scope of this fix
- Updated `page_payload()` to read current-state rows for the selected lab from `lab_boundaries`, `lab_boundary_objects`, `lab_reservations`, `lab_reconcile_runs`, `lab_reconcile_findings`, and `lab_fix_actions` instead of hard-coded empty collections.
- Added a conservative provider-identity guard with normalized rows in `lab_boundary_object_provider_identities` and a uniqueness constraint on `(provider, kind, identity_key, identity_value)` so the same non-empty concrete provider identity cannot be attached to multiple labs.
- Brought `record_event()` in line with the other public mutators by committing its insert.

### Additional tests added
- `test_page_payload_returns_current_state_and_append_only_events` now proves boundary and boundary object rows flow through `page_payload()`.
- `test_boundary_object_provider_identity_must_be_unique_across_labs` proves duplicate provider identity insertion across labs is rejected.

### RED evidence
Command:
```bash
cd autopilot-proxmox
python3 -m pytest tests/test_managed_labs_pg.py -q
```
Output:
```text
============================= test session starts ==============================
platform darwin -- Python 3.14.5, pytest-9.0.3, pluggy-1.6.0
rootdir: /private/tmp/ProxmoxVEAutopilot-managed-lab-reconciler/autopilot-proxmox
configfile: pyproject.toml
plugins: anyio-4.13.0
collected 4 items

tests/test_managed_labs_pg.py ..FF                                       [100%]

=================================== FAILURES ===================================
________ test_page_payload_returns_current_state_and_append_only_events ________
tests/test_managed_labs_pg.py:127: in test_page_payload_returns_current_state_and_append_only_events
    assert [item["id"] for item in payload["boundaries"]] == [boundary["id"]]
E   AssertionError: assert [] == ['2b43fb4f-7b18-4a73-929f-bb14ecaed1ba']

______ test_boundary_object_provider_identity_must_be_unique_across_labs _______
tests/test_managed_labs_pg.py:185: in test_boundary_object_provider_identity_must_be_unique_across_labs
    with pytest.raises(UniqueViolation):
E   Failed: DID NOT RAISE <class 'psycopg.errors.UniqueViolation'>

=========================== short test summary info ============================
FAILED tests/test_managed_labs_pg.py::test_page_payload_returns_current_state_and_append_only_events
FAILED tests/test_managed_labs_pg.py::test_boundary_object_provider_identity_must_be_unique_across_labs
=================== 2 failed, 2 passed, 7 warnings in 1.96s ====================
```
Expected RED confirmed: the payload still returned boundary stubs and duplicate provider identity insertion was not rejected.

### GREEN evidence
Command:
```bash
cd autopilot-proxmox
python3 -m pytest tests/test_managed_labs_pg.py -q
```
Output:
```text
============================= test session starts ==============================
platform darwin -- Python 3.14.5, pytest-9.0.3, pluggy-1.6.0
rootdir: /private/tmp/ProxmoxVEAutopilot-managed-lab-reconciler/autopilot-proxmox
configfile: pyproject.toml
plugins: anyio-4.13.0
collected 4 items

tests/test_managed_labs_pg.py ....                                       [100%]

======================== 4 passed, 7 warnings in 1.94s =========================
```

### Files changed in this follow-up
- `autopilot-proxmox/web/managed_labs_pg.py`
- `autopilot-proxmox/tests/test_managed_labs_pg.py`
