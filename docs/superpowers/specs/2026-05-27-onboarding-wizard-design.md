# Onboarding Wizard Design

Date: 2026-05-27
Status: Approved for implementation planning
Branch: claude/naughty-buck-700ffc (branched from main @ 942a100)

## Problem

A brand-new operator lands on the ProxmoxVEAutopilot web UI with zero prior context. Today there is no in-UI flow that walks them from "I just logged in" to "I have a working first provision." They have to read docs/SETUP.md, docs/FIRST_RUN_E2E.md, hand-edit vars.yml and vault.yml entries, kick artifact builds from individual pages, and stitch the whole thing together themselves.

We need a wizard that closes this gap. A fresh git clone plus docker compose up plus a browser open should be enough to reach a green VM with no terminal commands run by the operator.

## Scope

In scope:
- A new wizard page at `/react/onboarding` that walks the operator through everything the controller does not already know.
- A new live setup-monitor page at `/react/onboarding/setup` that streams progress as the operator's choices materialize.
- Backend persistence so the wizard survives browser refresh, log out, multi-tab, and re-login.
- Probe endpoints so each step can validate against the live cluster ("Test this now").
- A "Get started" hero CTA on ShellIndexPage when wizard status is `pending` or `in_progress`.

Out of scope:
- Controller bootstrap (`/setup` + `/api/setup/v1/*` already exist for this).
- Auth changes (reuses existing `web/auth.py`).
- Reinvented job orchestration (reuses `jobs.py`, `install_tracking_pg`).
- A new monitoring story (the setup screen is a focused projection over existing install_tracking + jobs data).

## Constraints

- ASCII hyphens only across all code, copy, and commit messages. No em-dash, no en-dash, anywhere.
- Keyboard navigable end-to-end. Screen reader announces step state.
- Resumability is a default, not an opt-in. Auto-save on every change.
- TDD for the React state machine and the new backend endpoints. Vitest for the reducer; pytest for endpoints; Pester only if a new PowerShell helper appears.
- Do not modify the E2E harness in `autopilot-proxmox/tools/e2e/`.
- Do not modify the codex/react-workflow-ux-pass branch.

## Operator personas

The wizard records a persona pick on step 1, which adjusts defaults but never hides fields ("no hiding anything" rule).

- `lab` - homelabber. Defaults toward workgroup identity, skipped Autopilot tenant, build CloudOSD artifact, controller's own node as the trial target.
- `msp` - MSP technician onboarding a customer. Defaults toward AD-joined, tenant required, reuse existing artifact, auto-pick first node.
- `corp` - corporate IT first-time setup. Defaults toward AD-joined, tenant required, reuse existing artifact, explicit node prompt.

## Architecture

### Component map

```
React shell (existing)
  ShellIndexPage
    reads bootstrap.onboarding.status on render
    pending | in_progress -> hero "Get started" CTA
    launched              -> small "Resume setup monitor" link
    complete              -> nothing (normal shell)
              |
              v
/react/onboarding   (NEW page, the wizard)
  OnboardingMachine (pure-reducer TypeScript state machine)
  step components: WelcomePersona, Identity, Tenant, Artifact, ReviewLaunch
  each step has: explainer, field(s), inline validation,
                 "Test this now" probe button, "What if it fails" expander
  auto-save on every change via PUT /api/onboarding/state
              |
              v
/react/onboarding/setup   (NEW page, live monitor)
  polls GET /api/onboarding/setup-status
  renders phase rail + per-phase log stream
  terminal states drive next navigation

Backend (NEW module: web/onboarding_endpoints.py)
  GET    /api/onboarding/state
  PUT    /api/onboarding/state           (patch answers; If-Match required)
  DELETE /api/onboarding/state           (reset; explicit re-run)
  POST   /api/onboarding/probe/ad
  POST   /api/onboarding/probe/tenant
  POST   /api/onboarding/probe/artifact
  POST   /api/onboarding/launch          (create install_tracking run, kick jobs)
  GET    /api/onboarding/setup-status    (phase snapshot for the monitor page)

Persistence (NEW table: onboarding_state)
  id              uuid primary key
  owner_sub       text unique  (Entra sub, or 'local-operator' in local-auth mode)
  status          text         pending | in_progress | launched | complete | aborted
                               (post-launch success/failure is derived from
                                install_tracking, not persisted here)
  current_step    text         wizard-step pointer; frozen once status flips
                               to 'launched'
  answers         jsonb        every field collected so far
  launched_run_id uuid         null until launch; fk to install_tracking
  persona         text         'lab' | 'msp' | 'corp'
  created_at      timestamptz
  updated_at      timestamptz  used as ETag for If-Match concurrency

Reused (untouched)
  auth.py                                session enforcement via Depends(current_user)
  jobs.py + jobs_pg                      launch fires existing job kinds
  install_tracking_pg                    underlying phase store
  proxmox_permissions                    reused by Proxmox health card on step 1
  proxmox_sdn                            reused for read-only "already configured" card
  settings_vault                         vault.yml IO for AD + local admin passwords
```

### Key decisions

- One row per operator, keyed on Entra `sub`. In local-auth mode the key is hardcoded `local-operator` so the schema is uniform.
- Auto-save on every change. Refresh-resume is the default behavior.
- State machine lives in the frontend. Backend is dumb persistence + probe runners + a one-shot launch transaction.
- Persona is a recorded fact, not a feature gate. All fields render regardless of persona; persona only flips which defaults are pre-selected and which copy variant renders.
- CTA on ShellIndexPage; no auto-redirect. The operator opts into the wizard.
- The setup monitor page is a sibling, not a takeover. The URL is bookmarkable.
- `DELETE /api/onboarding/state` wipes the wizard row only. It does not abort an in-flight `install_tracking` run; the operator aborts jobs through the existing `/react/jobs` page.

## Wizard steps

Five steps. Each carries the same shape: explainer copy, field(s), inline validation, "Test this now" probe button, "What if it fails" expander.

### 1. Welcome + Persona

- Top of page: read-only "Already configured" card showing what the controller knows (Proxmox host + node + version, default storage pools, default bridge, AD vault status). Calls existing health surfaces on render. The operator does not enter any of this.
- Asks: lane pick (Lab / MSP / Corp).
- Probe: none.

### 2. Identity

- Asks: Workgroup or AD-joined. AD branch asks domain (default `home.gell.one` if vault has a home.gell.one-shaped service account, else blank), join account, join password, local admin password.
- Probe: `POST /api/onboarding/probe/ad`. Runs DNS resolution, ICMP, LDAP bind. Surfaces which check failed.
- Failure remediations (verbatim copy in the expander):
  - "DNS does not resolve the domain. Open Settings > DNS and confirm your forwarder includes a domain controller."
  - "LDAP bind refused. The join account exists but cannot read the directory. Grant it 'Account Operators' or an equivalent group in AD Users and Computers."
  - "ICMP blocked. Some networks drop ping but allow LDAP. If the next probe attempt succeeds you can ignore this."

### 3. Tenant (Autopilot)

- Asks: `CloudAssignedTenantId`, `CloudAssignedTenantDomain`, `Comment_File`. Marked optional if persona is `lab` AND identity is `workgroup`.
- Probe: `POST /api/onboarding/probe/tenant`. Validates JSON shape. If Graph creds exist in vault, sanity-checks tenant id against Graph.
- Failure remediations:
  - "Graph creds missing. Open Settings > Entra and add an app secret with Directory.Read.All."
  - "Tenant id format invalid. Check the value in https://entra.microsoft.com under Overview."

### 4. Artifact

- Asks: use existing CloudOSD or OSDeploy artifact (picker shows what is on disk with timestamps), or build a new one now.
- Probe: `POST /api/onboarding/probe/artifact`. Returns inventory from `cloudosd_cache` + `osdeploy_cache`.
- Build-while-you-fill: if the operator kicks a build here, the wizard does not block. The build job appears in the monitor page's phase rail once the operator reaches step 5.
- Failure remediations:
  - "Build host unreachable. Settings > Build host." (link)
  - "Source media missing. Upload media at /react/files." (link)

### 5. Review + Launch

- Renders every prior answer with inline edit links back to its step.
- New fields on this step: trial VM parameters - VM name (default `autopilot-trial-<vmid>` where `<vmid>` is the Proxmox VMID the controller will allocate at clone time), target node (default the controller's known node for lab, auto-pick first for msp, prompt for corp), OS edition (default Win11 Pro). The VM name is editable; the `<vmid>` placeholder updates live as the operator types if they keep the suffix pattern.
- "Start setup" button at the bottom. Disabled with a precondition tooltip if anything is missing.

### Persona-default crosswalk

| Step               | Lab default                | MSP default                  | Corp default          |
| ------------------ | -------------------------- | ---------------------------- | --------------------- |
| Identity           | Workgroup                  | AD-joined                    | AD-joined             |
| Tenant             | Optional                   | Required                     | Required              |
| Artifact           | Build CloudOSD             | Use existing if present      | Use existing if present |
| Trial VM target    | controller's own node      | auto (first node)            | prompt explicitly     |

## Setup monitor screen

Page: `/react/onboarding/setup`. Landed on right after "Start setup."

### Phase definitions

Phases are projected from wizard answers. Skipped phases appear in the rail with `skipped` status (greyed and labeled) so the operator can see what was elided and why.

| Phase             | Triggers                                          | Always runs |
| ----------------- | ------------------------------------------------- | ----------- |
| Validate          | Sanity-check answers, probe live cluster once more | Yes        |
| Build artifact    | Phase tracks the existing build job if one was kicked from step 4's "build now"; never starts a new build at launch | Conditional |
| Clone template    | Proxmox clone + storage move                      | Yes         |
| Inject Autopilot  | Panther-inject path; tenant values from step 3    | Identity != workgroup |
| Provision         | Start VM, run task sequence                       | Yes         |
| Watch OOBE        | Polls QGA + agent telemetry until desktop or timeout | Yes      |

### Data source

- `GET /api/onboarding/setup-status` polled every 2s while the tab is foreground, every 10s when backgrounded (Page Visibility API).
- Returns: phase list with status (`waiting` | `running` | `ok` | `failed` | `skipped`), durations, current_job_id, last N log lines.
- Implementation reads `install_tracking` rows + recent `jobs` rows + tails the job log file. The phase model is a thin projection over those tables. The operator never sees the install_tracking nomenclature directly.
- Log stream is server-side filtered by job-tag to match the active phase.

### Terminal states

- All phases ok: completion card with the VM's IP, RDP shortcut, "Open VM detail" deep link to `/react/vms/<vmid>` (Proxmox VMID, matching the existing route pattern `/^\/react\/vms\/\d+$/`). Wizard status set to `complete`; ShellIndexPage hero CTA disappears.
- Any phase failed: phase rail shows the failure inline. The "What if it fails" expander for that phase auto-expands with the actual failure summary and three concrete next steps.
  - "Retry phase" button is shown only when the phase's job kind is idempotent. Idempotent phases at launch: Validate, Build artifact, Watch OOBE. Non-idempotent phases (Clone template, Inject Autopilot, Provision) get a "Back to wizard" button only, plus a manual-cleanup hint pointing at the VM in `/react/vms/<id>` so the operator can destroy it before retrying.
  - "Back to wizard" always present; returns the operator to the step that produced the bad answer and flips wizard status back to `in_progress`.
- Mid-run navigate-away: wizard status stays `launched`. ShellIndexPage hero changes to "Resume setup monitor". URL is bookmarkable.

## Data flow + state machine

### Frontend state machine

`frontend/src/onboarding/machine.ts`. Pure reducer: `(state, event) -> nextState`. No side effects in the reducer itself.

```
Persisted statuses (onboarding_state.status):
        pending -> in_progress -> launched -> complete
                                           \
                                            +-> (back to in_progress on retry)
        any              -> aborted   (explicit DELETE)

Wizard-only steps (current_step; only mutate while status='in_progress'):
        welcome -> identity -> tenant -> artifact -> review
                  (each can jump back to any prior step via edit link;
                   "review" is the terminal step before launch)

Monitor-only sub-states (derived from launched_run_id; never persisted on
the wizard row):
        running -> succeeded | failed
        failed  -> retry-phase | back-to-wizard:<step>
                                 (back-to-wizard flips status to in_progress
                                  and restores current_step to the step the
                                  operator picked to revisit)
```

- Forward transitions require the current step's gate to pass. Identity step gate: "either workgroup, or (AD + bind probe succeeded)." Backward transitions are always allowed.
- Each transition emits an `IntentToPersist` event. Page middleware commits it via `PUT /api/onboarding/state`.
- Launch is a one-shot atomic operation: `POST /api/onboarding/launch` returns the new `install_tracking` run id and flips wizard status to `launched` in a single transaction. No duplicate-runs if the operator double-clicks.

### Backend `answers` jsonb schema

```jsonc
{
  "schema_version": 1,
  "persona": "lab" | "msp" | "corp",
  "identity": {
    "mode": "workgroup" | "ad",
    "ad_domain": string | null,
    "ad_join_account": string | null,
    "ad_join_password_ref": "vault:onboarding/ad_join_password" | null,
    "local_admin_password_ref": "vault:onboarding/local_admin_password"
  },
  "tenant": {
    "skipped": boolean,
    "tenant_id": string | null,
    "tenant_domain": string | null,
    "comment_file": string | null
  },
  "artifact": {
    "kind": "cloudosd" | "osdeploy",
    "source": "existing" | "build",
    "existing_artifact_id": string | null,
    "build_job_id": string | null
  },
  "trial": {
    "vm_name": string,
    "target_node": string,
    "os_edition": "win11-pro" | "win11-ent" | "win10-pro"
  },
  "probe_results": {
    "ad":       { "at": iso8601, "ok": boolean, "detail": string } | null,
    "tenant":   { "at": iso8601, "ok": boolean, "detail": string } | null,
    "artifact": { "at": iso8601, "ok": boolean, "detail": string } | null
  }
}
```

- Secrets never round-trip. The wizard PUTs the actual secret value once at the password field on the form; the controller writes it to `vault.yml` via the same path `settings_vault` uses today and replaces the value in `answers.identity` with the sentinel ref string shown in the schema. On GET, every `*_password_ref` field returns the shape `{ref: "vault:onboarding/...", is_set: true|false}` so the UI can render a "set" indicator without seeing the value.
- Optimistic concurrency: `PUT /api/onboarding/state` requires an `If-Match` header carrying the row's `updated_at` value, encoded as a weak ETag (e.g. `W/"2026-05-27T14:32:11.482Z"`). The GET response sets the same value as its `ETag` header. Stale write returns 409 and the page re-fetches. Two tabs cannot overwrite each other silently.

## Error handling + resume

- Probe failures are non-blocking on the form itself. The operator can keep editing. They only gate the forward transition.
- Save failures retry with exponential backoff (3 attempts: 1s / 3s / 9s) then surface a banner: "Couldn't save your last change. Retry?" The form remains editable.
- 401 from any endpoint redirects to `/auth/login?next=<current-url>` so re-login resumes exactly.
- On page load, `GET /api/onboarding/state` returns the row. Frontend hydrates the state machine to `current_step`. If `status==launched`, page redirects itself to `/react/onboarding/setup`. If `status==complete`, page redirects to ShellIndexPage.
- Multi-tab: both tabs read the same row; only one can advance at a time thanks to `If-Match`. The stale tab gets a toast: "This changed in another tab, reload."

## Testing

- TDD-first for the state machine. Vitest suite drives the reducer through every transition (forward gates, backward jumps, idempotent re-emits, launch one-shot) before any UI lands. Pattern follows existing `frontend/src/*.test.tsx` and `routes.test.ts`.
- Pytest for `onboarding_endpoints.py` and each probe handler. One happy + one sad case per endpoint minimum. Patterns follow `tests/test_settings_vault.py` and `tests/test_react_shell.py`.
- The `setup-status` projection has its own pytest with a fixture install_tracking + jobs row to assert the phase model collapses correctly.
- No PowerShell expected. If a PS helper appears mid-build, Pester suite mirrors `tools/cloudosd-build/tests/*.Tests.ps1` style.
- Manual end-to-end gate: walk the wizard against the live controller at autopilot.gell.one and watch a trial provision reach `complete` against pve2. UI work is not done because tests pass; it is done because the user flow works.

## Accessibility

- Each wizard step is a `<form>` with one `<h1>` and `<fieldset>`-grouped controls. Tab order matches visual order.
- Step rail is `<nav aria-label="Onboarding steps">` with `aria-current="step"` on the active step.
- Probe results announce via `aria-live="polite"` regions; failures get `role="alert"`.
- "What if it fails" is a real `<details>`/`<summary>` element so it is keyboard-toggleable with Space/Enter.
- Setup monitor phase rail is `aria-live="polite"`. Status transitions announce as "Phase X of N, <phase name>, now <status>" where N is the count of phases actually included for this run (skipped phases still count toward N so the operator hears a stable total).
- Log stream is muted from screen readers by default; a "Read latest log line" button surfaces the most recent line on demand.
- No focus traps anywhere. Esc on any modal-ish panel returns focus to the trigger.

## File inventory (anticipated)

New files:
- `autopilot-proxmox/frontend/src/pages/OnboardingPage.tsx`
- `autopilot-proxmox/frontend/src/pages/OnboardingSetupPage.tsx`
- `autopilot-proxmox/frontend/src/onboarding/machine.ts`
- `autopilot-proxmox/frontend/src/onboarding/steps/*.tsx` (one per step)
- `autopilot-proxmox/frontend/src/onboarding/machine.test.ts`
- `autopilot-proxmox/frontend/src/OnboardingPage.test.tsx`
- `autopilot-proxmox/frontend/src/OnboardingSetupPage.test.tsx`
- `autopilot-proxmox/web/onboarding_endpoints.py`
- `autopilot-proxmox/web/onboarding_pg.py`
- `autopilot-proxmox/web/onboarding_phases.py` (the install_tracking projection)
- `autopilot-proxmox/tests/test_onboarding_endpoints.py`
- `autopilot-proxmox/tests/test_onboarding_phases.py`
- Postgres migration adding the `onboarding_state` table

Modified files (small, additive):
- `autopilot-proxmox/frontend/src/App.tsx` (route additions)
- `autopilot-proxmox/frontend/src/routes.ts` (new entries; navParent grouping)
- `autopilot-proxmox/frontend/src/pages/ShellIndexPage.tsx` (CTA based on bootstrap.onboarding)
- `autopilot-proxmox/frontend/src/contracts.ts` (bootstrap shape gains onboarding status)
- `autopilot-proxmox/web/app.py` (register the new router; inject onboarding status into bootstrap)

## Open questions resolved during brainstorming

- Persona handling: detect on step 1; persona changes defaults only, never hides fields.
- SDN / lab bubble exposure: nothing hidden. Read-only "already configured" card on step 1 surfaces what the controller knows; full SDN page remains for explicit management later.
- Trial provision target: not a separate wizard question. It is a consequence of the operator's earlier choices, configured on the review step.
- Local-auth-mode sentinel: hardcoded `local-operator` value so the schema is uniform across auth modes.
- Re-run semantics: `DELETE /api/onboarding/state` wipes the wizard row only; leaves any in-flight `install_tracking` run alone.

## What "done" means

A fresh git clone, plus `docker compose up`, plus `https://<controller>` in a browser, plus an operator who has never seen this tool, ends with:

- A green VM listed under `/react/vms/`.
- The setup monitor page showing all phases ok.
- The ShellIndexPage hero CTA gone.
- No terminal commands executed by the operator.
