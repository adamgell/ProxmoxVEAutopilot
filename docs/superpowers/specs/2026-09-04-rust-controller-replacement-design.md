# Rust Controller Replacement Design

**Date:** 2026-09-04  
**Status:** approved architecture captured for written review  
**Baseline:** `v2026.09.2` (`c8ab4b2`)  
**Primary development environment:** Apple Silicon macOS  

## 1. Purpose

Replace the current Python/Ansible orchestration authority with a Rust controller that owns durable operations, scheduling, reconciliation, evidence, and Proxmox mutations. Preserve the existing React UI, FastAPI API surface, Windows deployment clients, and deployment engines behind compatibility boundaries until each contract is proven and deliberately migrated.

The replacement is built and proven on macOS before production cutover is considered. The current production controller at `192.168.2.4` remains unchanged until a separately approved cutover. Read-only production observations are permitted for compatibility research; they are not implementation or deployment proof.

## 2. Approved product boundary

Rust owns:

- durable run, operation, attempt, lease, evidence, and decision state;
- job scheduling, concurrency, cancellation, orphan handling, and reconciliation;
- typed Proxmox operations and authoritative postcondition evaluation;
- callback-driven state transitions;
- task-sequence execution cursors and controller-owned deadlines;
- artifact lifecycle metadata and publish orchestration;
- human-readable failure summaries with sanitized technical evidence.

The existing system temporarily retains:

- React as the operator UI;
- FastAPI as the compatibility facade for existing routes and clients;
- the current WinPE, CloudOSD, OSDeploy, SetupComplete, and AutopilotAgent wire protocols;
- OSDCloud and OSDeploy as Windows image-application engines;
- Windows build-host PowerShell and .NET clients;
- Graph and Entra integration as separately authorized external adapters;
- allowlisted Ansible playbooks as transitional behavior adapters.

RustedOutClient is out of scope. It is neither a dependency nor a delivery target for this program.

## 3. Deferred work

The following tracks pause until the Rust contracts stabilize, because each will consume or change controller behavior:

- deployment reliability and recovery enhancements;
- VM 109 and generalized Autopilot OOBE remediation;
- remote-control implementation;
- operator-experience changes;
- adoption and first-run evaluation work;
- broad Ansible fragility remediation;
- nonessential PostgreSQL operational improvements.

Production `v2026.09.2` remains the operational baseline during local Rust development. Urgent production break/fix remains a separate, narrowly authorized path and does not expand the Rust project scope.

## 4. Current compatibility target

The deployed baseline was observed read-only on 2026-09-04:

- web, monitor, and four builder identities reported healthy service heartbeats at `c8ab4b2`;
- no job was queued or running during the observation;
- current jobs persist a generic executable argument vector, often `ansible-playbook` plus `/app` paths and `-e` parameters;
- the controller currently projects job, monitoring, VM, CloudOSD, OSDeploy, task-sequence, and agent state from PostgreSQL;
- runtime-container discovery through the MCP process could not access its Docker socket, proving that database heartbeats and container-runtime inspection are distinct health signals.

Only sanitized structural observations belong in fixtures or documentation. Raw VM inventories, tenant identifiers, credential references, and job payloads must not be copied into the repository.

## 5. Architectural approach

Use a new Rust operation journal alongside compatibility projections. Do not immediately rewrite the existing Python repositories in place and do not make Rust a generic consumer of arbitrary `cmd_json` commands.

```text
React and existing clients
          |
          v
FastAPI compatibility facade
          |
          v
Rust command intake and contract validation
          |
          v
Run -> Operation -> Attempt -> Evidence -> Decision
          |                         ^
          v                         |
PostgreSQL journal/outbox      typed observers
          |                  PVE / QGA / Agent
          v
compatibility projections
          |
          +-> transitional adapters
          +-> Rust-native operations
```

This separates authoritative decisions from compatibility formatting. Existing API status strings remain projections; they do not become the Rust domain model.

## 6. Rust workspace boundaries

The repository will gain one Cargo workspace with focused crates:

| Crate | Responsibility |
|---|---|
| `controller-domain` | Strongly typed IDs, lifecycle enums, commands, events, transition functions, evidence requirements, and readiness milestones. No I/O. |
| `event-journal` | Canonical JSON hashing, append-only semantic events, aggregate revisions, idempotency conflict detection, and transactional-outbox records. |
| `postgres-store` | SQLx repositories, additive migrations, PostgreSQL-clock leases/deadlines, advisory-lock conventions, and projection persistence. |
| `scheduler` | Authority generation, worker leases, per-kind limits, claims, heartbeats, cancellation, orphan handling, and startup fencing. |
| `operation-controller` | Preconditions, attempts, postcondition evaluation, continuation binding, recovery decisions, and retry authorization. |
| `pve-port` | Typed read and mutation traits plus fake and HTTP implementations. |
| `pve-observer` | PVE VM configuration, UPID, storage, resource, and host-side QGA observations. |
| `ansible-adapter` | Versioned allowlist for transitional playbooks and helper processes; no generic database-driven command execution. |
| `api-compat` | Existing request/response fields, callback authentication, schema versions, UUID/status serialization, and legacy environment names. |
| `cloudosd-adapter` | CloudOSD-specific package, callback, and readiness mapping. |
| `osdeploy-adapter` | OSDeploy-specific package, callback, role, and readiness mapping. |
| `agent-adapter` | Agent registration, heartbeat, approval, and work-item compatibility. |
| `artifact-index` | Authoritative artifact metadata, hashes, versions, and transfer decisions; artifact bytes remain external. |
| `projections` | Rebuildable job, deployment-health, machine-lifecycle, and operator-readiness views. |
| `controller-service` | Tokio/Axum process, configuration, health, metrics, graceful shutdown, and mode selection. |

The initial supported targets are macOS ARM64 for development and Linux AMD64 for the eventual Ubuntu controller artifact.

## 7. Domain model

### 7.1 Identity

- `run_id`: existing workflow UUID when one exists; otherwise a new UUIDv7.
- `operation_id`: persisted UUIDv7 with a unique semantic key of `(workflow_kind, run_id, operation_key, contract_version)`.
- `attempt_id`: UUIDv7 unique to one execution attempt.
- `attempt_number`: monotonically increasing integer within an operation.
- `lease_token`: unpredictable value unique to one acquisition.
- `authority_generation`: monotonically increasing database value identifying the active executor family.
- `event_id`: UUIDv7 for one append-only journal event.

Deterministic UUIDv5 is not used in v1. Semantic uniqueness is enforced by the database key while opaque UUIDv7 values avoid a permanent namespace/versioning commitment.

### 7.2 Separate state dimensions

The controller must not overload one status string for execution, observation, and readiness.

- Execution: `pending`, `leased`, `running`, `waiting`, `cancelling`, `satisfied`, `failed`, `blocked`, `unknown`, `conflicted`.
- Observation health: `fresh`, `stale`, `unavailable`, `unauthorized`, `timed_out`, `contradicted`.
- Readiness milestones: workflow-specific facts such as `vm_created`, `pe_registered`, `os_installed`, `agent_connected`, `qga_verified`, `verified_oobe`, `enrolled`, `esp_complete`, and `usable_endpoint`.

Rust enums and database `CHECK` constraints enforce known v1 states. Legacy unknown status strings may be retained as raw compatibility evidence but cannot enter the authoritative journal as a valid state.

### 7.3 Idempotency

Every command carries an idempotency key and canonical payload digest.

- Duplicate key plus identical digest returns the existing command outcome.
- Duplicate key plus different digest becomes `conflicted` and performs no mutation.
- Attempts never replace the stable operation identity.
- A callback is appended once by its semantic callback key and digest.

## 8. Evidence and postconditions

Evidence is typed, timestamped, source-identified, sanitized, and append-only. Authority is fact-specific rather than a single global source order.

| Fact | Authoritative source |
|---|---|
| PVE task completion | Matching Proxmox UPID status plus the intended resource postcondition |
| VM identity/configuration | Fresh Proxmox VM configuration and cluster-resource observations |
| Storage/media state | Fresh Proxmox storage and VM configuration observations |
| Host-side QGA channel | Proxmox `agent/ping` observation |
| Guest agent identity/state | Authenticated matching AutopilotAgent heartbeat |
| PE workflow progress | Authenticated, run-bound CloudOSD/OSDeploy/WinPE callback |
| Task-sequence step result | Authenticated step result bound to run, attempt, phase, and claimant |
| Derived health/readiness | Rebuildable projection from the facts above |

A successful HTTP response, UPID creation, process exit, Ansible exit code, callback, heartbeat, or screen image proves only its own fact. It does not by itself prove deployment completion.

Timeout produces `unknown`. Before retrying a mutation, the controller must obtain fresh evidence showing the postcondition is unsatisfied and that retry will preserve the intended resource identity.

Late success is journaled. It may satisfy the same current attempt only when its identity, payload digest, evidence revision, and decision fence still match. It cannot overwrite a newer contradictory decision or a different attempt without explicit reconciliation.

## 9. Recovery and mutation policy

The controller automatically retries read-only observations within bounded policy. Mutation retries require all of the following:

1. the same stable operation and run;
2. the same intended VMID, UUID, MAC, and semantic target where applicable;
3. a fresh authoritative observation showing the requested postcondition is absent;
4. no conflicting resource occupying the intended identity;
5. an operation-specific retry policy;
6. no more than two mutation attempts in v1 unless the operator explicitly approves another attempt.

Automatic deletion, replacement VM creation, storage destruction, rollback of an unrelated resource, or tenant mutation is prohibited. Compensating destructive actions require their own approval and journal entry.

Cancellation prevents new steps. An in-flight mutation remains `unknown` until observed. Archiving hides completed work from normal views but never deletes journal or evidence rows. V1 retains operation and event history indefinitely; artifact-byte retention remains governed by the existing artifact policy.

## 10. Scheduler and single-writer fencing

The current PostgreSQL job claim prevents two workers from claiming one row simultaneously but does not prevent Python and Rust from sharing authority. Before any Rust execution against a shared environment, both implementations must honor an additive authority record:

```text
orchestration_authority
  singleton_key
  executor_kind        # python or rust
  generation
  changed_at
  change_reference
```

Every claim, heartbeat, finalization, and continuation is bound to `executor_kind`, `generation`, `worker_id`, and `lease_token`. A stale generation cannot finalize work. Authority changes require enqueue freeze, active-work disposition, old-worker shutdown, heartbeat verification, and a new generation.

During local development the Rust controller uses a separate database and namespace. It must never claim production `jobs` rows.

## 11. Controller modes

- `observe`: normalize local compatibility fixtures into typed plans; cannot claim, write execution state, spawn processes, or mutate PVE.
- `adapter`: claim only explicitly allowlisted transitional operation kinds and validated parameter schemas.
- `native`: execute Rust-owned typed operations with journaled attempts and authoritative postconditions.

Mode is explicit configuration and appears in health output. Production binaries fail closed if the mode or authority generation is missing.

## 12. Transitional Ansible adapter

The adapter accepts a versioned operation schema, not arbitrary executable arguments. Each allowed entry defines:

- operation kind and contract version;
- exact executable and playbook identity;
- allowed parameters and types;
- allowed filesystem roots;
- required capabilities;
- secret-reference fields that may never be persisted as values;
- expected events and postconditions;
- cancellation and timeout behavior.

Generated Bash wrappers, unrestricted Python commands, unknown `-e` keys, shell interpolation, credential values in argv, and unapproved absolute paths fail before claim. The first executable contract is the synthetic `_test_long_sleep.yml` lifecycle harness.

Ansible exit status is evidence. The Rust operation controller decides whether the intended postcondition is satisfied.

## 13. API and agent compatibility

The baseline API, package, callback, and bearer behavior remains byte/shape compatible through `api-compat` and FastAPI while migration is underway. Security tightening that would break an existing client requires a new protocol version rather than an unannounced behavior change.

Old agents do not need to understand controller lease tokens. The compatibility adapter authenticates the agent and binds its callback server-side to the current run, operation, attempt, identity, and decision fence. A callback that cannot be bound fails closed and remains sanitized evidence.

Task-sequence timeouts and retry delays become controller-owned durable deadlines. Agents may report local timing, but they cannot extend or redefine the authoritative deadline.

## 14. Workflow completion semantics

CloudOSD and OSDeploy expose separate readiness milestones instead of one overloaded `complete` value.

- Legacy CloudOSD completion maps to the compatible OS-installed/agent-connected projection. It does not imply `verified_oobe`, enrollment, ESP, or usable endpoint.
- Legacy OSDeploy base completion may remain visible when QGA is unknown for compatibility, but Rust records `qga_unverified`; native `verified_operational` requires fresh host-side QGA evidence unless a versioned workflow policy explicitly waives it.
- OOBE, enrollment, ESP, kiosk readiness, domain-role readiness, and application acceptance remain independent gates.

## 15. Proxmox operation model

Typed operations replace playbook-wide replay:

1. preflight required node, storage, bridge, artifact, and template facts;
2. reserve/allocate VM identity;
3. create or clone and persist the UPID;
4. observe task completion and exact VM identity;
5. change configuration through typed diffs;
6. attach or detach media;
7. set boot order;
8. start, stop, or reset only under operation-specific policy;
9. observe QGA and guest readiness;
10. decide satisfaction, conflict, block, or recovery.

Root SSH remains a narrow capability adapter only for operations the PVE token cannot perform, such as required SMBIOS/QEMU argument handling. It must use an allowlisted command protocol, not a general shell. Large artifacts continue to use the existing PVE-pull design when controller upload is unsuitable.

## 16. macOS isolation and proof environment

Local development uses:

- Apple Silicon Rust and Cargo;
- a repository-local isolated worktree;
- local PostgreSQL 16 in Docker;
- separate database, schema, queue namespace, secrets, logs, output, and artifacts;
- an Axum/WireMock-compatible fake PVE service;
- contract-shaped fake WinPE, CloudOSD, OSDeploy, and agent clients;
- synthetic and manually sanitized fixtures;
- network denial for `192.168.2.4` and production PVE endpoints in automated tests;
- Graph/Entra adapters disabled by default.

Fixtures originate from checked-in schemas/tests and explicitly approved read-only observations. No production database dump, secrets directory, raw job payload, tenant material, or unsanitized VM inventory enters the repository. A fixture manifest records source contract version, sanitizer version, and content hash. CI fails when checked-in Python/OpenAPI/schema contracts drift without fixture regeneration.

## 17. Development and migration sequence

1. Freeze and inventory `v2026.09.2` contracts.
2. Create the Rust workspace and pure domain types.
3. Add journal, outbox, store, scheduler, leases, and projections.
4. Build fake PVE and callback systems with production-network denial.
5. Prove observation-only plan normalization.
6. Prove the synthetic allowlisted Ansible adapter locally.
7. Implement native PVE read/preflight operations.
8. Implement identity, clone/create, media, boot, power, and QGA operations.
9. Complete one native OSDeploy vertical slice through unchanged callback contracts.
10. Migrate agent/build-host coordination.
11. Migrate CloudOSD and legacy WinPE orchestration.
12. Add executor-generation fencing to both Python and Rust, validated locally.
13. Produce immutable macOS ARM64 and Linux AMD64 artifacts.
14. Complete local fault, differential, integration, and restore proof.
15. Re-plan deferred product tracks against the stable Rust contracts.
16. Seek separate authorization for a disposable non-production PVE proof.
17. Seek separate authorization for production cutover.
18. Retire Ansible operation families only after a rollback window.

## 18. Test and quality gates

Required local and CI evidence includes:

- `cargo fmt --check`;
- Clippy across all targets/features with warnings denied;
- unit tests for every transition and postcondition evaluator;
- property tests for transition invariants, idempotency, and canonical hashing;
- PostgreSQL 16 integration tests with two-connection races;
- authority-flip, stale-generation, lease-ABA, crash, cancellation, orphan, and restart tests;
- fake-PVE tests for 401, 404, 409, timeout, late UPID completion, stale reads, lock conflicts, VMID collision, and contradictory identity;
- callback duplicate, replay, late result, wrong identity, and wrong attempt tests;
- Python/Rust differential fixture tests;
- API and package-schema compatibility tests;
- process-group termination and log-stream compatibility tests;
- projection rebuild tests from an empty projection store;
- macOS ARM64 native execution;
- Linux AMD64 binary/container execution;
- dependency, license, and vulnerability policy checks;
- the existing Python, React, PowerShell, .NET, and Ansible contract suites;
- disposable multi-worker Compose tests;
- backup-and-restore rehearsal.

No gate may equate job completion with OOBE, enrollment, ESP, or usable-device acceptance.

## 19. Health and observability

Rust health reports:

- exact version and Git SHA;
- controller mode;
- executor kind and authority generation;
- database and outbox health;
- active leases and oldest pending operation age;
- reconciler progress and last successful sweep;
- adapter capability versions;
- fake versus real PVE transport;
- blocked, unknown, and conflicted counts.

Web schema readiness alone is not orchestration readiness. Production acceptance requires web, Rust controller, monitor, database, queue, callback, and end-to-end workflow evidence.

Logs and operator messages lead with impact, last confirmed progress, checks performed, current result, next action, and protections. Raw identifiers and evidence remain in sanitized details. Secrets never appear in argv, logs, journal payloads, health details, or fixtures.

## 20. Non-production proof gate

Use of any non-production Proxmox environment requires separate authorization. The exact immutable CI artifact is deployed into a controller stack isolated from production database, secrets, config, jobs, and artifacts.

Acceptance proceeds through read-only compatibility, synthetic execution, one reversible mutation, one sacrificial VM, one complete OSDeploy workflow, one complete CloudOSD workflow, and restart/recovery exercises. Failed proof VMs are preserved until evidence is reviewed.

## 21. Production no-touch and cutover gate

Before explicit cutover approval, prohibited actions against `192.168.2.4` include deployment, configuration changes, schema writes, queue claims, job actions, monitoring sweeps, approvals, settings writes, agent work, VM operations, and tenant/Graph calls.

Approved read-only research may inspect sanitized service health, settings metadata, jobs, run/readiness projections, VM/task evidence, and documentation. It must not retrieve secrets or persist raw production payloads.

Production cutover requires a separate user approval after all local and non-production gates pass. The cutover procedure must:

1. freeze enqueue and disposition every pending/running job;
2. capture and checksum PostgreSQL, configuration, secrets, artifacts, logs, Compose state, image SHA, replica count, and source bundle;
3. test restoration away from production;
4. deploy additive fencing/schema while Python remains authoritative;
5. stop Python builders and verify their heartbeats are absent;
6. change the authority generation to Rust;
7. start the exact tested Rust artifact;
8. verify single-writer claims, logs, callbacks, and health;
9. run a no-op canary, one reversible mutation, and one approved end-to-end deployment;
10. soak through restart and recovery cases before widening concurrency.

## 22. Rollback

Rollback is rehearsed before cutover:

1. freeze enqueue;
2. stop Rust;
3. mark in-flight mutations `unknown` and observe postconditions;
4. preserve Rust logs and journal;
5. retain the current database when additive schema remains Python-compatible;
6. capture a post-failure dump before any restore decision;
7. restore the previous Compose, image, configuration, and source bundle;
8. advance authority to a new Python generation;
9. verify only Python builders claim;
10. reconcile unknown operations individually.

A pre-cutover database restore is not automatic because it can erase legitimate post-cutover writes. It requires an explicit data-loss/reconciliation decision.

## 23. Ansible retirement criteria

Ansible remains available but disabled for one rollback window after the last native family migrates. It is removed only when:

- no producer enqueues `ansible-playbook`;
- no generated wrapper contains Ansible execution;
- CI statically enforces both conditions;
- all required job, run, callback, and evidence contracts have Rust coverage;
- no runtime image requires Ansible;
- a rollback drill to the last adapter-capable release succeeds;
- deployment, troubleshooting, recovery, and operator documentation is updated.

## 24. Completion criteria

The Rust-controller program is complete only when:

- Rust is the sole orchestration authority;
- every mutation is a typed operation with durable identity, attempts, evidence, and postconditions;
- ambiguous outcomes remain recoverable without blind replay;
- existing clients and operator surfaces retain their approved contracts;
- production cutover and rollback have both been exercised successfully;
- Ansible is absent from the production runtime or retained solely in an explicitly approved disabled rollback artifact;
- deferred product tracks have been re-planned against the Rust contracts.

