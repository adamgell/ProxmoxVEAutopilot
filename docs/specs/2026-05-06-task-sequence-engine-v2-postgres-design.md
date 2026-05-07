# Task Sequence Engine v2 + Content Manifest v1 Design

## Summary

Task Sequence Engine v2 is the next spine for ProxmoxVEAutopilot. The
current WinPE path proved that a ConfigMgr-style image-based OSD flow can
boot, apply an OS image, hand off through Windows mini-setup, run an OSD
client from `SetupComplete.cmd`, install QGA, and mark the run complete.

This design turns that successful path into a general ordered-tree task
sequence engine backed by PostgreSQL. It is intentionally aimed at the
first serious step toward an open DeployR-like platform: boot, build,
recover, and provision devices at scale with live observability and
repeatable content.

The first milestone is not a full DeployR replacement. It is the durable
engine and content contract that later supports apps, packages,
conditions, reboots, recovery, and final Autopilot provisioning.

## Goals

- Store task sequences as an ordered tree of groups and steps.
- Store all task sequence engine state in PostgreSQL.
- Compile editable task sequences into immutable per-run snapshots.
- Make WinPE and full Windows OSD agents use the same execution model.
- Support phase-aware execution across WinPE, Windows setup, full OS, and
  future recovery phases.
- Add a per-run content manifest so runs are reproducible and debuggable.
- Support conditions, variables, retry, timeout, continue-on-error,
  reboot/resume, and step event history.
- Preserve the existing working WinPE E2E path while migrating it onto
  the new engine.

## Non-Goals

- Full app deployment orchestration in the first engine migration.
- PXE/iPXE server implementation.
- Multi-site distribution point topology.
- Peer cache / branch cache.
- Full Intune or Autopilot automation beyond keeping the interfaces ready.
- A complete UI builder in the first backend milestone.
- Replacing all existing SQLite-backed app data in one change.

## Product Direction

The long-term product direction is an open OS deployment platform inspired
by DeployR, MDT, and ConfigMgr OSD:

- Boot physical or virtual machines into a trusted provisioning client.
- Build devices from known-good OS images and content manifests.
- Recover devices after misconfiguration, compromise, or disk loss.
- Provision final state with Autopilot, Intune, domain join, apps, and
  configuration baselines.
- Observe every deployment in real time with step logs, reboot state,
  package downloads, diagnostics, and failure explanations.

Engine v2 is the control plane for that direction.

## North Star Notes

These notes are intentionally explicit so future implementation sessions
keep the product goal in view.

The long-term goal is an open source, next-generation OSD and bare-metal
provisioning platform. The working name is not decided, but the product
intent is "open DeployR": a single standardized solution to boot, build,
recover, and provision machines at scale.

The platform should eventually:

- Boot machines through PXE/iPXE, boot ISO, virtual media, and recovery
  media.
- Build Windows endpoints from known-good OS images and versioned content.
- Recover devices from misconfiguration, failed deployments, ransomware,
  or other cyberattack scenarios.
- Replace MDT-style task sequences with a modern PowerShell and API-first
  implementation.
- Provide live deployment monitoring and troubleshooting from the web UI.
- Integrate with Windows Autopilot for final device provisioning.
- Capture hardware hashes and register or assign Autopilot profiles.
- Support apps, scripts, driver packages, OS images, boot images, tools,
  and recovery packages as first-class content.
- Make every run reproducible through immutable run plans and manifests.
- Make every failure explainable through step events, logs, diagnostics,
  screenshots, dumps, and recovery artifacts.

Short-term work should still be conservative. We are not trying to build
the whole platform overnight. The next milestone is the engine spine:
ordered task sequence tree, PostgreSQL state, immutable run snapshot,
content manifest v1, and agent protocol v2.

## Memory Anchors For Future Sessions

Future planning and implementation should preserve these decisions unless
the project intentionally changes direction:

- The immediate project is **Task Sequence Engine v2 + Content Manifest
  v1**, not a full DeployR replacement.
- PostgreSQL is the required database for the v2 engine.
- The task sequence authoring model is an ordered tree of groups and
  steps.
- The execution model is an immutable flattened run plan compiled from
  that tree.
- Content references resolve into an immutable per-run manifest.
- WinPE, Windows setup, full OS, and future recovery clients should share
  one agent protocol.
- Reliability/debuggability and feature parity are both core goals.
- The known-good live baseline is the ConfigMgr-style WinPE OSD path that
  completed run 22 on VM 119: WinPE steps OK, OSD `install_qga` OK, OSD
  `fix_recovery_partition` OK, final state `done`.
- The current hardcoded WinPE and OSD action lists are compatibility
  scaffolding. They should be represented by v2 run plans before being
  removed.
- Sequence edits must never mutate active deployments.
- Reboot/resume is a first-class engine feature, not an agent-side hack.
- Conditions must use a constrained expression model, not arbitrary code.
- Secrets should be referenced and resolved just-in-time, not copied into
  logs or immutable snapshots.

## Implementation Status

As of the first backend implementation pass:

- Phase 0 and Phase 1 are started: the compose stack declares Postgres,
  app startup can initialize the v2 schema, and the v2 repository stores
  task sequence trees, compiled versions, run plans, content manifests,
  events, and logs.
- Phase 2 is partially started: the repository can compile an ordered tree
  into an immutable flattened run plan with constrained condition
  evaluation.
- Phase 3 is started: `/osd/v2/agent/*` endpoints exist for register,
  next-step claim, logs, result reporting, rebooting, phase completion,
  and content manifest metadata.
- The existing `/winpe/*` and `/osd/client/*` paths remain the production
  OSD flow until Phase 4 explicitly cuts new runs over to v2.

## Current State

The current merged WinPE path has these important behaviors:

- A run starts in `queued`.
- Ansible creates a VM and posts VM identity.
- The VM boots WinPE.
- WinPE registers and receives a fixed action list from
  `compile_winpe()`.
- WinPE partitions the disk, applies the WIM, stages drivers, prepares
  Windows setup, stages the OSD client package, writes boot files, and
  hands off.
- `/winpe/done` moves the run to `awaiting_windows_setup`.
- Windows mini-setup runs.
- `SetupComplete.cmd` launches `OsdClient.ps1`.
- The OSD client registers, receives hardcoded OSD actions, installs QGA,
  fixes recovery partition layout, marks OSD steps complete, and marks
  the run done.

The current model works, but it is still hardcoded:

- WinPE actions are compiled as a fixed list.
- OSD actions are generated by `_build_osd_actions_for_run()`.
- Step semantics are duplicated across WinPE and OSD endpoints.
- There is no authorable group/condition/reboot model.
- There is no immutable content manifest.
- There is no general cursor that can resume after reboot across agents.

## Design Principles

1. **Authoring is editable; execution is immutable.**
   Sequence edits affect future runs only. A run executes the compiled
   snapshot it started with.

2. **The tree is for humans; the run plan is for machines.**
   Operators edit ordered groups and steps. Agents execute a flattened
   phase-aware run plan.

3. **Agents are phase clients, not special cases.**
   WinPE, Windows setup, full OS, and recovery agents all use the same
   register/next/result/log protocol.

4. **Content is resolved before execution.**
   Steps reference logical content. A run gets exact content versions,
   checksums, source URIs, and staging targets.

5. **Every state transition is auditable.**
   Step attempts, logs, retries, skips, condition results, and reboot
   requests become append-only events.

6. **PostgreSQL is the source of truth.**
   Concurrency, locks, event history, snapshots, and timeline queries
   should use database primitives instead of filesystem state.

## PostgreSQL Strategy

PostgreSQL should be introduced as the durable store for task sequence
engine v2. The existing SQLite stores can remain for unrelated app data
during the first migration, but all new v2 engine records live in
Postgres.

Recommended deployment shape:

- Add a `postgres` service to the production compose stack.
- Store credentials in existing vault/config conventions.
- Use SQLAlchemy or a lightweight migration layer such as Alembic.
- Keep schema migrations explicit and versioned.
- Use UUID primary keys for new engine tables where practical.
- Use JSONB for flexible step parameters, conditions, variables, and
  content metadata.
- Use relational columns for state, ordering, parentage, timestamps, and
  query-critical fields.
- Use row locks for claim/resume semantics.
- Use `LISTEN/NOTIFY` later for live UI updates, but do not require it in
  v1.

## Data Model Overview

There are four logical data areas:

1. **Authoring**
   Editable task sequence definitions.

2. **Versioning**
   Immutable sequence versions compiled from the authoring tree.

3. **Runs**
   Per-deployment run plan, cursor, step state, attempts, events, and
   logs.

4. **Content**
   Content library entries and per-run resolved manifests.

## Core Tables

### task_sequences

Editable top-level sequence metadata.

Columns:

- `id uuid primary key`
- `name text not null`
- `description text`
- `enabled boolean not null default true`
- `current_version_id uuid null`
- `created_at timestamptz not null`
- `updated_at timestamptz not null`
- `created_by text`
- `updated_by text`

### task_sequence_nodes

Editable ordered tree.

Columns:

- `id uuid primary key`
- `sequence_id uuid not null references task_sequences(id)`
- `parent_id uuid null references task_sequence_nodes(id)`
- `position integer not null`
- `node_type text not null` - `group` or `step`
- `name text not null`
- `description text`
- `kind text null`
- `phase text not null default 'any'`
- `enabled boolean not null default true`
- `condition_json jsonb not null default '{}'`
- `variables_json jsonb not null default '{}'`
- `params_json jsonb not null default '{}'`
- `continue_on_error boolean not null default false`
- `retry_count integer not null default 0`
- `retry_delay_seconds integer not null default 10`
- `timeout_seconds integer null`
- `reboot_behavior text not null default 'none'`
- `created_at timestamptz not null`
- `updated_at timestamptz not null`

Important constraints:

- Unique `(sequence_id, parent_id, position)`.
- `node_type='group'` requires `kind is null`.
- `node_type='step'` requires `kind is not null`.

### task_sequence_versions

Immutable compiled authoring snapshots.

Columns:

- `id uuid primary key`
- `sequence_id uuid not null references task_sequences(id)`
- `version integer not null`
- `source_hash text not null`
- `compiled_tree_json jsonb not null`
- `compiled_at timestamptz not null`
- `compiled_by text`
- `notes text`

Important constraints:

- Unique `(sequence_id, version)`.
- Unique `(sequence_id, source_hash)` to avoid duplicate snapshots.

### provisioning_runs

The high-level deployment record. This can either replace the current
SQLite provisioning runs table for v2 runs or coexist during migration
under a new name such as `osd_runs`.

Columns:

- `id uuid primary key`
- `legacy_run_id integer null`
- `sequence_id uuid not null`
- `sequence_version_id uuid not null`
- `state text not null`
- `phase text null`
- `cursor_step_id uuid null`
- `vmid integer null`
- `vm_uuid text null`
- `computer_name text null`
- `serial_number text null`
- `deployment_target_json jsonb not null default '{}'`
- `run_variables_json jsonb not null default '{}'`
- `started_at timestamptz not null`
- `finished_at timestamptz null`
- `last_error text null`
- `created_by text`

Run states:

- `queued`
- `awaiting_winpe`
- `running_winpe`
- `awaiting_windows_setup`
- `awaiting_osd_client`
- `running_full_os`
- `awaiting_reboot`
- `awaiting_recovery`
- `done`
- `failed`
- `cancelled`

### run_plan_steps

Flattened immutable steps for a single run.

Columns:

- `id uuid primary key`
- `run_id uuid not null references provisioning_runs(id)`
- `source_node_id uuid null`
- `parent_source_node_id uuid null`
- `ordinal integer not null`
- `depth integer not null`
- `path text not null`
- `name text not null`
- `kind text not null`
- `phase text not null`
- `state text not null default 'pending'`
- `condition_json jsonb not null default '{}'`
- `condition_result_json jsonb null`
- `variables_json jsonb not null default '{}'`
- `params_json jsonb not null default '{}'`
- `resolved_params_json jsonb not null default '{}'`
- `content_refs_json jsonb not null default '[]'`
- `continue_on_error boolean not null default false`
- `retry_count integer not null default 0`
- `retry_delay_seconds integer not null default 10`
- `timeout_seconds integer null`
- `reboot_behavior text not null default 'none'`
- `attempt integer not null default 0`
- `claimed_by text null`
- `claimed_at timestamptz null`
- `started_at timestamptz null`
- `finished_at timestamptz null`
- `last_error text null`

Step states:

- `pending`
- `skipped`
- `running`
- `retry_wait`
- `reboot_pending`
- `ok`
- `warning`
- `error`
- `cancelled`

Important constraints:

- Unique `(run_id, ordinal)`.
- Index `(run_id, state, phase, ordinal)`.

### run_step_events

Append-only event stream.

Columns:

- `id bigserial primary key`
- `run_id uuid not null`
- `step_id uuid null`
- `event_type text not null`
- `severity text not null default 'info'`
- `agent_id text null`
- `phase text null`
- `attempt integer null`
- `message text null`
- `data_json jsonb not null default '{}'`
- `created_at timestamptz not null`

Event types:

- `run_created`
- `phase_registered`
- `step_claimed`
- `condition_evaluated`
- `step_started`
- `step_log`
- `step_succeeded`
- `step_failed`
- `step_retry_scheduled`
- `step_skipped`
- `reboot_requested`
- `phase_completed`
- `run_completed`
- `run_failed`
- `run_cancelled`

### run_step_logs

Large log chunks, separated from event rows so timeline queries stay
fast.

Columns:

- `id bigserial primary key`
- `run_id uuid not null`
- `step_id uuid null`
- `agent_id text null`
- `stream text not null`
- `content text not null`
- `created_at timestamptz not null`

Streams:

- `stdout`
- `stderr`
- `setupcomplete`
- `osd-client`
- `dism`
- `diskpart`
- `agent`

### content_items

Logical content library entries.

Columns:

- `id uuid primary key`
- `name text not null`
- `content_type text not null`
- `description text`
- `enabled boolean not null default true`
- `created_at timestamptz not null`
- `updated_at timestamptz not null`

Content types:

- `os_image`
- `boot_image`
- `driver_package`
- `app_package`
- `script`
- `tool`
- `recovery_media`
- `autopilot_profile`

### content_versions

Immutable content versions.

Columns:

- `id uuid primary key`
- `content_item_id uuid not null references content_items(id)`
- `version text not null`
- `sha256 text not null`
- `size_bytes bigint null`
- `source_uri text not null`
- `metadata_json jsonb not null default '{}'`
- `created_at timestamptz not null`
- `created_by text`

Important constraints:

- Unique `(content_item_id, version)`.
- Index `sha256`.

### run_content_manifest

Resolved per-run content manifest.

Columns:

- `id uuid primary key`
- `run_id uuid not null references provisioning_runs(id)`
- `content_version_id uuid not null references content_versions(id)`
- `logical_name text not null`
- `content_type text not null`
- `required_phase text not null`
- `required boolean not null default true`
- `source_uri text not null`
- `sha256 text not null`
- `size_bytes bigint null`
- `staging_path text null`
- `status text not null default 'pending'`
- `metadata_json jsonb not null default '{}'
`
- `created_at timestamptz not null`

Manifest status values:

- `pending`
- `staged`
- `verified`
- `missing`
- `error`

## Ordered Tree Semantics

The authoring model is a rooted ordered tree:

```text
Task Sequence
  Group: Preflight
    Step: collect_identity
    Step: validate_network
  Group: Disk + OS
    Step: partition_disk
    Step: apply_os_image
    Step: apply_driver_package
  Group: Windows Setup
    Step: prepare_windows_setup
    Step: stage_osd_client
    Step: reboot_to_windows
  Group: Full OS
    Step: install_qga
    Step: install_app_package
    Step: capture_hash
  Group: Provisioning
    Step: register_autopilot
    Step: wait_for_intune
```

The compiler walks the tree depth-first by `position`.

Groups are not executable steps in the first version. They affect:

- ordering
- conditions
- variables
- enabled/disabled state
- display path
- subtree skip behavior

The compiled run plan contains executable steps only, with enough group
metadata to render the original tree shape in the UI.

## Compiler Behavior

Compile inputs:

- editable task sequence tree
- sequence-level variables
- deployment target metadata
- selected OS image and package bindings
- credential references
- content library references

Compile outputs:

- `task_sequence_versions` row
- `provisioning_runs` row
- `run_plan_steps` rows
- `run_content_manifest` rows

Compiler rules:

1. Disabled groups remove their subtrees from the compiled executable plan
   or compile those children as skipped metadata, depending on the UI
   timeline requirement. The recommended v1 behavior is to include
   executable descendants as `skipped` only at run time, not at compile
   time, so condition reporting is visible.

2. Disabled steps compile into run plan rows with state `skipped`.

3. Group variables flow down to children. Child variables override parent
   variables.

4. Group conditions are copied into each descendant's effective condition
   as an `all` expression.

5. Content references are resolved to exact `content_versions` at compile
   time.

6. The compiler stores both original `params_json` and
   `resolved_params_json`. The original values are useful for debugging;
   the resolved values are what agents execute.

7. Credentials are never written into logs or event data. If a step needs
   a secret, the run plan stores a credential reference and the server
   resolves it into the agent response only for the allowed phase and
   step.

## Condition Model v1

Conditions must be safe and portable. Do not run arbitrary PowerShell,
Python, Jinja, or SQL.

Represent conditions as JSON expressions:

```json
{
  "op": "all",
  "items": [
    {"op": "eq", "left": "phase", "right": "winpe"},
    {"op": "contains", "left": "model", "right": "Latitude"}
  ]
}
```

Supported operators in v1:

- `all`
- `any`
- `not`
- `eq`
- `ne`
- `contains`
- `starts_with`
- `ends_with`
- `exists`
- `in`
- `step_state`
- `content_available`

Variable lookup namespaces:

- `run.*`
- `target.*`
- `agent.*`
- `sequence.*`
- `step_outputs.*` later

Examples:

```json
{"op": "eq", "left": "agent.phase", "right": "winpe"}
```

```json
{"op": "content_available", "content": "virtio-win"}
```

```json
{"op": "step_state", "step": "install_qga", "state": "ok"}
```

## Variable Model v1

Variables are JSON values resolved in this precedence order:

1. built-in system variables
2. target/device variables
3. sequence variables
4. group variables from root to leaf
5. step variables
6. run override variables

Built-in variables:

- `run.id`
- `run.state`
- `run.phase`
- `target.vmid`
- `target.vm_uuid`
- `target.serial_number`
- `target.computer_name`
- `target.manufacturer`
- `target.model`
- `agent.phase`
- `agent.id`
- `agent.computer_name`

Template substitution in step parameters should be explicit and minimal:

```json
{"computer_name": "${target.computer_name}"}
```

Substitution happens server-side before the action is returned to the
agent.

## Agent Contract v2

The server exposes one protocol for every phase agent.

Endpoints:

- `POST /osd/v2/agent/register`
- `POST /osd/v2/agent/next`
- `POST /osd/v2/agent/step/{step_id}/result`
- `POST /osd/v2/agent/step/{step_id}/logs`
- `POST /osd/v2/agent/rebooting`
- `POST /osd/v2/agent/phase-complete`
- `GET /osd/v2/content/{manifest_id}`

Register request:

```json
{
  "run_id": "uuid",
  "agent_id": "winpe-a3a1",
  "phase": "winpe",
  "computer_name": "MININT-123",
  "build_sha": "8c153c6",
  "capabilities": ["dism", "diskpart", "powershell5"]
}
```

Next response:

```json
{
  "run_id": "uuid",
  "phase": "winpe",
  "actions": [
    {
      "step_id": "uuid",
      "kind": "apply_os_image",
      "attempt": 1,
      "timeout_seconds": 3600,
      "params": {},
      "content": []
    }
  ],
  "reboot_required": false,
  "bearer_token": "..."
}
```

The server should return a small batch of actions only when it is safe to
run them serially in the same phase without waiting for external state.
Default v1 behavior should be one action at a time.

## Step Claiming and Concurrency

Use PostgreSQL row locks to claim the next runnable step:

```sql
select *
from run_plan_steps
where run_id = $1
  and state = 'pending'
  and phase in ($2, 'any')
order by ordinal
for update skip locked
limit 1;
```

The claim transaction:

1. Finds the next runnable step.
2. Evaluates conditions.
3. Marks skipped if false.
4. Marks running if true.
5. Sets `claimed_by`, `claimed_at`, and `attempt`.
6. Appends a `step_claimed` or `step_skipped` event.
7. Returns the action to the agent.

Only one agent can claim a step at a time.

## Reboot and Resume

A step can declare `reboot_behavior`:

- `none`
- `request_reboot_continue`
- `request_reboot_repeat_step`
- `requires_reboot_before`
- `requires_reboot_after`

Recommended v1 support:

- `none`
- `request_reboot_continue`

When a step requests reboot:

1. Agent posts result with `reboot_required=true`.
2. Server marks step `ok` or `reboot_pending` depending on the behavior.
3. Server stores `cursor_step_id`.
4. Server moves run to `awaiting_reboot`.
5. Agent initiates reboot or asks the controller to reboot.
6. Agent returns in the next phase or same phase.
7. Server resumes at the next pending step.

The cursor is an optimization and display aid. The authoritative ordering
is still `run_plan_steps.ordinal`.

## Content Manifest v1

Content Manifest v1 resolves logical content to exact content versions for
one run.

Example manifest:

```json
{
  "run_id": "uuid",
  "items": [
    {
      "logical_name": "windows-11-enterprise",
      "content_type": "os_image",
      "version": "26100.1-enterprise",
      "sha256": "...",
      "source_uri": "proxmox-iso://isos/Win11.iso",
      "required_phase": "winpe",
      "staging_path": "X:\\sources\\install.wim"
    },
    {
      "logical_name": "virtio-win",
      "content_type": "driver_package",
      "version": "0.1.285",
      "sha256": "...",
      "source_uri": "proxmox-iso://isos/virtio-win-0.1.285.iso",
      "required_phase": "winpe",
      "staging_path": "D:\\"
    }
  ]
}
```

Initial content types:

- OS image
- boot image
- driver package
- OSD client package
- script package
- tool package

The manifest should be stored as rows for queryability and as a JSON
rendering for agents.

## Initial Step Kinds

WinPE phase:

- `capture_hash`
- `partition_disk`
- `apply_os_image`
- `apply_driver_package`
- `prepare_windows_setup`
- `stage_osd_client`
- `bake_boot_entry`
- `handoff_to_windows_setup`

Windows setup / full OS phase:

- `install_qga`
- `fix_recovery_partition`
- `run_powershell_script`
- `install_msi_package`
- `capture_hash`

Future phases:

- `register_autopilot_device`
- `assign_autopilot_profile`
- `wait_for_intune_device`
- `install_app_package`
- `collect_diagnostics`
- `upload_crash_dump`
- `recover_user_state`
- `restore_user_state`

## Migration Plan

### Phase 0: Add Postgres

- Add PostgreSQL service to local/dev/prod compose.
- Add database URL config.
- Add migration tooling.
- Add health check and app startup validation.
- Do not move existing SQLite stores yet.

### Phase 1: Engine Schema

- Add authoring, version, run plan, event, log, and content tables.
- Add migrations.
- Add repository layer for the v2 engine.
- Add tests for tree persistence and run snapshot creation.

### Phase 2: Compiler v2

- Compile current fixed WinPE sequence into the new run plan format.
- Create a compatibility sequence that mirrors the currently working flow.
- Preserve current UI and API while the v2 compiler runs behind a feature
  flag.

### Phase 3: Agent API v2

- Add `/osd/v2/agent/*` endpoints.
- Keep existing `/winpe/*` and `/osd/client/*` endpoints during migration.
- Add a WinPE agent compatibility mode that can consume v2 actions.
- Add an OSD client compatibility mode that can consume v2 actions.

### Phase 4: Cut WinPE Path to v2

- For new runs, create a v2 run plan and content manifest.
- Agents request next action from v2 endpoints.
- Existing v1 endpoints remain for old WinPE media until retired.

### Phase 5: UI

- Render run timeline from `run_plan_steps` and `run_step_events`.
- Add read-only tree view for run snapshots.
- Add basic authoring editor after backend semantics are stable.

## Testing Strategy

Postgres integration tests:

- tree insert, update, reorder
- sequence version creation
- immutable run snapshot
- content manifest resolution
- step claim with `for update skip locked`
- concurrent claim cannot double-assign a step
- condition false skips correctly
- group condition skips descendants
- retry schedules `retry_wait`
- timeout marks step error
- continue-on-error marks warning and continues
- reboot stores cursor and resumes
- sequence edit does not affect active run

Agent contract tests:

- WinPE agent can register and claim first WinPE step.
- Full OS agent cannot claim WinPE-only step.
- OSD client can resume after reboot.
- Agent token cannot operate on another run.
- Step result for wrong phase is rejected.

E2E tests:

- Existing WinPE E2E still reaches `done`.
- Existing VM hash capture still works.
- QGA installation remains observable as an OSD step.
- Recovery partition fix remains non-blocking unless configured otherwise.

## Open Questions

1. Should v2 task sequence authoring replace the current sequence editor
   immediately, or should it begin as an advanced/experimental editor?

2. Should content library files live on local disk first, Proxmox storage
   first, or object storage first?

3. Should the initial Postgres service be required for all app startup, or
   should only v2 engine features require it during transition?

4. Should old SQLite provisioning run history be migrated into Postgres or
   left as legacy history?

5. Should agent v2 use the existing HMAC bearer tokens or move directly to
   per-agent registration tokens stored in Postgres?

## Recommended First Implementation Cut

Build the smallest useful vertical slice:

1. Add Postgres service and migration infrastructure.
2. Add v2 schema for sequences, run plans, events, and content manifests.
3. Add repository tests for ordered tree and immutable run snapshots.
4. Add a compiler that transforms the current hardcoded WinPE flow into a
   v2 run plan.
5. Add read-only run timeline from v2 data.
6. Keep v1 agents running until the v2 run plan can exactly mirror the
   known-good VM 119 E2E flow.

This keeps the next milestone grounded: the first success condition is not
"new UI" or "all package types." It is that the engine can represent and
persist the OSD flow we already proved in production.
