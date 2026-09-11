# Rust Controller Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Build and prove on macOS the Rust controller foundation through observation-only compatibility and one synthetic allowlisted Ansible adapter, without contacting or changing production.

**Architecture:** Add a Rust 2024 Cargo workspace under \`rust-controller/\`. Pure domain crates feed an append-only PostgreSQL journal and generation-fenced scheduler; fake PVE and fixture adapters provide deterministic local evidence. The executable supports \`observe\` and local-only \`adapter\` modes, while native Proxmox mutation and production cutover remain outside this plan.

**Tech Stack:** Rust 1.92, Cargo edition 2024, Tokio, Axum, Serde, SQLx with PostgreSQL 16, UUIDv7, SHA-256, tracing, wiremock, proptest, Docker, GitHub Actions.

**Spec:** \`docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md\`

## Global Constraints

- Baseline is \`v2026.09.2\` at \`c8ab4b2\`.
- Build and test on Apple Silicon macOS; also compile/test the Linux AMD64 target in CI.
- Automated Rust tests must deny connections to \`192.168.2.4\` and configured production PVE endpoints.
- Do not deploy to, configure, enqueue against, claim from, or mutate \`192.168.2.4\`.
- Production access is read-only and only for sanitized compatibility research.
- RustedOutClient, React changes, native PVE mutations, OSDeploy execution, CloudOSD execution, Graph, Entra, and tenant operations are out of scope.
- Do not consume arbitrary \`cmd_json\`; transitional execution is allowlisted by typed contract.
- Do not persist secrets, credential values, raw production job payloads, tenant identifiers, or VM inventory.
- A timeout is \`unknown\`; it is never automatic permission to retry a mutation.
- Every task follows RED-GREEN-REFACTOR, passes focused tests, and ends in one reviewable commit.

## Program Decomposition

This is release 1 of the approved Rust-controller program. Later independently reviewed plans cover native PVE operations, the OSDeploy vertical slice, CloudOSD/WinPE/agent migration, and production cutover/Ansible retirement. Those plans consume the interfaces defined here and cannot bypass this foundation's local proof gates.

## File Structure

\`\`\`text
rust-controller/
  Cargo.toml
  Cargo.lock
  deny.toml
  crates/
    controller-domain/src/{lib.rs,id.rs,state.rs,command.rs,evidence.rs,transition.rs}
    event-journal/src/{lib.rs,canonical.rs,event.rs}
    postgres-store/{migrations/0001_foundation.sql,src/lib.rs,src/store.rs,tests/postgres.rs}
    scheduler/src/{lib.rs,authority.rs,lease.rs}
    pve-port/src/{lib.rs,model.rs,fake.rs,observer.rs}
    ansible-adapter/src/{lib.rs,contract.rs,runner.rs}
    api-compat/src/{lib.rs,job.rs,plan.rs}
    controller-service/src/{main.rs,config.rs,health.rs,observe.rs}
  fixtures/{manifest.json,jobs/synthetic-long-sleep.json,pve/upid-complete.json}
  tests/network_deny.rs
  docker-compose.test.yml
  README.md
.github/workflows/rust-controller.yml
\`\`\`

---

### Task 1: Cargo Workspace and Fail-Closed Local Configuration

**Files:**
- Create: \`rust-controller/Cargo.toml\`
- Create: \`rust-controller/crates/controller-service/Cargo.toml\`
- Create: \`rust-controller/crates/controller-service/src/main.rs\`
- Create: \`rust-controller/crates/controller-service/src/config.rs\`
- Create: \`rust-controller/README.md\`
- Modify: \`.gitignore\`

**Interfaces:**
- Consumes: \`RUST_CONTROLLER_MODE\`, \`RUST_CONTROLLER_DATABASE_URL\`, \`RUST_CONTROLLER_PVE_BASE_URL\`, \`RUST_CONTROLLER_ALLOW_PRODUCTION_READS\`.
- Produces: \`ControllerMode\`, \`ControllerConfig::from_env()\`, and \`ControllerConfig::validate_network_boundary()\`.

- [ ] **Step 1: Write the failing configuration tests**

\`\`\`rust
#[test]
fn rejects_production_controller_in_adapter_mode() {
    let config = ControllerConfig {
        mode: ControllerMode::Adapter,
        database_url: "postgresql://localhost/rust_controller".into(),
        pve_base_url: "http://192.168.2.4:5000".into(),
        allow_production_reads: false,
    };
    assert_eq!(
        config.validate_network_boundary().unwrap_err().to_string(),
        "adapter mode cannot target production address 192.168.2.4"
    );
}

#[test]
fn observe_requires_explicit_production_read_permission() {
    let mut config = ControllerConfig::local_observe();
    config.pve_base_url = "http://192.168.2.4:5000".into();
    assert!(config.validate_network_boundary().is_err());
    config.allow_production_reads = true;
    assert!(config.validate_network_boundary().is_ok());
}
\`\`\`

- [ ] **Step 2: Create the workspace manifest and verify RED**

Use resolver 2, edition 2024, Rust 1.92, and workspace dependencies \`anyhow\`, \`async-trait\`, \`axum\`, \`chrono\`, \`clap\`, \`hex\`, \`proptest\`, \`reqwest\` with rustls, \`serde\`, \`serde_json\`, \`sha2\`, \`sqlx\` 0.8 with PostgreSQL/Tokio/rustls/UUID/chrono/json/migrate, \`thiserror\`, \`tokio\`, \`tower-http\`, \`tracing\`, \`tracing-subscriber\`, \`uuid\` with v7, and \`wiremock\`.

Run: \`cargo test --manifest-path rust-controller/Cargo.toml -p controller-service config::tests\`

Expected: FAIL because the configuration types are undefined.

- [ ] **Step 3: Implement fail-closed configuration**

\`\`\`rust
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ControllerMode { Observe, Adapter, Native }

impl ControllerConfig {
    pub fn validate_network_boundary(&self) -> anyhow::Result<()> {
        let production = self.pve_base_url.contains("192.168.2.4");
        if production && self.mode != ControllerMode::Observe {
            anyhow::bail!("{:?} mode cannot target production address 192.168.2.4", self.mode);
        }
        if production && !self.allow_production_reads {
            anyhow::bail!("production reads require explicit opt-in");
        }
        Ok(())
    }
}
\`\`\`

Parse only \`observe\`, \`adapter\`, and \`native\`. Missing mode, database URL, or PVE URL fails before the service binds.

- [ ] **Step 4: Add the minimal CLI and safety README**

Load and validate configuration, emit version/mode without secrets, and exit nonzero on a violation. Document local defaults, prohibited production modes, and that native mutation is unavailable.

- [ ] **Step 5: Run checks and commit**

\`\`\`bash
cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
cargo clippy --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
cargo test --manifest-path rust-controller/Cargo.toml --workspace
git add .gitignore rust-controller
git commit -m "feat: scaffold fail-closed Rust controller"
\`\`\`

---

### Task 2: Typed Domain Identity, State, and Idempotency

**Files:**
- Create: \`rust-controller/crates/controller-domain/Cargo.toml\`
- Create: \`rust-controller/crates/controller-domain/src/{lib.rs,id.rs,state.rs,command.rs,evidence.rs,transition.rs}\`

**Interfaces:**
- Consumes: UUIDv7 and canonical payload digests.
- Produces: \`RunId\`, \`OperationId\`, \`AttemptId\`, \`EventId\`, \`SemanticOperationKey\`, \`ExecutionState\`, \`ObservationHealth\`, \`ReadinessMilestone\`, \`CommandEnvelope\`, \`DomainSignal\`, and \`decide_transition()\`.

- [ ] **Step 1: Write failing separation tests**

\`\`\`rust
#[test]
fn timeout_is_unknown_not_failed() {
    let transition = decide_transition(ExecutionState::Running, DomainSignal::DeadlineElapsed).unwrap();
    assert_eq!(transition.next, ExecutionState::Unknown);
}

#[test]
fn readiness_does_not_mutate_execution_state() {
    let mut aggregate = OperationAggregate::running_fixture();
    aggregate.record_readiness(ReadinessMilestone::AgentConnected).unwrap();
    assert_eq!(aggregate.execution_state(), ExecutionState::Running);
}
\`\`\`

Run: \`cargo test --manifest-path rust-controller/Cargo.toml -p controller-domain\`

Expected: FAIL because the domain crate is absent.

- [ ] **Step 2: Implement typed IDs and semantic uniqueness**

\`\`\`rust
#[derive(Clone, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub struct SemanticOperationKey {
    pub workflow_kind: WorkflowKind,
    pub run_id: RunId,
    pub operation_key: String,
    pub contract_version: u16,
}
\`\`\`

Generate opaque IDs with \`Uuid::now_v7()\`; reject empty operation keys and contract version zero.

- [ ] **Step 3: Implement separate state enums**

Define the spec's execution, observation-health, and readiness variants with snake-case Serde names. Do not add catch-all string variants. Test every serialized value.

- [ ] **Step 4: Implement idempotency**

\`\`\`rust
pub enum IdempotencyDecision {
    AcceptNew,
    ReturnExisting(OperationId),
    Conflict { existing: OperationId },
}

pub fn check_idempotency(
    existing: Option<&PersistedCommand>,
    incoming: &CommandEnvelope,
) -> IdempotencyDecision;
\`\`\`

Same key/digest returns the existing operation; same key/different digest conflicts.

- [ ] **Step 5: Implement exhaustive transitions and properties**

Terminal states cannot regress. Deadline from running/waiting becomes unknown. Cancellation from running becomes cancelling. Property tests prove timeout never becomes failed and readiness never changes execution state.

- [ ] **Step 6: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p controller-domain
cargo clippy --manifest-path rust-controller/Cargo.toml -p controller-domain --all-targets -- -D warnings
git add rust-controller/crates/controller-domain rust-controller/Cargo.lock
git commit -m "feat: define Rust controller domain contracts"
\`\`\`

---

### Task 3: Canonical Event Journal Contracts

**Files:**
- Create: \`rust-controller/crates/event-journal/Cargo.toml\`
- Create: \`rust-controller/crates/event-journal/src/{lib.rs,canonical.rs,event.rs}\`

**Interfaces:**
- Consumes: controller-domain IDs and states.
- Produces: \`canonical_json_bytes()\`, \`payload_digest()\`, \`JournalEvent\`, \`EventKind\`, and \`AppendDecision\`.

- [ ] **Step 1: Write failing canonicalization tests**

\`\`\`rust
#[test]
fn object_key_order_does_not_change_digest() {
    let left = serde_json::json!({"b": 2, "a": 1});
    let right = serde_json::json!({"a": 1, "b": 2});
    assert_eq!(payload_digest(&left).unwrap(), payload_digest(&right).unwrap());
}

#[test]
fn arrays_preserve_order() {
    assert_ne!(
        payload_digest(&serde_json::json!([1, 2])).unwrap(),
        payload_digest(&serde_json::json!([2, 1])).unwrap()
    );
}
\`\`\`

Run: \`cargo test --manifest-path rust-controller/Cargo.toml -p event-journal\`

Expected: FAIL because canonicalization is absent.

- [ ] **Step 2: Implement canonical JSON and SHA-256**

Recursively sort object keys, preserve array order, serialize compact JSON, and return lowercase SHA-256 hex.

- [ ] **Step 3: Define validated append-only events**

\`\`\`rust
pub struct JournalEvent {
    pub event_id: EventId,
    pub operation_id: OperationId,
    pub attempt_id: Option<AttemptId>,
    pub aggregate_revision: i64,
    pub semantic_key: String,
    pub payload_digest: String,
    pub kind: EventKind,
    pub payload: serde_json::Value,
    pub observed_at: DateTime<Utc>,
}
\`\`\`

Reject empty semantic keys, revision below one, and payload/digest mismatch.

- [ ] **Step 4: Pin duplicate behavior**

Same semantic key/digest returns \`AlreadyPresent\`; same key/different digest returns \`Conflict\` without emitting an event.

- [ ] **Step 5: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p event-journal
cargo clippy --manifest-path rust-controller/Cargo.toml -p event-journal --all-targets -- -D warnings
git add rust-controller/crates/event-journal rust-controller/Cargo.lock
git commit -m "feat: add canonical operation journal contracts"
\`\`\`

---

### Task 4: PostgreSQL Journal, Outbox, and Projections

**Files:**
- Create: \`rust-controller/crates/postgres-store/Cargo.toml\`
- Create: \`rust-controller/crates/postgres-store/migrations/0001_foundation.sql\`
- Create: \`rust-controller/crates/postgres-store/src/{lib.rs,store.rs}\`
- Create: \`rust-controller/crates/postgres-store/tests/postgres.rs\`

**Interfaces:**
- Consumes: \`CommandEnvelope\`, \`JournalEvent\`, domain IDs and states.
- Produces: \`PgStore::migrate()\`, \`append_command()\`, \`append_event()\`, \`load_operation()\`, \`rebuild_projection()\`, and \`dequeue_outbox()\`.

- [ ] **Step 1: Write a failing migration test**

Start ephemeral \`postgres:16-alpine\` using the same Docker-port discovery pattern as \`autopilot-proxmox/tests/conftest.py\`. Assert schema \`rust_controller\` and tables \`commands\`, \`operations\`, \`attempts\`, \`journal_events\`, \`outbox\`, \`operation_projection\`, \`orchestration_authority\`, and \`worker_leases\`.

Expected: FAIL before the migration exists.

- [ ] **Step 2: Create constrained additive schema**

\`\`\`sql
CREATE SCHEMA IF NOT EXISTS rust_controller;
CREATE TABLE rust_controller.operations (
  operation_id uuid PRIMARY KEY,
  workflow_kind text NOT NULL,
  run_id uuid NOT NULL,
  operation_key text NOT NULL,
  contract_version smallint NOT NULL CHECK (contract_version > 0),
  state text NOT NULL CHECK (state IN ('pending','leased','running','waiting','cancelling','satisfied','failed','blocked','unknown','conflicted')),
  revision bigint NOT NULL CHECK (revision >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workflow_kind, run_id, operation_key, contract_version)
);
\`\`\`

Add constrained commands, attempts, events, outbox, projections, authority, and lease tables. Events are unique by \`(operation_id, semantic_key)\`; commands by idempotency key.

- [ ] **Step 3: Implement transactional append**

Lock the operation, require expected revision, insert event plus outbox, increment revision, and update projection in one transaction. Convert unique violations into already-present or digest conflict.

- [ ] **Step 4: Add two-connection tests**

Prove one append winner, revision conflict for the loser, outbox rollback atomicity, duplicate idempotency, conflicting duplicate rejection, and projection rebuild equality.

- [ ] **Step 5: Prove PostgreSQL clock authority**

Lease and deadline timestamps come from \`clock_timestamp()\`; Rust wall-clock time cannot authorize a database lease.

- [ ] **Step 6: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p postgres-store --test postgres -- --test-threads=1
cargo clippy --manifest-path rust-controller/Cargo.toml -p postgres-store --all-targets -- -D warnings
git add rust-controller/crates/postgres-store rust-controller/Cargo.lock
git commit -m "feat: persist Rust operation journal in Postgres"
\`\`\`

---

### Task 5: Generation-Fenced Scheduler and Leases

**Files:**
- Create: \`rust-controller/crates/scheduler/Cargo.toml\`
- Create: \`rust-controller/crates/scheduler/src/{lib.rs,authority.rs,lease.rs}\`
- Modify: \`rust-controller/crates/postgres-store/src/store.rs\`
- Modify: \`rust-controller/crates/postgres-store/tests/postgres.rs\`

**Interfaces:**
- Consumes: \`PgStore\`, operations, authority and lease tables.
- Produces: \`AuthoritySnapshot\`, \`LeaseGrant\`, \`Scheduler::claim_next()\`, \`heartbeat()\`, \`request_cancel()\`, \`finalize()\`, and \`reap_expired()\`.

- [ ] **Step 1: Write failing stale-authority tests**

\`\`\`rust
#[tokio::test]
async fn stale_generation_cannot_finalize() {
    let first = fixture.claim_as("rust", 7).await.unwrap();
    fixture.advance_authority("python", 8).await.unwrap();
    let error = fixture.scheduler.finalize(&first, ExecutionState::Satisfied).await.unwrap_err();
    assert!(matches!(error, SchedulerError::StaleAuthority { expected: 8, actual: 7 }));
}
\`\`\`

Add worker mismatch, stale token, cap, skip-locked, cancellation, and expiry cases.

- [ ] **Step 2: Implement uncached authority checks**

Load the singleton authority inside every claim, heartbeat, finalization, and continuation transaction. Missing authority fails closed.

- [ ] **Step 3: Implement per-acquisition leases**

Create a random token per acquisition. Bind operation, attempt, worker, executor kind, generation, and PostgreSQL expiry. Claim with \`FOR UPDATE SKIP LOCKED\` after enforcing the per-kind cap.

- [ ] **Step 4: Implement cancellation and reaping**

Running cancellation becomes cancelling. Expired running work becomes unknown. Expired leased-but-not-started work returns to pending only when no mutation-start event exists.

- [ ] **Step 5: Run adversarial concurrency tests**

Ten concurrent claimers must respect one-row ownership and caps; authority flip immediately fences stale heartbeat/finalize; reacquisition receives a different token.

- [ ] **Step 6: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p scheduler -- --test-threads=1
cargo clippy --manifest-path rust-controller/Cargo.toml -p scheduler --all-targets -- -D warnings
git add rust-controller/crates/scheduler rust-controller/crates/postgres-store rust-controller/Cargo.lock
git commit -m "feat: fence Rust scheduler authority and leases"
\`\`\`

---

### Task 6: Typed PVE Read Port, Fake Server, and Evidence

**Files:**
- Create: \`rust-controller/crates/pve-port/Cargo.toml\`
- Create: \`rust-controller/crates/pve-port/src/{lib.rs,model.rs,fake.rs,observer.rs}\`
- Create: \`rust-controller/fixtures/pve/upid-complete.json\`
- Create: \`rust-controller/tests/network_deny.rs\`

**Interfaces:**
- Consumes: validated base URL and typed VM/UPID identifiers.
- Produces: \`PveReadPort\`, \`PveEvidence\`, \`EvidenceSource\`, \`FakePve\`, and \`ReqwestPveObserver\`.

- [ ] **Step 1: Write failing evidence tests**

\`\`\`rust
#[tokio::test]
async fn upid_success_is_not_vm_postcondition() {
    let fake = FakePve::new().with_upid_complete("UPID:test");
    let evidence = observe_clone_outcome(&fake, fixture_intent()).await.unwrap();
    assert_eq!(evidence.task_complete, Some(true));
    assert_eq!(evidence.vm_identity_satisfied, None);
    assert_eq!(evidence.health, ObservationHealth::Unavailable);
}
\`\`\`

Add 401, 404, 409, timeout, stale read, contradictory UUID/MAC, and late completion cases.

- [ ] **Step 2: Define read-only port**

\`\`\`rust
#[async_trait]
pub trait PveReadPort: Send + Sync {
    async fn vm_config(&self, node: &NodeName, vmid: Vmid) -> Result<VmConfig, PveReadError>;
    async fn task_status(&self, node: &NodeName, upid: &Upid) -> Result<TaskStatus, PveReadError>;
    async fn storage_content(&self, node: &NodeName, storage: &StorageName) -> Result<Vec<Volume>, PveReadError>;
    async fn qga_ping(&self, node: &NodeName, vmid: Vmid) -> Result<QgaStatus, PveReadError>;
}
\`\`\`

No mutation trait exists in this plan.

- [ ] **Step 3: Implement deterministic fake and HTTP observer**

Queue fake responses by typed method/path and record requests. Use reqwest with rustls and certificate verification enabled. Map timeout to timed-out, 401/403 to unauthorized, old observations to stale, and identity mismatch to contradicted.

- [ ] **Step 4: Enforce the network boundary**

Reject production addresses before creating the HTTP client unless mode is observe with explicit read permission. The automated network-deny test must leave the request recorder empty.

- [ ] **Step 5: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p pve-port
cargo test --manifest-path rust-controller/Cargo.toml --test network_deny
cargo clippy --manifest-path rust-controller/Cargo.toml -p pve-port --all-targets -- -D warnings
git add rust-controller/crates/pve-port rust-controller/fixtures/pve rust-controller/tests rust-controller/Cargo.lock
git commit -m "feat: observe typed Proxmox evidence locally"
\`\`\`

---

### Task 7: Sanitized Fixtures and Observation-Only Plans

**Files:**
- Create: \`rust-controller/crates/api-compat/Cargo.toml\`
- Create: \`rust-controller/crates/api-compat/src/{lib.rs,job.rs,plan.rs}\`
- Create: \`rust-controller/fixtures/manifest.json\`
- Create: \`rust-controller/fixtures/jobs/synthetic-long-sleep.json\`
- Create: \`rust-controller/crates/controller-service/src/observe.rs\`

**Interfaces:**
- Consumes: sanitized \`JobEnvelope\`, adapter registry, canonical hashing.
- Produces: \`NormalizedPlan\`, \`PlanFingerprint\`, \`normalize_job()\`, and observe-mode output.

- [ ] **Step 1: Write failing sanitizer tests**

Reject key/value patterns for token, password, secret, bearer, private keys, tenant/application UUID fields, and non-loopback IP addresses.

\`\`\`rust
#[test]
fn fixture_rejects_secret_shaped_values() {
    let fixture = JobEnvelope::synthetic_long_sleep();
    assert!(fixture.validate_sanitized().is_ok());
    let unsafe_fixture = fixture.with_arg("token", "PVEAPIToken=user!id=secret");
    assert!(unsafe_fixture.validate_sanitized().is_err());
}
\`\`\`

- [ ] **Step 2: Define normalized plans**

\`\`\`rust
pub struct NormalizedPlan {
    pub job_id: String,
    pub operation_kind: OperationKind,
    pub contract_version: u16,
    pub adapter_identity: String,
    pub parameters: BTreeMap<String, SanitizedValue>,
    pub required_capabilities: BTreeSet<String>,
    pub expected_events: Vec<String>,
    pub postconditions: Vec<String>,
}
\`\`\`

Fingerprint canonical JSON with SHA-256.

- [ ] **Step 3: Implement fail-closed normalization**

Recognize only \`synthetic_long_sleep\`. Reject unknown job type, executable, playbook, argument, duplicate key, path traversal, or unsafe value before an executable plan exists.

- [ ] **Step 4: Prove observe-mode zero writes**

Use a SELECT-only PostgreSQL role and statement audit. Assert no insert/update/delete/DDL/advisory lock/process spawn. Output only compatibility result and redacted fingerprint.

- [ ] **Step 5: Add fixture manifest**

Record fixture name, baseline \`c8ab4b2\`, contract version, sanitizer version 1, and SHA-256. Fixture contents are synthetic.

- [ ] **Step 6: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p api-compat
cargo test --manifest-path rust-controller/Cargo.toml -p controller-service observe
cargo clippy --manifest-path rust-controller/Cargo.toml --workspace --all-targets -- -D warnings
git add rust-controller/crates/api-compat rust-controller/crates/controller-service/src/observe.rs rust-controller/fixtures rust-controller/Cargo.lock
git commit -m "feat: normalize legacy jobs without execution"
\`\`\`

---

### Task 8: Synthetic Allowlisted Ansible Adapter

**Files:**
- Create: \`rust-controller/crates/ansible-adapter/Cargo.toml\`
- Create: \`rust-controller/crates/ansible-adapter/src/{lib.rs,contract.rs,runner.rs}\`
- Verify: \`autopilot-proxmox/playbooks/_test_long_sleep.yml\`

**Interfaces:**
- Consumes: \`NormalizedPlan\`, \`LeaseGrant\`, explicit registry.
- Produces: \`AdapterContract\`, \`ValidatedInvocation\`, \`AdapterRunner::run()\`, and \`AdapterEvent\`.

- [ ] **Step 1: Write failing allowlist tests**

Reject any executable except resolved \`ansible-playbook\`, any other playbook, unknown \`-e\` key, shell metacharacter, generated script, path outside the worktree, unapproved environment variable, or credential-shaped argument.

- [ ] **Step 2: Define the synthetic contract**

\`\`\`rust
pub const SYNTHETIC_LONG_SLEEP_V1: AdapterContract = AdapterContract {
    operation_kind: OperationKind::SyntheticLongSleep,
    contract_version: 1,
    playbook_relative_path: "autopilot-proxmox/playbooks/_test_long_sleep.yml",
    allowed_extra_vars: &["sleep_seconds"],
    timeout_seconds: 30,
};
\`\`\`

Validate \`sleep_seconds\` as an integer from 0 through 20 and canonicalize the worktree path.

- [ ] **Step 3: Implement safe process lifecycle**

Spawn without a shell, create a process group, close stdin, isolate temp paths, and stream combined output through a bounded line channel. Cancellation signals the group, waits two seconds, then force-kills it. Persist only sanitized lines.

- [ ] **Step 4: Bind scheduler authority**

Verify executor, generation, worker, and lease before spawn and every heartbeat/finalization. Lost authority terminates the group and records unknown.

- [ ] **Step 5: Add lifecycle tests**

Prove success, nonzero exit, five-second heartbeats with paused Tokio time, cancellation within ten seconds, timeout to unknown, stale-generation termination, child cleanup, bounded logs, and zero secret persistence.

- [ ] **Step 6: Run checks and commit**

\`\`\`bash
cargo test --manifest-path rust-controller/Cargo.toml -p ansible-adapter
cargo clippy --manifest-path rust-controller/Cargo.toml -p ansible-adapter --all-targets -- -D warnings
git add rust-controller/crates/ansible-adapter rust-controller/Cargo.lock
git commit -m "feat: run one allowlisted Ansible adapter"
\`\`\`

---

### Task 9: Health, Local Integration, and CI

**Files:**
- Create: \`rust-controller/crates/controller-service/src/health.rs\`
- Modify: \`rust-controller/crates/controller-service/src/main.rs\`
- Create: \`rust-controller/docker-compose.test.yml\`
- Create: \`rust-controller/deny.toml\`
- Create: \`.github/workflows/rust-controller.yml\`
- Modify: \`rust-controller/README.md\`

**Interfaces:**
- Consumes: scheduler, store, adapter registry, configuration, build metadata.
- Produces: \`GET /healthz\`, \`GET /readyz\`, local Compose proof, and CI.

- [ ] **Step 1: Write failing health tests**

\`\`\`rust
#[tokio::test]
async fn readiness_reports_executor_and_transport() {
    let response = test_app(fixture_health()).oneshot(request("/readyz")).await.unwrap();
    let body: ReadyResponse = json_body(response).await;
    assert_eq!(body.mode, "observe");
    assert_eq!(body.executor_kind, "rust");
    assert_eq!(body.pve_transport, "fake");
    assert_eq!(body.authority_generation, 1);
    assert!(body.database);
    assert!(body.outbox);
}
\`\`\`

Database failure, missing authority, stale reconciler, real transport in test profile, or excessive outbox backlog makes readyz non-200 while healthz remains process liveness.

- [ ] **Step 2: Implement sanitized structured health**

Include version, Git SHA, mode, executor/generation, database/outbox state, lease counts, oldest pending age, reconciliation time, adapter versions, transport kind, and blocked/unknown/conflicted counts. Exclude URLs, DSNs, tokens, VM IDs, and payloads.

- [ ] **Step 3: Add isolated three-worker Compose proof**

Use PostgreSQL 16, fake PVE, loopback-only ports, and isolated names. Prove one claim winner, cap enforcement, cancellation, stale lease recovery, and stable plan fingerprints.

- [ ] **Step 4: Add dependency and CI policy**

Run fmt, Clippy with warnings denied, all tests, cargo-deny, Compose integration, macOS ARM64 tests, and Linux AMD64 release build. Do not publish an image.

- [ ] **Step 5: Verify locally**

\`\`\`bash
cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
cargo clippy --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
cargo test --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast
docker compose -f rust-controller/docker-compose.test.yml config
git diff --check
\`\`\`

Expected: PASS and no production address outside rejection tests/documentation.

- [ ] **Step 6: Record exact completion evidence**

Update README with local observe, synthetic adapter, health, PostgreSQL, and network-deny commands. State that this release has no native PVE mutation and is not production-ready.

- [ ] **Step 7: Commit**

\`\`\`bash
git add .github/workflows/rust-controller.yml rust-controller
git commit -m "ci: prove Rust controller foundation locally"
\`\`\`

---

## Foundation Acceptance Gate

Before the native-PVE plan:

- every Rust test passes;
- formatting and Clippy are clean;
- PostgreSQL concurrency/crash tests pass repeatedly;
- observe mode performs zero writes and spawns no process;
- adapter mode accepts only the synthetic contract;
- three local workers cannot double-claim or exceed caps;
- authority flips fence stale finalization;
- timeout and lost authority become unknown;
- fake PVE evidence remains distinct from execution/readiness;
- fixtures are synthetic and sanitizer-validated;
- automated tests cannot connect to \`192.168.2.4\`;
- Linux AMD64 artifact builds;
- no image is published and no production system is changed.

The native-PVE plan begins only after this gate and review of the resulting Rust interfaces.

