# VM Fleet Bubbles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bubble-aware `/vms` operator console with a PostgreSQL bubble model, service/audit APIs, fleet/infrastructure/service page sections, and lifecycle hooks for CloudOSD/OSDeploy.

**Architecture:** Add a focused `web/lab_bubbles_pg.py` repository as the source of truth for bubble membership, services, readiness, gates, and audit events. Keep evidence collection in existing CloudOSD, OSDeploy, monitoring, and AutopilotAgent stores, then aggregate those signals into a VM page payload instead of mutating membership silently.

**Tech Stack:** FastAPI, Jinja templates, PostgreSQL via `psycopg`, existing pytest fixtures in `autopilot-proxmox/tests/conftest.py`, existing CloudOSD/OSDeploy endpoint modules.

---

## Scope And Boundaries

This plan implements the approved design in two executable slices, with explicit subagent ownership so multiple workers can help without touching the same files at the same time.

Phase 1 produces working software on its own: bubble tables, repository functions, API routes, audit/service support, and a `/vms` page reframe that removes the duplicate Autopilot device table.

Phase 2 wires bubble IDs into CloudOSD and OSDeploy launch flows, creates asset memberships from launches, and exposes lifecycle gates. It does not create Proxmox bridges, VLANs, firewall rules, NAT, router VMs, or DHCP servers outside the AD/DC agent path.

Before execution, each worker must run `git status --short --branch --untracked-files=all`. At plan review time, `main` was clean but `ahead 4, behind 2` relative to `origin/main`; do not pull, merge, reset, or rebase unless the operator explicitly approves that coordination step.

## File Structure

- Create `autopilot-proxmox/web/lab_bubbles_pg.py`: owns schema, CRUD, asset membership, services, readiness, gate calculation, audit rows, and VM page aggregation helpers.
- Create `autopilot-proxmox/tests/test_lab_bubbles_pg.py`: repository-level tests for schema, CRUD, assets, services, readiness, audit, and gates.
- Modify `autopilot-proxmox/web/app.py`: initialize the new repository, mount bubble APIs, pass a bubble fleet payload to `vms.html`, and stop passing matched Autopilot devices to the VM page.
- Modify `autopilot-proxmox/web/templates/vms.html`: add `VM Workstation Fleets`, `Critical Infrastructure`, and `Connected Services` sections; remove `Autopilot Devices (Intune)`.
- Modify `autopilot-proxmox/tests/test_cockpit_ui.py`: page rendering tests for the new `/vms` sections and removal of the duplicate Autopilot devices table.
- Modify `autopilot-proxmox/web/cloudosd_endpoints.py`: accept bubble launch fields and create membership after run creation.
- Modify `autopilot-proxmox/web/osdeploy_endpoints.py`: accept bubble launch fields and create membership after run or bundle creation.
- Modify `autopilot-proxmox/tests/test_osdeploy_endpoints.py`: OSDeploy launch and gate tests.
- Modify `autopilot-proxmox/tests/test_cloudosd_endpoints.py`: CloudOSD launch membership tests.
- Modify `autopilot-proxmox/web/agent_v1_endpoints.py`: accept DC readiness evidence from the domain-controller agent and feed bubble readiness.
- Modify `autopilot-proxmox/tests/test_agent_v1_endpoints.py`: DC readiness heartbeat tests.

## Subagent Coordination Contract

Use `superpowers:subagent-driven-development` when executing this plan with workers. Dispatch fresh workers with the smallest file ownership possible, and tell every worker they are not alone in the codebase: they must not revert edits made by others and must adapt to already-landed changes.

The coordinating agent owns task sequencing, review, final integration, and commits that cross ownership boundaries. A worker should finish with a concise handoff containing changed files, tests run, expected failures if any, and any assumptions that still need integration review.

### Dependency Order

1. Run **Foundation Worker** first for Task 1 and Task 2. No other worker should edit `autopilot-proxmox/web/lab_bubbles_pg.py` until this worker has landed or handed off a patch.
2. Run **API Worker** for Task 3 after Foundation Worker passes repository tests.
3. Run **VM Page Worker** for Task 4 and Task 5 after API Worker exposes the bubble payload.
4. Run **CloudOSD Worker** and **OSDeploy Worker** after Foundation Worker. These can run in parallel with each other because they own different endpoint/test files, but the coordinator must resolve any shared import or helper naming assumptions before committing both.
5. Run **Agent Readiness Worker** after Foundation Worker. This can run in parallel with VM Page, CloudOSD, or OSDeploy work if it avoids changing repository helpers already owned by Foundation Worker.
6. Run **Verification Worker** only after the coordinator has integrated all previous workers.

### Worker Ownership Map

| Worker | Tasks | Owns | Must Not Edit | Required Handoff |
| --- | --- | --- | --- | --- |
| Foundation Worker | 1, 2 | `autopilot-proxmox/web/lab_bubbles_pg.py`, `autopilot-proxmox/tests/test_lab_bubbles_pg.py` | Endpoint modules, templates, CloudOSD/OSDeploy tests, agent endpoint | Repository API names, schema columns, gate semantics, tests run |
| API Worker | 3 | `autopilot-proxmox/web/app.py`, API portions of `autopilot-proxmox/tests/test_cockpit_ui.py` | `lab_bubbles_pg.py` except imports/calls, `vms.html` | API routes added, response shapes, startup init behavior, tests run |
| VM Page Worker | 4, 5 | VM page aggregation in `autopilot-proxmox/web/app.py`, `autopilot-proxmox/web/templates/vms.html`, page-render tests | CloudOSD/OSDeploy/agent endpoint modules | Template sections, payload keys, removed Autopilot Devices area, screenshot/browser notes if run |
| CloudOSD Worker | 6 | `autopilot-proxmox/web/cloudosd_endpoints.py`, `autopilot-proxmox/tests/test_cloudosd_endpoints.py` | OSDeploy files, VM template, agent endpoint | Bubble fields accepted, membership creation point, tests run |
| OSDeploy Worker | 7 | `autopilot-proxmox/web/osdeploy_endpoints.py`, `autopilot-proxmox/tests/test_osdeploy_endpoints.py` | CloudOSD files, VM template, agent endpoint | Gate integration, workgroup early-launch behavior, membership creation point, tests run |
| Agent Readiness Worker | 8 | `autopilot-proxmox/web/agent_v1_endpoints.py`, `autopilot-proxmox/tests/test_agent_v1_endpoints.py` | CloudOSD/OSDeploy files, VM template | Heartbeat payload shape, DC-only readiness rule, DHCP evidence fields, tests run |
| Verification Worker | 9 | No source ownership unless fixing coordinator-approved integration defects | Any unrelated file | Full command output summary, failing test names, targeted fix recommendation |

### Dispatch Prompts

Use these prompts as the starting point for worker dispatch. Add the current commit hash and any already-landed worker handoffs before sending.

**Foundation Worker Prompt**

```text
You are implementing Tasks 1 and 2 from docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Edit only autopilot-proxmox/web/lab_bubbles_pg.py and autopilot-proxmox/tests/test_lab_bubbles_pg.py.
- Do not modify endpoint modules, templates, CloudOSD/OSDeploy tests, or agent endpoint files.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Create the bubble repository schema, CRUD, assets, services, audit, readiness, gate functions, patch/move helpers, and repository tests exactly as planned unless the current code requires a tighter local pattern.
- Run the task-specific pytest commands from Tasks 1 and 2.
- End with changed files, tests run, and any API names later workers must use.
```

**API Worker Prompt**

```text
You are implementing Task 3 from docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Edit only autopilot-proxmox/web/app.py and API-related tests in autopilot-proxmox/tests/test_cockpit_ui.py.
- Use the already-landed lab_bubbles_pg repository API. Do not redesign its schema or helper names.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Initialize bubble storage where existing app startup/init code expects repository setup.
- Add the /api/bubbles routes, asset patch/move routes, service patch route, readiness route, and audit route.
- Run the Task 3 pytest command.
- End with route list, response shape notes, changed files, and tests run.
```

**VM Page Worker Prompt**

```text
You are implementing Tasks 4 and 5 from docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Edit VM page aggregation code in autopilot-proxmox/web/app.py, autopilot-proxmox/web/templates/vms.html, and page-render tests in autopilot-proxmox/tests/test_cockpit_ui.py.
- Do not edit CloudOSD, OSDeploy, or agent endpoint modules.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Remove the Autopilot Devices area from /vms.
- Add VM Workstation Fleets, Critical Infrastructure, Connected Services, and Unassigned Assets sections driven by the planned payload keys.
- Preserve the existing VM page timezone regression behavior.
- Run the Task 4, Task 5, and timezone regression pytest commands.
- End with payload keys consumed, template sections changed, changed files, and tests run.
```

**CloudOSD Worker Prompt**

```text
You are implementing Task 6 from docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Edit only autopilot-proxmox/web/cloudosd_endpoints.py and autopilot-proxmox/tests/test_cloudosd_endpoints.py.
- Do not edit OSDeploy files, VM templates, or agent endpoint files.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Add bubble_id and asset_role launch fields.
- Create lab_bubble_assets membership rows after CloudOSD run creation.
- Keep existing CloudOSD catalog and artifact behavior unchanged.
- Run the Task 6 pytest command.
- End with changed files, membership creation location, and tests run.
```

**OSDeploy Worker Prompt**

```text
You are implementing Task 7 from docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Edit only autopilot-proxmox/web/osdeploy_endpoints.py and autopilot-proxmox/tests/test_osdeploy_endpoints.py.
- Do not edit CloudOSD files, VM templates, or agent endpoint files.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Add bubble_id and asset_role launch fields.
- Create lab_bubble_assets membership rows after OSDeploy run or bundle creation.
- Enforce launch gates in /api/osdeploy/v1/preflight using early workgroup allowance only for single-bubble, no-domain/no-ConfigMgr cases.
- Run the Task 7 pytest commands.
- End with changed files, gate behavior summary, and tests run.
```

**Agent Readiness Worker Prompt**

```text
You are implementing Task 8 from docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Edit only autopilot-proxmox/web/agent_v1_endpoints.py plus the agent readiness test in autopilot-proxmox/tests/test_agent_v1_endpoints.py.
- Only edit autopilot-proxmox/web/lab_bubbles_pg.py if asset_for_agent is still missing after Foundation Worker lands; otherwise use the existing helper.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Accept bubble_id and dc_readiness on heartbeat.
- Update bubble AD DS, DNS, DHCP readiness only when the authenticated agent is an active/provisioning domain_controller asset in that bubble.
- Persist DHCP scope, DHCP pool start, and DHCP pool end through update_readiness_from_dc_evidence.
- Run the Task 8 pytest commands.
- End with changed files, heartbeat payload fields, and tests run.
```

**Verification Worker Prompt**

```text
You are verifying the integrated branch for docs/superpowers/plans/2026-05-19-vm-fleet-bubbles.md in /Users/Adam.Gell/repo/ProxmoxVEAutopilot.

Ownership:
- Do not make source edits unless the coordinator explicitly asks for a targeted fix after a failing command.
- You are not alone in the codebase; do not revert edits made by others.

Deliver:
- Run all commands in Task 9.
- Report exact failing test names, first relevant assertion/error, and likely owning worker if anything fails.
- If everything passes, report command list and a final changed-file summary.
```

### Integration Checkpoints

- After Foundation Worker: run `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py -q` before dispatching API, launch, or agent workers.
- After API Worker: run `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_bubble_api_create_assets_services_and_audit -q` and confirm `/api/bubbles/{bubble_id}/assets/{asset_id}/move` and `/api/bubbles/{bubble_id}/services/{service_id}` exist before UI or lifecycle agents depend on them.
- After VM Page Worker: run `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_vms_page_uses_fleet_bubble_sections autopilot-proxmox/tests/test_cockpit_ui.py::test_vms_agent_heartbeat_uses_local_timezone_markup -q`.
- After CloudOSD and OSDeploy workers: run `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cloudosd_endpoints.py::test_cloudosd_run_records_bubble_membership autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_run_records_bubble_membership autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_preflight_blocks_domain_join_before_bubble_readiness -q` so shared repository assumptions fail together.
- After Agent Readiness Worker: run `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py::test_agent_heartbeat_updates_bubble_dc_dns_dhcp_readiness autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_assets_services_audit_and_readiness -q` to verify heartbeat wiring and DHCP evidence persistence together.
- Before final commit: run Task 9 exactly, then `git status --short --branch --untracked-files=all`.

## Task 1: Bubble Repository Schema And CRUD

**Files:**
- Create: `autopilot-proxmox/web/lab_bubbles_pg.py`
- Test: `autopilot-proxmox/tests/test_lab_bubbles_pg.py`

- [ ] **Step 1: Write failing schema and CRUD tests**

Add this test file:

```python
from web import lab_bubbles_pg


def test_bubble_schema_create_get_list_patch(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)

    bubble = lab_bubbles_pg.create_bubble(
        pg_conn,
        name="ACME Lab",
        domain_name="lab.acme.test",
        netbios_name="ACME",
        cidr="10.42.12.0/24",
        gateway_ip="10.42.12.1",
        planned_bridge="vmbr-lab12",
        dhcp_scope="10.42.12.0",
        dhcp_pool_start="10.42.12.100",
        dhcp_pool_end="10.42.12.199",
    )

    assert bubble["slug"] == "acme-lab"
    assert bubble["lifecycle_state"] == "planned"
    assert bubble["domain_name"] == "lab.acme.test"
    assert bubble["dhcp_owner_asset_id"] is None

    listed = lab_bubbles_pg.list_bubbles(pg_conn)
    assert [row["id"] for row in listed] == [bubble["id"]]

    patched = lab_bubbles_pg.update_bubble(
        pg_conn,
        bubble["id"],
        lifecycle_state="active",
        dns_ready=True,
    )
    assert patched["lifecycle_state"] == "active"
    assert patched["dns_ready"] is True
    assert lab_bubbles_pg.get_bubble(pg_conn, bubble["id"])["dns_ready"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_bubble_schema_create_get_list_patch -q
```

Expected: FAIL with `ImportError` or `AttributeError` for `lab_bubbles_pg`.

- [ ] **Step 3: Implement minimal repository schema and CRUD**

Create `autopilot-proxmox/web/lab_bubbles_pg.py` with:

```python
"""PostgreSQL store for lab/tenant bubbles and their assets."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from web import db_pg


SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_bubbles (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    slug text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    lifecycle_state text NOT NULL DEFAULT 'planned',
    domain_name text NOT NULL DEFAULT '',
    netbios_name text NOT NULL DEFAULT '',
    cidr text NOT NULL DEFAULT '',
    gateway_ip text NOT NULL DEFAULT '',
    planned_bridge text NOT NULL DEFAULT '',
    planned_vlan integer NULL,
    isolation_status text NOT NULL DEFAULT 'planned',
    dhcp_scope text NOT NULL DEFAULT '',
    dhcp_pool_start text NOT NULL DEFAULT '',
    dhcp_pool_end text NOT NULL DEFAULT '',
    dhcp_owner_asset_id uuid NULL,
    dc_ready boolean NOT NULL DEFAULT false,
    dns_ready boolean NOT NULL DEFAULT false,
    dhcp_ready boolean NOT NULL DEFAULT false,
    workload_ready boolean NOT NULL DEFAULT false,
    allow_early_workgroup_launch boolean NOT NULL DEFAULT true,
    require_domain_ready boolean NOT NULL DEFAULT true,
    require_multi_domain_ready boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
"""

DROP_SCHEMA_FOR_TESTS = """
DROP TABLE IF EXISTS lab_bubble_audit_events CASCADE;
DROP TABLE IF EXISTS lab_bubble_services CASCADE;
DROP TABLE IF EXISTS lab_bubble_assets CASCADE;
DROP TABLE IF EXISTS lab_bubbles CASCADE;
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or f"bubble-{uuid.uuid4().hex[:8]}"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("id", "dhcp_owner_asset_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def init(conn: Connection | None = None) -> None:
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


def create_bubble(
    conn: Connection,
    *,
    name: str,
    description: str = "",
    domain_name: str = "",
    netbios_name: str = "",
    cidr: str = "",
    gateway_ip: str = "",
    planned_bridge: str = "",
    planned_vlan: int | None = None,
    dhcp_scope: str = "",
    dhcp_pool_start: str = "",
    dhcp_pool_end: str = "",
) -> dict:
    now = _now()
    bubble_id = _new_id()
    row = conn.execute(
        """
        INSERT INTO lab_bubbles (
            id, name, slug, description, domain_name, netbios_name, cidr,
            gateway_ip, planned_bridge, planned_vlan, dhcp_scope,
            dhcp_pool_start, dhcp_pool_end, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            bubble_id,
            name.strip(),
            _slug(name),
            description.strip(),
            domain_name.strip().lower(),
            netbios_name.strip().upper(),
            cidr.strip(),
            gateway_ip.strip(),
            planned_bridge.strip(),
            planned_vlan,
            dhcp_scope.strip(),
            dhcp_pool_start.strip(),
            dhcp_pool_end.strip(),
            now,
            now,
        ),
    ).fetchone()
    conn.commit()
    return _row(row)


def get_bubble(conn: Connection, bubble_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM lab_bubbles WHERE id = %s", (bubble_id,)).fetchone())


def list_bubbles(conn: Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM lab_bubbles ORDER BY name").fetchall()
    return [_row(row) for row in rows]


def update_bubble(conn: Connection, bubble_id: str, **fields: Any) -> dict:
    allowed = {
        "name",
        "description",
        "lifecycle_state",
        "domain_name",
        "netbios_name",
        "cidr",
        "gateway_ip",
        "planned_bridge",
        "planned_vlan",
        "isolation_status",
        "dhcp_scope",
        "dhcp_pool_start",
        "dhcp_pool_end",
        "dhcp_owner_asset_id",
        "dc_ready",
        "dns_ready",
        "dhcp_ready",
        "workload_ready",
        "allow_early_workgroup_launch",
        "require_domain_ready",
        "require_multi_domain_ready",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        current = get_bubble(conn, bubble_id)
        if current is None:
            raise ValueError("bubble not found")
        return current
    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{key} = %s" for key in updates)
    values = list(updates.values()) + [bubble_id]
    row = conn.execute(
        f"UPDATE lab_bubbles SET {set_sql} WHERE id = %s RETURNING *",
        values,
    ).fetchone()
    if row is None:
        raise ValueError("bubble not found")
    conn.commit()
    return _row(row)
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_bubble_schema_create_get_list_patch -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/lab_bubbles_pg.py autopilot-proxmox/tests/test_lab_bubbles_pg.py
git commit -m "Add lab bubble repository"
```

## Task 2: Asset Membership, Services, Audit, And Gates

**Files:**
- Modify: `autopilot-proxmox/web/lab_bubbles_pg.py`
- Test: `autopilot-proxmox/tests/test_lab_bubbles_pg.py`

- [ ] **Step 1: Write failing membership and service tests**

Append:

```python
def test_assets_services_audit_and_readiness(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")

    dc_asset = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
        agent_id="dc01-agent",
        membership_state="active",
    )
    service = lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        scope="bubble_local",
        provider_asset_id=dc_asset["id"],
        readiness_state="ready",
        evidence_summary={"scope": "10.42.12.0", "leases": 3},
    )
    updated_asset = lab_bubbles_pg.update_asset(
        pg_conn,
        dc_asset["id"],
        evidence_state="confirmed",
        notes="DC agent evidence confirmed",
    )
    updated_service = lab_bubbles_pg.update_service(
        pg_conn,
        service["id"],
        readiness_state="degraded",
        evidence_summary={"scope": "10.42.12.0", "leases": 4, "warning": "short lease window"},
    )
    patched = lab_bubbles_pg.update_readiness_from_dc_evidence(
        pg_conn,
        bubble["id"],
        dc_asset_id=dc_asset["id"],
        evidence={
            "ad_ds_ready": True,
            "dns_ready": True,
            "dhcp_ready": True,
            "dhcp_scope": "10.42.12.0",
            "dhcp_pool_start": "10.42.12.100",
            "dhcp_pool_end": "10.42.12.199",
        },
    )

    assert service["service_kind"] == "dhcp"
    assert updated_asset["evidence_state"] == "confirmed"
    assert updated_service["readiness_state"] == "degraded"
    assert updated_service["evidence_summary"]["leases"] == 4
    assert patched["dc_ready"] is True
    assert patched["dns_ready"] is True
    assert patched["dhcp_ready"] is True
    assert patched["workload_ready"] is True
    assert patched["dhcp_pool_start"] == "10.42.12.100"
    assert patched["dhcp_pool_end"] == "10.42.12.199"

    moved = lab_bubbles_pg.move_asset(
        pg_conn,
        dc_asset["id"],
        bubble["id"],
        reason="repair membership",
        actor="operator",
    )
    assert moved["id"] == dc_asset["id"]
    events = lab_bubbles_pg.list_audit_events(pg_conn, bubble["id"])
    assert events[-1]["action"] == "asset_moved"


def test_gate_states_allow_workgroup_and_block_domain_before_readiness(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")

    workgroup = lab_bubbles_pg.evaluate_launch_gate(
        pg_conn,
        bubble["id"],
        requires_domain_join=False,
        requires_configmgr=False,
        is_multi_bubble_context=False,
        is_multi_domain_context=False,
    )
    assert workgroup["state"] == "warning"
    assert workgroup["allowed"] is True

    domain = lab_bubbles_pg.evaluate_launch_gate(
        pg_conn,
        bubble["id"],
        requires_domain_join=True,
        requires_configmgr=False,
        is_multi_bubble_context=False,
        is_multi_domain_context=False,
    )
    assert domain["state"] == "blocked"
    assert domain["allowed"] is False
    assert "DC agent has not reported DHCP scope readiness" in domain["reasons"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py -q
```

Expected: FAIL for missing asset/service/audit/gate functions.

- [ ] **Step 3: Add tables and repository functions**

Extend `SCHEMA` in `lab_bubbles_pg.py` after `lab_bubbles`:

```python
CREATE TABLE IF NOT EXISTS lab_bubble_assets (
    id uuid PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    asset_type text NOT NULL,
    asset_role text NOT NULL,
    vmid integer NULL,
    vm_uuid text NULL,
    run_id uuid NULL,
    agent_id text NULL,
    service_id uuid NULL,
    membership_state text NOT NULL DEFAULT 'active',
    evidence_state text NOT NULL DEFAULT 'unknown',
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_bubble_role
    ON lab_bubble_assets(bubble_id, asset_role);
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_vmid
    ON lab_bubble_assets(vmid) WHERE vmid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_run
    ON lab_bubble_assets(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lab_bubble_assets_agent
    ON lab_bubble_assets(agent_id) WHERE agent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS lab_bubble_services (
    id uuid PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    service_kind text NOT NULL,
    service_name text NOT NULL,
    scope text NOT NULL DEFAULT 'bubble_local',
    provider_asset_id uuid NULL REFERENCES lab_bubble_assets(id) ON DELETE SET NULL,
    consumer_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    readiness_state text NOT NULL DEFAULT 'unknown',
    last_evidence_at timestamptz NULL,
    evidence_summary_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_bubble_services_bubble_kind
    ON lab_bubble_services(bubble_id, service_kind);

CREATE TABLE IF NOT EXISTS lab_bubble_audit_events (
    id bigserial PRIMARY KEY,
    bubble_id uuid NOT NULL REFERENCES lab_bubbles(id) ON DELETE CASCADE,
    asset_id uuid NULL REFERENCES lab_bubble_assets(id) ON DELETE SET NULL,
    actor text NOT NULL,
    action text NOT NULL,
    reason text NOT NULL DEFAULT '',
    old_values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    new_values_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);
```

Add row helpers:

```python
def _asset_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("id", "bubble_id", "run_id", "service_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _service_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("id", "bubble_id", "provider_asset_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    out["consumer_refs"] = out.pop("consumer_refs_json") or []
    out["evidence_summary"] = out.pop("evidence_summary_json") or {}
    for key in ("last_evidence_at", "created_at", "updated_at"):
        out[key] = _iso(out.get(key))
    return out


def _audit_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["id"] = str(out["id"])
    for key in ("bubble_id", "asset_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    out["old_values"] = out.pop("old_values_json") or {}
    out["new_values"] = out.pop("new_values_json") or {}
    out["created_at"] = _iso(out.get("created_at"))
    return out
```

Add functions:

```python
def record_audit_event(
    conn: Connection,
    *,
    bubble_id: str,
    action: str,
    actor: str = "system",
    asset_id: str | None = None,
    reason: str = "",
    old_values: dict | None = None,
    new_values: dict | None = None,
) -> dict:
    row = conn.execute(
        """
        INSERT INTO lab_bubble_audit_events (
            bubble_id, asset_id, actor, action, reason,
            old_values_json, new_values_json, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            bubble_id,
            asset_id,
            actor,
            action,
            reason,
            Jsonb(old_values or {}),
            Jsonb(new_values or {}),
            _now(),
        ),
    ).fetchone()
    return _audit_row(row)


def add_asset(
    conn: Connection,
    bubble_id: str,
    *,
    asset_type: str,
    asset_role: str,
    vmid: int | None = None,
    vm_uuid: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    service_id: str | None = None,
    membership_state: str = "active",
    evidence_state: str = "unknown",
    notes: str = "",
    actor: str = "system",
) -> dict:
    now = _now()
    asset_id = _new_id()
    row = conn.execute(
        """
        INSERT INTO lab_bubble_assets (
            id, bubble_id, asset_type, asset_role, vmid, vm_uuid, run_id,
            agent_id, service_id, membership_state, evidence_state, notes,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            asset_id,
            bubble_id,
            asset_type,
            asset_role,
            vmid,
            vm_uuid,
            run_id or None,
            agent_id,
            service_id or None,
            membership_state,
            evidence_state,
            notes,
            now,
            now,
        ),
    ).fetchone()
    asset = _asset_row(row)
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        asset_id=asset["id"],
        action="asset_added",
        actor=actor,
        new_values=asset,
    )
    conn.commit()
    return asset


def list_assets(conn: Connection, bubble_id: str | None = None) -> list[dict]:
    if bubble_id:
        rows = conn.execute(
            "SELECT * FROM lab_bubble_assets WHERE bubble_id = %s ORDER BY asset_role, vmid NULLS LAST, agent_id",
            (bubble_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM lab_bubble_assets ORDER BY asset_role, vmid NULLS LAST, agent_id").fetchall()
    return [_asset_row(row) for row in rows]


def update_asset(conn: Connection, asset_id: str, **fields: Any) -> dict:
    allowed = {"asset_role", "vmid", "vm_uuid", "run_id", "agent_id", "service_id", "membership_state", "evidence_state", "notes"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        row = conn.execute("SELECT * FROM lab_bubble_assets WHERE id = %s", (asset_id,)).fetchone()
        if row is None:
            raise ValueError("asset not found")
        return _asset_row(row)
    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{key} = %s" for key in updates)
    row = conn.execute(
        f"UPDATE lab_bubble_assets SET {set_sql} WHERE id = %s RETURNING *",
        list(updates.values()) + [asset_id],
    ).fetchone()
    if row is None:
        raise ValueError("asset not found")
    asset = _asset_row(row)
    record_audit_event(
        conn,
        bubble_id=asset["bubble_id"],
        asset_id=asset_id,
        action="asset_updated",
        new_values=asset,
    )
    conn.commit()
    return asset


def move_asset(
    conn: Connection,
    asset_id: str,
    bubble_id: str,
    *,
    reason: str,
    actor: str = "operator",
) -> dict:
    current = conn.execute("SELECT * FROM lab_bubble_assets WHERE id = %s", (asset_id,)).fetchone()
    if current is None:
        raise ValueError("asset not found")
    old = _asset_row(current)
    row = conn.execute(
        """
        UPDATE lab_bubble_assets
        SET bubble_id = %s, updated_at = %s
        WHERE id = %s
        RETURNING *
        """,
        (bubble_id, _now(), asset_id),
    ).fetchone()
    moved = _asset_row(row)
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        asset_id=asset_id,
        action="asset_moved",
        actor=actor,
        reason=reason,
        old_values=old,
        new_values=moved,
    )
    conn.commit()
    return moved


def add_service(
    conn: Connection,
    bubble_id: str,
    *,
    service_kind: str,
    service_name: str,
    scope: str = "bubble_local",
    provider_asset_id: str | None = None,
    consumer_refs: list | None = None,
    readiness_state: str = "unknown",
    evidence_summary: dict | None = None,
) -> dict:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO lab_bubble_services (
            id, bubble_id, service_kind, service_name, scope,
            provider_asset_id, consumer_refs_json, readiness_state,
            last_evidence_at, evidence_summary_json, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            _new_id(),
            bubble_id,
            service_kind,
            service_name,
            scope,
            provider_asset_id,
            Jsonb(consumer_refs or []),
            readiness_state,
            _now() if readiness_state != "unknown" else None,
            Jsonb(evidence_summary or {}),
            now,
            now,
        ),
    ).fetchone()
    conn.commit()
    return _service_row(row)


def list_services(conn: Connection, bubble_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lab_bubble_services WHERE bubble_id = %s ORDER BY service_kind, service_name",
        (bubble_id,),
    ).fetchall()
    return [_service_row(row) for row in rows]


def update_service(conn: Connection, service_id: str, **fields: Any) -> dict:
    allowed = {"service_kind", "service_name", "scope", "provider_asset_id", "consumer_refs", "readiness_state", "evidence_summary"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "consumer_refs" in updates:
        updates["consumer_refs_json"] = Jsonb(updates.pop("consumer_refs") or [])
    if "evidence_summary" in updates:
        updates["evidence_summary_json"] = Jsonb(updates.pop("evidence_summary") or {})
    if "readiness_state" in updates:
        updates["last_evidence_at"] = _now()
    if not updates:
        row = conn.execute("SELECT * FROM lab_bubble_services WHERE id = %s", (service_id,)).fetchone()
        if row is None:
            raise ValueError("service not found")
        return _service_row(row)
    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{key} = %s" for key in updates)
    row = conn.execute(
        f"UPDATE lab_bubble_services SET {set_sql} WHERE id = %s RETURNING *",
        list(updates.values()) + [service_id],
    ).fetchone()
    if row is None:
        raise ValueError("service not found")
    conn.commit()
    return _service_row(row)


def list_audit_events(conn: Connection, bubble_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lab_bubble_audit_events WHERE bubble_id = %s ORDER BY id",
        (bubble_id,),
    ).fetchall()
    return [_audit_row(row) for row in rows]


def update_readiness_from_dc_evidence(
    conn: Connection,
    bubble_id: str,
    *,
    dc_asset_id: str,
    evidence: dict,
) -> dict:
    dc_ready = bool(evidence.get("ad_ds_ready"))
    dns_ready = bool(evidence.get("dns_ready"))
    dhcp_ready = bool(evidence.get("dhcp_ready"))
    workload_ready = dc_ready and dns_ready and dhcp_ready
    patched = update_bubble(
        conn,
        bubble_id,
        dhcp_owner_asset_id=dc_asset_id,
        dc_ready=dc_ready,
        dns_ready=dns_ready,
        dhcp_ready=dhcp_ready,
        workload_ready=workload_ready,
        dhcp_scope=str(evidence.get("dhcp_scope") or ""),
        dhcp_pool_start=str(evidence.get("dhcp_pool_start") or ""),
        dhcp_pool_end=str(evidence.get("dhcp_pool_end") or ""),
    )
    record_audit_event(
        conn,
        bubble_id=bubble_id,
        asset_id=dc_asset_id,
        action="readiness_evidence_updated",
        actor="agent",
        new_values=evidence,
    )
    conn.commit()
    return patched


def evaluate_launch_gate(
    conn: Connection,
    bubble_id: str,
    *,
    requires_domain_join: bool,
    requires_configmgr: bool,
    is_multi_bubble_context: bool,
    is_multi_domain_context: bool,
) -> dict:
    bubble = get_bubble(conn, bubble_id)
    if bubble is None:
        return {"state": "blocked", "allowed": False, "reasons": ["bubble not found"]}
    reasons = []
    ready = bool(bubble["dc_ready"] and bubble["dns_ready"] and bubble["dhcp_ready"])
    if not bubble["dc_ready"]:
        reasons.append("DC agent has not reported AD DS readiness")
    if not bubble["dns_ready"]:
        reasons.append("DC agent has not reported DNS readiness")
    if not bubble["dhcp_ready"]:
        reasons.append("DC agent has not reported DHCP scope readiness")
    if requires_configmgr:
        configmgr_ready = any(
            svc["service_kind"] in {"configmgr", "mecm"} and svc["readiness_state"] == "ready"
            for svc in list_services(conn, bubble_id)
        )
        if not configmgr_ready:
            reasons.append("ConfigMgr service readiness is missing")
    hard_requires_ready = (
        requires_domain_join
        or requires_configmgr
        or is_multi_bubble_context
        or is_multi_domain_context
    )
    if hard_requires_ready and reasons:
        return {"state": "blocked", "allowed": False, "reasons": reasons}
    if not ready and reasons:
        return {"state": "warning", "allowed": True, "reasons": reasons}
    return {"state": "allowed", "allowed": True, "reasons": []}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autopilot-proxmox/web/lab_bubbles_pg.py autopilot-proxmox/tests/test_lab_bubbles_pg.py
git commit -m "Add bubble assets services and gates"
```

## Task 3: Bubble API Routes And Startup Initialization

**Files:**
- Modify: `autopilot-proxmox/web/app.py`
- Test: `autopilot-proxmox/tests/test_cockpit_ui.py`

- [ ] **Step 1: Write failing API tests**

Append to `autopilot-proxmox/tests/test_cockpit_ui.py`:

```python
def test_bubble_api_create_assets_services_and_audit(web_client: TestClient, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)

    created = web_client.post(
        "/api/bubbles",
        json={
            "name": "ACME Lab",
            "domain_name": "lab.acme.test",
            "netbios_name": "ACME",
            "cidr": "10.42.12.0/24",
            "gateway_ip": "10.42.12.1",
        },
    )
    assert created.status_code == 201
    bubble_id = created.json()["id"]

    asset = web_client.post(
        f"/api/bubbles/{bubble_id}/assets",
        json={"asset_type": "vm", "asset_role": "domain_controller", "vmid": 130, "agent_id": "dc01"},
    )
    assert asset.status_code == 201
    asset_id = asset.json()["id"]

    service = web_client.post(
        f"/api/bubbles/{bubble_id}/services",
        json={
            "service_kind": "dhcp",
            "service_name": "ACME DHCP",
            "scope": "bubble_local",
            "provider_asset_id": asset_id,
        },
    )
    assert service.status_code == 201
    service_id = service.json()["id"]

    patched_service = web_client.patch(
        f"/api/bubbles/{bubble_id}/services/{service_id}",
        json={"readiness_state": "ready", "evidence_summary": {"leases": 3}},
    )
    assert patched_service.status_code == 200
    assert patched_service.json()["readiness_state"] == "ready"

    patched_asset = web_client.patch(
        f"/api/bubbles/{bubble_id}/assets/{asset_id}",
        json={"evidence_state": "confirmed", "notes": "agent matched"},
    )
    assert patched_asset.status_code == 200
    assert patched_asset.json()["evidence_state"] == "confirmed"

    moved = web_client.post(
        f"/api/bubbles/{bubble_id}/assets/{asset_id}/move",
        json={"target_bubble_id": bubble_id, "reason": "self move audit check"},
    )
    assert moved.status_code == 200

    readiness = web_client.get(f"/api/bubbles/{bubble_id}/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["bubble"]["id"] == bubble_id

    audit = web_client.get(f"/api/bubbles/{bubble_id}/audit-events")
    assert audit.status_code == 200
    assert any(row["action"] == "asset_added" for row in audit.json()["events"])
    assert any(row["action"] == "asset_moved" for row in audit.json()["events"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_bubble_api_create_assets_services_and_audit -q
```

Expected: FAIL with 404 for `/api/bubbles`.

- [ ] **Step 3: Initialize repository on startup**

In `_init_app_database()` in `autopilot-proxmox/web/app.py`, add `lab_bubbles_pg` to the import and call `lab_bubbles_pg.init(conn)` after `deployment_health_pg.init(conn)`:

```python
from web import (
    agent_telemetry_pg,
    cloudosd_cache,
    cloudosd_pg,
    db_pg,
    deployment_health_pg,
    device_history_pg,
    devices_pg,
    lab_bubbles_pg,
    osdeploy_cache,
    osdeploy_pg,
    ts_engine_pg,
)
```

```python
lab_bubbles_pg.init(conn)
```

- [ ] **Step 4: Add request models and API routes**

Near other API model classes in `app.py`, add:

```python
class _BubbleCreate(BaseModel):
    name: str
    description: str = ""
    domain_name: str = ""
    netbios_name: str = ""
    cidr: str = ""
    gateway_ip: str = ""
    planned_bridge: str = ""
    planned_vlan: Optional[int] = None
    dhcp_scope: str = ""
    dhcp_pool_start: str = ""
    dhcp_pool_end: str = ""


class _BubblePatch(BaseModel):
    description: Optional[str] = None
    lifecycle_state: Optional[str] = None
    domain_name: Optional[str] = None
    netbios_name: Optional[str] = None
    cidr: Optional[str] = None
    gateway_ip: Optional[str] = None
    planned_bridge: Optional[str] = None
    planned_vlan: Optional[int] = None
    isolation_status: Optional[str] = None
    dc_ready: Optional[bool] = None
    dns_ready: Optional[bool] = None
    dhcp_ready: Optional[bool] = None
    workload_ready: Optional[bool] = None


class _BubbleAssetCreate(BaseModel):
    asset_type: str
    asset_role: str
    vmid: Optional[int] = None
    vm_uuid: Optional[str] = None
    run_id: Optional[str] = None
    agent_id: Optional[str] = None
    service_id: Optional[str] = None
    membership_state: str = "active"
    evidence_state: str = "unknown"
    notes: str = ""


class _BubbleServiceCreate(BaseModel):
    service_kind: str
    service_name: str
    scope: str = "bubble_local"
    provider_asset_id: Optional[str] = None
    consumer_refs: list = Field(default_factory=list)
    readiness_state: str = "unknown"
    evidence_summary: dict = Field(default_factory=dict)


class _BubbleAssetPatch(BaseModel):
    asset_role: Optional[str] = None
    vmid: Optional[int] = None
    vm_uuid: Optional[str] = None
    run_id: Optional[str] = None
    agent_id: Optional[str] = None
    service_id: Optional[str] = None
    membership_state: Optional[str] = None
    evidence_state: Optional[str] = None
    notes: Optional[str] = None


class _BubbleAssetMove(BaseModel):
    target_bubble_id: str
    reason: str = ""


class _BubbleServicePatch(BaseModel):
    service_kind: Optional[str] = None
    service_name: Optional[str] = None
    scope: Optional[str] = None
    provider_asset_id: Optional[str] = None
    consumer_refs: Optional[list] = None
    readiness_state: Optional[str] = None
    evidence_summary: Optional[dict] = None
```

Add routes:

```python
@app.get("/api/bubbles")
def api_bubbles_list():
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return {"bubbles": lab_bubbles_pg.list_bubbles(conn)}


@app.post("/api/bubbles", status_code=201)
def api_bubbles_create(body: _BubbleCreate):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return lab_bubbles_pg.create_bubble(conn, **body.model_dump())


@app.get("/api/bubbles/{bubble_id}")
def api_bubbles_get(bubble_id: str):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        bubble = lab_bubbles_pg.get_bubble(conn, bubble_id)
        if bubble is None:
            raise HTTPException(status_code=404, detail="bubble not found")
        return bubble


@app.patch("/api/bubbles/{bubble_id}")
def api_bubbles_patch(bubble_id: str, body: _BubblePatch):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            return lab_bubbles_pg.update_bubble(
                conn,
                bubble_id,
                **{k: v for k, v in body.model_dump().items() if v is not None},
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/bubbles/{bubble_id}/readiness")
def api_bubbles_readiness(bubble_id: str):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        bubble = lab_bubbles_pg.get_bubble(conn, bubble_id)
        if bubble is None:
            raise HTTPException(status_code=404, detail="bubble not found")
        return {
            "bubble": bubble,
            "assets": lab_bubbles_pg.list_assets(conn, bubble_id),
            "services": lab_bubbles_pg.list_services(conn, bubble_id),
        }


@app.get("/api/bubbles/{bubble_id}/assets")
def api_bubbles_assets_list(bubble_id: str):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return {"assets": lab_bubbles_pg.list_assets(conn, bubble_id)}


@app.post("/api/bubbles/{bubble_id}/assets", status_code=201)
def api_bubbles_assets_create(bubble_id: str, body: _BubbleAssetCreate):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return lab_bubbles_pg.add_asset(conn, bubble_id, **body.model_dump(), actor="operator")


@app.patch("/api/bubbles/{bubble_id}/assets/{asset_id}")
def api_bubbles_assets_patch(bubble_id: str, asset_id: str, body: _BubbleAssetPatch):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            return lab_bubbles_pg.update_asset(
                conn,
                asset_id,
                **{k: v for k, v in body.model_dump().items() if v is not None},
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/bubbles/{bubble_id}/assets/{asset_id}/move")
def api_bubbles_assets_move(bubble_id: str, asset_id: str, body: _BubbleAssetMove):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            return lab_bubbles_pg.move_asset(
                conn,
                asset_id,
                body.target_bubble_id,
                reason=body.reason,
                actor="operator",
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/bubbles/{bubble_id}/services")
def api_bubbles_services_list(bubble_id: str):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return {"services": lab_bubbles_pg.list_services(conn, bubble_id)}


@app.post("/api/bubbles/{bubble_id}/services", status_code=201)
def api_bubbles_services_create(bubble_id: str, body: _BubbleServiceCreate):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return lab_bubbles_pg.add_service(conn, bubble_id, **body.model_dump())


@app.patch("/api/bubbles/{bubble_id}/services/{service_id}")
def api_bubbles_services_patch(bubble_id: str, service_id: str, body: _BubbleServicePatch):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        try:
            return lab_bubbles_pg.update_service(
                conn,
                service_id,
                **{k: v for k, v in body.model_dump().items() if v is not None},
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/bubbles/{bubble_id}/audit-events")
def api_bubbles_audit_events(bubble_id: str):
    from web import db_pg, lab_bubbles_pg
    with db_pg.connection(_database_url()) as conn:
        lab_bubbles_pg.init(conn)
        return {"events": lab_bubbles_pg.list_audit_events(conn, bubble_id)}
```

- [ ] **Step 5: Run test to verify it passes**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_bubble_api_create_assets_services_and_audit -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_cockpit_ui.py
git commit -m "Expose lab bubble APIs"
```

## Task 4: VM Page Payload Aggregation

**Files:**
- Modify: `autopilot-proxmox/web/lab_bubbles_pg.py`
- Modify: `autopilot-proxmox/web/app.py`
- Test: `autopilot-proxmox/tests/test_lab_bubbles_pg.py`

- [ ] **Step 1: Write failing payload test**

Append:

```python
def test_build_vm_page_payload_groups_fleets_infra_services_and_unassigned(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    lab_bubbles_pg.add_asset(pg_conn, bubble["id"], asset_type="vm", asset_role="workstation", vmid=101)
    dc = lab_bubbles_pg.add_asset(pg_conn, bubble["id"], asset_type="vm", asset_role="domain_controller", vmid=130)
    lab_bubbles_pg.add_service(
        pg_conn,
        bubble["id"],
        service_kind="dhcp",
        service_name="ACME DHCP",
        provider_asset_id=dc["id"],
        readiness_state="ready",
    )
    payload = lab_bubbles_pg.build_vm_page_payload(
        pg_conn,
        vms=[
            {"vmid": 101, "name": "WS01", "status": "running", "part_of_domain": False},
            {"vmid": 130, "name": "DC01", "status": "running", "part_of_domain": True},
            {"vmid": 200, "name": "LOOSE", "status": "stopped", "part_of_domain": False},
        ],
        agent_rows=[{"agent_id": "dc01", "vmid": 130, "domain_joined": True}],
    )

    assert payload["workstation_fleets"][0]["bubble"]["name"] == "ACME Lab"
    assert payload["workstation_fleets"][0]["workstation_count"] == 1
    assert payload["critical_infrastructure"][0]["role"] == "domain_controller"
    assert payload["connected_services"][0]["service_kind"] == "dhcp"
    assert payload["unassigned_assets"][0]["vmid"] == 200
    assert payload["gate_states"][0]["bubble_id"] == bubble["id"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_build_vm_page_payload_groups_fleets_infra_services_and_unassigned -q
```

Expected: FAIL for missing `build_vm_page_payload`.

- [ ] **Step 3: Add payload builder**

Add to `lab_bubbles_pg.py`:

```python
def build_vm_page_payload(conn: Connection, *, vms: list[dict], agent_rows: list[dict]) -> dict:
    bubbles = list_bubbles(conn)
    assets = list_assets(conn)
    services_by_bubble = {bubble["id"]: list_services(conn, bubble["id"]) for bubble in bubbles}
    vm_by_id = {int(vm["vmid"]): vm for vm in vms if vm.get("vmid") is not None}
    assets_by_bubble: dict[str, list[dict]] = {}
    assigned_vmids = set()
    for asset in assets:
        assets_by_bubble.setdefault(asset["bubble_id"], []).append(asset)
        if asset.get("vmid") is not None:
            assigned_vmids.add(int(asset["vmid"]))
    agent_by_vmid = {
        int(row["vmid"]): row
        for row in agent_rows
        if row.get("vmid") is not None
    }

    workstation_fleets = []
    critical_infrastructure = []
    connected_services = []
    gate_states = []

    for bubble in bubbles:
        bubble_assets = assets_by_bubble.get(bubble["id"], [])
        workstation_assets = [asset for asset in bubble_assets if asset["asset_role"] == "workstation"]
        infra_assets = [asset for asset in bubble_assets if asset["asset_role"] != "workstation"]
        workstation_vms = [vm_by_id[asset["vmid"]] for asset in workstation_assets if asset.get("vmid") in vm_by_id]
        running = sum(1 for vm in workstation_vms if vm.get("status") == "running")
        workstation_fleets.append({
            "bubble": bubble,
            "workstation_count": len(workstation_assets),
            "running_count": running,
            "stopped_count": max(0, len(workstation_assets) - running),
            "assets": workstation_assets,
            "vms": workstation_vms,
            "readiness": {
                "dc_ready": bubble["dc_ready"],
                "dns_ready": bubble["dns_ready"],
                "dhcp_ready": bubble["dhcp_ready"],
                "workload_ready": bubble["workload_ready"],
            },
        })
        for asset in infra_assets:
            vm = vm_by_id.get(asset.get("vmid"))
            agent = agent_by_vmid.get(asset.get("vmid"))
            critical_infrastructure.append({
                "bubble": bubble,
                "asset": asset,
                "role": asset["asset_role"],
                "vm": vm,
                "agent": agent,
            })
        for service in services_by_bubble.get(bubble["id"], []):
            connected_services.append({
                "bubble": bubble,
                **service,
            })
        gate_states.append({
            "bubble_id": bubble["id"],
            "workgroup": evaluate_launch_gate(
                conn,
                bubble["id"],
                requires_domain_join=False,
                requires_configmgr=False,
                is_multi_bubble_context=len(bubbles) > 1,
                is_multi_domain_context=False,
            ),
            "domain_join": evaluate_launch_gate(
                conn,
                bubble["id"],
                requires_domain_join=True,
                requires_configmgr=False,
                is_multi_bubble_context=len(bubbles) > 1,
                is_multi_domain_context=False,
            ),
        })

    unassigned_assets = [
        vm for vm in vms
        if vm.get("vmid") is not None and int(vm["vmid"]) not in assigned_vmids
    ]
    return {
        "workstation_fleets": workstation_fleets,
        "critical_infrastructure": critical_infrastructure,
        "connected_services": connected_services,
        "unassigned_assets": unassigned_assets,
        "warnings": [],
        "gate_states": gate_states,
    }
```

- [ ] **Step 4: Pass payload to template**

In `vms_page()` in `app.py`, before `TemplateResponse`, add:

```python
from web import db_pg, lab_bubbles_pg

with db_pg.connection(_database_url()) as conn:
    lab_bubbles_pg.init(conn)
    bubble_payload = lab_bubbles_pg.build_vm_page_payload(
        conn,
        vms=vms,
        agent_rows=_agent_inventory_rows(),
    )
```

Store `_agent_inventory_rows()` once:

```python
agent_devices = _agent_inventory_rows()
```

Use `agent_devices=agent_devices` and `bubble_payload=bubble_payload` in the template context.

- [ ] **Step 5: Run test to verify it passes**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_build_vm_page_payload_groups_fleets_infra_services_and_unassigned -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/lab_bubbles_pg.py autopilot-proxmox/web/app.py autopilot-proxmox/tests/test_lab_bubbles_pg.py
git commit -m "Build VM bubble page payload"
```

## Task 5: Reframe `/vms` Template

**Files:**
- Modify: `autopilot-proxmox/web/templates/vms.html`
- Test: `autopilot-proxmox/tests/test_cockpit_ui.py`

- [ ] **Step 1: Write failing page tests**

Add:

```python
def test_vms_page_uses_fleet_bubble_sections(web_client: TestClient, monkeypatch):
    from web import app as web_app

    async def fake_vms_payload():
        return {
            "data": [{"vmid": 101, "name": "WS01", "serial": "SER101", "status": "running"}],
            "devices": ([{"serial": "SER101", "display_name": "WS01"}], ""),
            "hash_serials": set(),
        }, 0

    monkeypatch.setattr(web_app, "_load_vars", lambda: {"hypervisor_type": "proxmox"})
    monkeypatch.setattr(web_app, "_get_vms_payload", fake_vms_payload)
    monkeypatch.setattr(web_app, "_latest_monitor_sweep_status", lambda: None)
    monkeypatch.setattr(web_app, "_agent_inventory_rows", lambda: [])

    res = web_client.get("/vms")
    assert res.status_code == 200
    body = res.text
    assert "VM Workstation Fleets" in body
    assert "Critical Infrastructure" in body
    assert "Connected Services" in body
    assert "Autopilot Devices (Intune)" not in body
    assert "/cloud" in body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_vms_page_uses_fleet_bubble_sections -q
```

Expected: FAIL because new headings are missing and old heading exists.

- [ ] **Step 3: Add new sections near the top of `vms.html`**

After the current top cockpit panel, insert:

```html
<section class="cockpit-panel" style="padding:16px;margin-bottom:14px;">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
    <div>
      <div class="cockpit-metric-label">VM Workstation Fleets</div>
      <div class="subtitle">Workstation groups by lab bubble, with lifecycle readiness and launch gates.</div>
    </div>
    <a class="action-btn" href="/cloud">Cloud device inventory</a>
  </div>
  {% if bubble_payload.workstation_fleets %}
  <table>
    <tr><th>Bubble</th><th>Workstations</th><th>Running</th><th>DC</th><th>DNS</th><th>DHCP</th><th>Launch state</th></tr>
    {% for fleet in bubble_payload.workstation_fleets %}
    <tr>
      <td>{{ fleet.bubble.name }}</td>
      <td>{{ fleet.workstation_count }}</td>
      <td>{{ fleet.running_count }}</td>
      <td>{% if fleet.readiness.dc_ready %}<span class="badge badge-green">ready</span>{% else %}<span class="badge badge-gray">waiting</span>{% endif %}</td>
      <td>{% if fleet.readiness.dns_ready %}<span class="badge badge-green">ready</span>{% else %}<span class="badge badge-gray">waiting</span>{% endif %}</td>
      <td>{% if fleet.readiness.dhcp_ready %}<span class="badge badge-green">ready</span>{% else %}<span class="badge badge-gray">waiting</span>{% endif %}</td>
      <td>
        {% set gates = (bubble_payload.gate_states | selectattr("bubble_id", "equalto", fleet.bubble.id) | list) %}
        {% if gates and gates[0].workgroup.state == 'warning' %}
          <span class="badge badge-yellow">workgroup warning</span>
        {% elif gates and gates[0].workgroup.state == 'allowed' %}
          <span class="badge badge-green">allowed</span>
        {% else %}
          <span class="badge badge-gray">not ready</span>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
    {{ empty_state("No workstation fleets assigned to bubbles yet.", hint="Provision into a bubble or adopt existing VMs to build fleet groups.") }}
  {% endif %}
</section>

<section class="cockpit-panel" style="padding:16px;margin-bottom:14px;">
  <div class="cockpit-metric-label">Critical Infrastructure</div>
  <div class="subtitle">Domain controllers, DNS/DHCP owners, file servers, ConfigMgr, and planned network assets.</div>
  {% if bubble_payload.critical_infrastructure %}
  <table>
    <tr><th>Bubble</th><th>Role</th><th>VM</th><th>Agent</th><th>State</th></tr>
    {% for row in bubble_payload.critical_infrastructure %}
    <tr>
      <td>{{ row.bubble.name }}</td>
      <td>{{ row.role | replace('_', ' ') }}</td>
      <td>{% if row.vm %}{{ row.vm.name }}{% if row.vm.vmid %} <small>VMID {{ row.vm.vmid }}</small>{% endif %}{% else %}—{% endif %}</td>
      <td>{% if row.agent %}{{ row.agent.agent_id }}{% else %}—{% endif %}</td>
      <td>{{ row.asset.evidence_state }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
    {{ empty_state("No critical infrastructure assigned yet.", hint="Domain controllers, DHCP/DNS owners, and server-role assets appear here after adoption or launch.") }}
  {% endif %}
</section>

<section class="cockpit-panel" style="padding:16px;margin-bottom:14px;">
  <div class="cockpit-metric-label">Connected Services</div>
  <div class="subtitle">Bubble-local and explicitly shared services consumed by fleets and infrastructure.</div>
  {% if bubble_payload.connected_services %}
  <table>
    <tr><th>Bubble</th><th>Service</th><th>Scope</th><th>Readiness</th><th>Evidence</th></tr>
    {% for svc in bubble_payload.connected_services %}
    <tr>
      <td>{{ svc.bubble.name }}</td>
      <td>{{ svc.service_kind }} · {{ svc.service_name }}</td>
      <td>{{ svc.scope }}</td>
      <td>{{ svc.readiness_state }}</td>
      <td>{{ svc.evidence_summary | tojson }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
    {{ empty_state("No connected services linked yet.", hint="AD, DNS, DHCP, Entra, file, and ConfigMgr services appear here when linked to a bubble.") }}
  {% endif %}
</section>
```

- [ ] **Step 4: Remove old Autopilot device section**

Delete the section starting at:

```html
<h2>Autopilot Devices (Intune)</h2>
```

through the matching `{% endif %}` after the registered device count paragraph. Leave the main VM table, missing VM section, and AutopilotAgent section in place.

- [ ] **Step 5: Run page test**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_vms_page_uses_fleet_bubble_sections -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/templates/vms.html autopilot-proxmox/tests/test_cockpit_ui.py
git commit -m "Reframe VM page around bubble fleets"
```

## Task 6: CloudOSD Bubble Launch Membership

**Files:**
- Modify: `autopilot-proxmox/web/cloudosd_endpoints.py`
- Test: `autopilot-proxmox/tests/test_cloudosd_endpoints.py`

- [ ] **Step 1: Write failing CloudOSD run test**

Append to `autopilot-proxmox/tests/test_cloudosd_endpoints.py`:

```python
def test_cloudosd_run_records_bubble_membership(cloudosd_client, pg_conn):
    from web import cloudosd_pg, lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    artifact = _create_artifact(pg_conn)

    res = cloudosd_client.post(
        "/api/cloudosd/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "ACME-WS01",
            "bubble_id": bubble["id"],
            "asset_role": "workstation",
        },
    )
    assert res.status_code == 201
    run_id = res.json()["run"]["run_id"]
    assets = lab_bubbles_pg.list_assets(pg_conn, bubble["id"])
    assert any(asset["run_id"] == run_id and asset["asset_role"] == "workstation" for asset in assets)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cloudosd_endpoints.py::test_cloudosd_run_records_bubble_membership -q
```

Expected: FAIL because `bubble_id` is ignored and no membership row exists.

- [ ] **Step 3: Add launch fields**

In `cloudosd_endpoints.py` `RunCreateBody`, add:

```python
bubble_id: Optional[str] = None
asset_role: str = "workstation"
allow_early_workgroup_launch: bool = False
```

- [ ] **Step 4: Create membership after run creation**

In `create_run`, after `cloudosd_pg.create_run(...)` succeeds and before returning:

```python
if body.bubble_id:
    from web import lab_bubbles_pg

    lab_bubbles_pg.init(conn)
    lab_bubbles_pg.add_asset(
        conn,
        body.bubble_id,
        asset_type="run",
        asset_role=body.asset_role or "workstation",
        run_id=run["run_id"],
        vmid=run.get("vmid") or run.get("requested_vmid"),
        vm_uuid=run.get("vm_uuid"),
        membership_state="provisioning",
        evidence_state="run_created",
        notes=f"CloudOSD run {run['workflow_name']}",
        actor="cloudosd",
    )
```

- [ ] **Step 5: Run test**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cloudosd_endpoints.py::test_cloudosd_run_records_bubble_membership -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autopilot-proxmox/web/cloudosd_endpoints.py autopilot-proxmox/tests/test_cloudosd_endpoints.py
git commit -m "Record CloudOSD bubble membership"
```

## Task 7: OSDeploy Bubble Launch Membership And Gates

**Files:**
- Modify: `autopilot-proxmox/web/osdeploy_endpoints.py`
- Test: `autopilot-proxmox/tests/test_osdeploy_endpoints.py`

- [ ] **Step 1: Write failing OSDeploy membership test**

Append:

```python
def test_osdeploy_run_records_bubble_membership(osdeploy_client, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    artifact = _create_osdeploy_artifact(pg_conn)

    res = osdeploy_client.post(
        "/api/osdeploy/v1/runs",
        json={
            "artifact_id": artifact["id"],
            "vm_name": "ACME-DC01",
            "server_role": "isolated_domain_controller",
            "role_options": _isolated_dc_options(),
            "bubble_id": bubble["id"],
            "asset_role": "domain_controller",
        },
    )
    assert res.status_code == 201
    run_id = res.json()["run"]["run_id"]
    assets = lab_bubbles_pg.list_assets(pg_conn, bubble["id"])
    assert any(asset["run_id"] == run_id and asset["asset_role"] == "domain_controller" for asset in assets)
```

- [ ] **Step 2: Write failing gate test**

Append:

```python
def test_osdeploy_preflight_blocks_domain_join_before_bubble_readiness(osdeploy_client, pg_conn):
    from web import lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    artifact = _create_osdeploy_artifact(pg_conn)

    body = {
        "artifact_id": artifact["id"],
        "vm_name": "ACME-FS01",
        "server_role": "file_server",
        "role_options": _file_server_options(),
        "bubble_id": bubble["id"],
        "asset_role": "file_server",
    }
    res = osdeploy_client.post("/api/osdeploy/v1/preflight", json=body)
    assert res.status_code == 200
    checks = res.json()["blocking_checks"]
    assert any(check["id"] == "bubble_not_ready" for check in checks)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_run_records_bubble_membership autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_preflight_blocks_domain_join_before_bubble_readiness -q
```

Expected: FAIL because `bubble_id` is ignored and no bubble gate exists.

- [ ] **Step 4: Add OSDeploy launch fields**

In `osdeploy_endpoints.py` `RunCreateBody`, add:

```python
bubble_id: Optional[str] = None
asset_role: str = "file_server"
allow_early_workgroup_launch: bool = False
```

- [ ] **Step 5: Add preflight gate**

In `preflight_payload(body)`, after existing role validation and before returning, add:

```python
if body.bubble_id:
    from web import db_pg, lab_bubbles_pg

    with db_pg.connection(web_app._database_url()) as conn:
        lab_bubbles_pg.init(conn)
        requires_domain_join = bool((body.role_options or {}).get("domain_join")) or body.server_role in {
            "file_server",
            "mecm_prereq",
            "lab_in_a_box",
        }
        requires_configmgr = body.server_role == "mecm_prereq"
        gate = lab_bubbles_pg.evaluate_launch_gate(
            conn,
            body.bubble_id,
            requires_domain_join=requires_domain_join,
            requires_configmgr=requires_configmgr,
            is_multi_bubble_context=len(lab_bubbles_pg.list_bubbles(conn)) > 1,
            is_multi_domain_context=False,
        )
    if not gate["allowed"]:
        blocking.append(_blocking_check("bubble_not_ready", "; ".join(gate["reasons"])))
    elif gate["state"] == "warning":
        warnings.append(_warning_check("bubble_warning", "; ".join(gate["reasons"])))
```

- [ ] **Step 6: Create membership after run creation**

In `create_run`, after `osdeploy_pg.create_run(...)` succeeds:

```python
if body.bubble_id:
    from web import lab_bubbles_pg

    lab_bubbles_pg.init(conn)
    lab_bubbles_pg.add_asset(
        conn,
        body.bubble_id,
        asset_type="run",
        asset_role=body.asset_role or body.server_role,
        run_id=run["run_id"],
        vmid=run.get("vmid") or run.get("requested_vmid"),
        vm_uuid=run.get("vm_uuid"),
        membership_state="provisioning",
        evidence_state="run_created",
        notes=f"OSDeploy run {run['workflow_name']}",
        actor="osdeploy",
    )
```

- [ ] **Step 7: Run tests**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_run_records_bubble_membership autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_preflight_blocks_domain_join_before_bubble_readiness -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add autopilot-proxmox/web/osdeploy_endpoints.py autopilot-proxmox/tests/test_osdeploy_endpoints.py
git commit -m "Wire OSDeploy bubble lifecycle gates"
```

## Task 8: DC Agent Readiness Evidence

**Files:**
- Modify: `autopilot-proxmox/web/lab_bubbles_pg.py`
- Modify: `autopilot-proxmox/web/agent_v1_endpoints.py`
- Test: `autopilot-proxmox/tests/test_agent_v1_endpoints.py`

- [ ] **Step 1: Add repository helper test**

Append to `autopilot-proxmox/tests/test_lab_bubbles_pg.py`:

```python
def test_asset_for_agent_returns_bubble_membership(pg_conn):
    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    asset = lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        agent_id="dc01-agent",
    )

    found = lab_bubbles_pg.asset_for_agent(pg_conn, bubble["id"], "dc01-agent")
    assert found["id"] == asset["id"]
    assert lab_bubbles_pg.asset_for_agent(pg_conn, bubble["id"], "missing") is None
```

- [ ] **Step 2: Run helper test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_asset_for_agent_returns_bubble_membership -q
```

Expected: FAIL for missing `asset_for_agent`.

- [ ] **Step 3: Add helper implementation**

Add to `lab_bubbles_pg.py`:

```python
def asset_for_agent(conn: Connection, bubble_id: str, agent_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM lab_bubble_assets
        WHERE bubble_id = %s
          AND agent_id = %s
          AND membership_state IN ('active', 'provisioning')
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (bubble_id, agent_id),
    ).fetchone()
    return _asset_row(row)
```

- [ ] **Step 4: Run helper test**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_asset_for_agent_returns_bubble_membership -q
```

Expected: PASS.

- [ ] **Step 5: Write failing heartbeat readiness test**

Append to `autopilot-proxmox/tests/test_agent_v1_endpoints.py`:

```python
def test_agent_heartbeat_updates_bubble_dc_dns_dhcp_readiness(agent_client, pg_conn):
    from web import agent_telemetry_pg, lab_bubbles_pg

    lab_bubbles_pg.reset_for_tests(pg_conn)
    lab_bubbles_pg.init(pg_conn)
    bubble = lab_bubbles_pg.create_bubble(pg_conn, name="ACME Lab")
    agent_telemetry_pg.upsert_device(
        pg_conn,
        agent_id="dc01-agent",
        token="secret-token",
        vmid=130,
        computer_name="DC01",
    )
    lab_bubbles_pg.add_asset(
        pg_conn,
        bubble["id"],
        asset_type="vm",
        asset_role="domain_controller",
        vmid=130,
        agent_id="dc01-agent",
    )

    response = agent_client.post(
        "/api/agent/v1/heartbeat",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "agent_id": "dc01-agent",
            "vmid": 130,
            "computer_name": "DC01",
            "current_phase": "full_os",
            "bubble_id": bubble["id"],
            "dc_readiness": {
                "ad_ds_ready": True,
                "dns_ready": True,
                "dhcp_ready": True,
                "dhcp_scope": "10.42.12.0",
                "dhcp_pool_start": "10.42.12.100",
                "dhcp_pool_end": "10.42.12.199",
            },
        },
    )
    assert response.status_code == 200
    refreshed = lab_bubbles_pg.get_bubble(pg_conn, bubble["id"])
    assert refreshed["dc_ready"] is True
    assert refreshed["dns_ready"] is True
    assert refreshed["dhcp_ready"] is True
    assert refreshed["workload_ready"] is True
```

- [ ] **Step 6: Run heartbeat test to verify it fails**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_agent_v1_endpoints.py::test_agent_heartbeat_updates_bubble_dc_dns_dhcp_readiness -q
```

Expected: FAIL because heartbeat payload does not accept or apply bubble readiness.

- [ ] **Step 7: Extend heartbeat body**

In `agent_v1_endpoints.py` `HeartbeatBody`, add:

```python
bubble_id: Optional[str] = None
dc_readiness: dict = Field(default_factory=dict)
```

- [ ] **Step 8: Feed DC readiness after heartbeat storage**

In `agent_v1_endpoints.py` `heartbeat()`, after `agent_telemetry_pg.record_heartbeat(...)`, add:

```python
        if body.bubble_id and body.dc_readiness:
            from web import lab_bubbles_pg

            lab_bubbles_pg.init(conn)
            dc_asset = lab_bubbles_pg.asset_for_agent(conn, body.bubble_id, body.agent_id)
            if dc_asset and dc_asset["asset_role"] == "domain_controller":
                lab_bubbles_pg.update_readiness_from_dc_evidence(
                    conn,
                    body.bubble_id,
                    dc_asset_id=dc_asset["id"],
                    evidence=body.dc_readiness,
                )
```

This preserves the rule that membership is authoritative: heartbeat evidence updates readiness only when the agent is already linked to the target bubble as a domain controller.

- [ ] **Step 9: Run heartbeat and repository tests**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest \
  autopilot-proxmox/tests/test_lab_bubbles_pg.py::test_asset_for_agent_returns_bubble_membership \
  autopilot-proxmox/tests/test_agent_v1_endpoints.py::test_agent_heartbeat_updates_bubble_dc_dns_dhcp_readiness \
  -q
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add autopilot-proxmox/web/lab_bubbles_pg.py autopilot-proxmox/web/agent_v1_endpoints.py autopilot-proxmox/tests/test_lab_bubbles_pg.py autopilot-proxmox/tests/test_agent_v1_endpoints.py
git commit -m "Feed bubble readiness from DC agent heartbeats"
```

## Task 9: Full Verification

**Files:**
- No source edits expected.

- [ ] **Step 1: Run focused unit and page tests**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest \
  autopilot-proxmox/tests/test_lab_bubbles_pg.py \
  autopilot-proxmox/tests/test_cockpit_ui.py::test_vms_page_uses_fleet_bubble_sections \
  autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_run_records_bubble_membership \
  autopilot-proxmox/tests/test_osdeploy_endpoints.py::test_osdeploy_preflight_blocks_domain_join_before_bubble_readiness \
  autopilot-proxmox/tests/test_agent_v1_endpoints.py::test_agent_heartbeat_updates_bubble_dc_dns_dhcp_readiness \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run existing VM page timezone regression**

```bash
/Users/Adam.Gell/repo/ProxmoxVEAutopilot/autopilot-proxmox/.venv/bin/python -m pytest autopilot-proxmox/tests/test_cockpit_ui.py::test_vms_agent_heartbeat_uses_local_timezone_markup -q
```

Expected: PASS.

- [ ] **Step 3: Run diff hygiene**

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Review final changed files**

```bash
git status --short
git diff --stat
```

Expected: only files from this plan are modified or committed. If unrelated files are present, do not stage or revert them.

## Self-Review

- Spec coverage: Tasks cover bubble schema, assets, services, audit, readiness gates, VM page sections, Autopilot device removal from `/vms`, CloudOSD membership, OSDeploy membership, OSDeploy launch gates, and DC agent AD/DNS/DHCP readiness evidence.
- Deferred scope: Proxmox bridge, VLAN, firewall, NAT, and router automation remain out of scope.
- Incomplete-marker scan: no incomplete markers remain.
- Lifecycle API coverage: Patch/move endpoints are planned for assets, patch endpoints are planned for services, and audit assertions pin both asset add and move events.
- DC evidence coverage: DC agent heartbeat evidence persists AD DS, DNS, DHCP readiness, DHCP scope, and DHCP pool bounds.
- Subagent readiness: Worker prompts define ownership, non-owned files, dependency order, handoff expectations, and integration checkpoints before final verification.
- Type consistency: `bubble_id`, `asset_id`, `service_id`, `asset_role`, `readiness_state`, `workstation_fleets`, `critical_infrastructure`, `connected_services`, `unassigned_assets`, `warnings`, and `gate_states` are used consistently across tasks.
