# ProxmoxVEAutopilot Roadmap

This is the current execution roadmap for near-term work. Each item is
subagent-driven by default.

## Subagent Execution Rule

Every roadmap task below is a separate work package for
`superpowers:subagent-driven-development`.

- Use a clean worktree before implementation work.
- Check `.superpowers/sdd/progress.md` before dispatching any task; completed
  tasks in that ledger must not be re-dispatched.
- Dispatch one fresh implementer subagent per task. Do not run roadmap tasks
  inline unless the user explicitly overrides this file.
- Give the implementer a task brief file, not the entire roadmap plus chat
  history.
- Require the implementer to use TDD, keep commits task-scoped, run the listed
  verification, and write a task report file.
- After each implementer finishes, generate a review package from that task's
  base commit to HEAD and dispatch a task reviewer subagent for spec compliance
  and code quality.
- Critical or Important review findings go to a fix subagent, then back through
  the same task review gate.
- Mark a task complete only after the reviewer approves it and the ledger is
  updated.
- After all selected tasks are complete, dispatch one final whole-branch review
  before merge, deploy, or PR cleanup.

## Task 1: Fleet Duplicate Row Investigation

**Goal:** Remove the duplicate `LABZ1-DC01` fleet rows without deleting live VMs
or masking a real stale-state problem.

**Subagent brief:** Start from `/api/vms/fleet` payloads and the frontend fleet
merge/rendering path. Compare Proxmox VM identity, lab bubble membership,
AutopilotAgent/QGA identity, and any cached database rows before changing UI or
cleanup behavior.

**Likely files:**

- `autopilot-proxmox/web/app.py`
- `autopilot-proxmox/web/lab_bubbles_pg.py`
- `autopilot-proxmox/frontend/src/pages/VmsPage.tsx`
- `autopilot-proxmox/frontend/src/contracts.ts`
- Related fleet/API/frontend tests

**Verification:** Add focused backend and frontend regression coverage for the
duplicate source found, then verify the live `/react/vms` or VM fleet page after
deploy.

**Done:** The fleet payload has one coherent row per live lab VM identity, the
selected VM details still work, tests pass, and production shows no duplicate
`LABZ1-DC01` row.

## Task 2: Lab-Specific Gen 1 Hash Upload Boundary

**Goal:** Make Gen 1 Autopilot hardware-hash upload resolve an explicit lab
M365/Entra credential boundary instead of silently using controller-wide
`vault_entra_*` defaults.

**Subagent brief:** Follow the tracked TODO in
`docs/OSDEPLOY_V2_MATURITY_PLAN.md`. Preserve the controller-wide path as a
fallback only when no lab boundary is selected, and record target tenant/app
evidence in job args or equivalent durable evidence.

**Likely files:**

- `autopilot-proxmox/playbooks/upload_hashes.yml`
- `autopilot-proxmox/scripts/upload_hashes.ps1`
- `autopilot-proxmox/web/lab_bubbles_pg.py`
- `autopilot-proxmox/web/cloudosd_endpoints.py`
- Hash upload tests and job/evidence tests

**Verification:** Tests must prove tenant/app selection is lab-specific, job
evidence records the target tenant and app/client ID, secrets are not logged,
and the legacy controller-wide path still works when intentionally selected.

**Done:** A lab with an external Entra/Intune boundary can upload hashes to that
tenant with operator-visible evidence of where the hash went.

## Task 3: OSDeploy v2 M1 Live E2E Gate

**Goal:** Prove OSDeploy Windows Server Base through the live Proxmox E2E gate
before enabling broader server roles.

**Subagent brief:** Use `docs/OSDEPLOY_V2_MATURITY_PLAN.md` as the source of
truth. Keep the task focused on Server Base. Do not unlock File Server, Domain
Controller, MECM, or Lab in a Box until Server Base has complete live evidence.

**Likely files:**

- `autopilot-proxmox/web/osdeploy_endpoints.py`
- `autopilot-proxmox/web/osdeploy_cache.py`
- `autopilot-proxmox/scripts/osdeploy_remote_build.py`
- `autopilot-proxmox/scripts/osdeploy_build_job.py`
- `autopilot-proxmox/scripts/osdeploy_publish_job.py`
- OSDeploy API, cache, build-host, and React tests

**Verification:** Complete the numbered live E2E gate in
`docs/OSDEPLOY_V2_MATURITY_PLAN.md`, including build, publish, preflight,
provision, PE events, full-OS heartbeat, readiness `complete`, and CloudOSD /
Legacy WinPE regression checks.

**Done:** The live run detail shows all required Server Base evidence and final
readiness `complete`; regressions prove OSDeploy remains additive.

## Task 4: Managed Lab Reconciler First Slice

**Goal:** Build the first managed lab control-plane slice: durable lab profiles,
current-state plus append-only DB records, Proxmox/network reconcile validation,
SDN auto-fix execution, blocked retry state, `/api/labs`, `/react/labs`, and a
lab-aware deploy journey.

**Subagent brief:** Execute
`docs/superpowers/plans/2026-06-21-managed-lab-reconciler.md` task-by-task with
fresh implementer and reviewer subagents. The first write-capable provider is
Proxmox/network; AD, Entra, Intune, users, ESP, apps, and policies are modeled
but blocked from mutation in this slice.

**Likely files:** Use the file structure in the managed-lab implementation plan.

**Verification:** Each managed-lab plan task has its own test cycle and review
gate. Final verification includes backend tests, frontend tests, typecheck,
build, Playwright shell coverage, production deploy, and live proof.

**Done:** A lab can be represented as durable intent, validated against
Proxmox/network state, fixed through DB-backed fix actions, surfaced in
`/react/labs`, and used by the deploy journey without broadening provider write
scope beyond Proxmox/network.

## Supporting Track: Proxmox SDN Lab Networks

`docs/superpowers/plans/2026-05-23-proxmox-sdn-lab-networks.md` remains the SDN
foundation plan. If the current branch already contains `/react/networks`,
`web/proxmox_sdn.py`, and `web/sdn_endpoints.py`, do not re-run completed SDN
tasks. If gaps remain, execute only the missing SDN tasks with the same
subagent-driven contract above.
