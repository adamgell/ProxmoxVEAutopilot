# Durable OSDeploy Activation and Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement durable OSDeploy storage, the real first-three-stage FakePVE activation/dispatch path, bounded recovery and existing-controller send ownership as an intermediate milestone toward the full sixteen-stage service workflow.

**Architecture:** Extend the existing `PgStore` and store-backed `Scheduler` with OSDeploy-private records and proof-checked transactions. Reuse the completed provisioning evaluator, requests, evidence and concrete `NativeFakePve`; keep observation outside database locks and send authority in a consuming permit. Integrate a sibling OSDeploy path in `operation-controller`, with closed-on-start cooperative send admission and owned futures that cannot escape drain accounting.

**Tech Stack:** Rust 2024, Rust minimum 1.92, existing Tokio/SQLx PostgreSQL/chrono/serde/serde_json/uuid/event-journal/pve-port/osdeploy-adapter dependencies. Additive SQL migration 0005; no new registry package, production transport or daemon.

**Spec:** Read the complete committed specification under `docs/superpowers/specs/2026-09-05-rust-osdeploy-durability/` in this precedence order: `next-durable-planning-decisions.md`, `next-durable-schema-proposal.md` (including its send/capture amendment), `next-durable-main-decisions.md`, and `next-durable-transaction-boundaries.md`. Read `next-durable-gap-audit.md` as rationale, not a policy override.

## Global Constraints

- Main-reviewed implementation plan. Code proof remains `f412f2c9d577081985ce503773dfa8226567f099`; main reported the Linux artifact checkpoint closed and subsequent evidence-only commits `4db345e` then `1fb97f7`. Dispatch baseline is the full exact current HEAD resolved by main at dispatch (expected `1fb97f7`), not the older source-only SHA. Main owns task dispatch and acceptance. No command below has been executed by this planning task.
- User already selected Astra subagent execution. Main dispatches one source owner at a time and independent spec/code reviews after each task; when new-agent allocation is refused, apply the explicit existing-uninvolved-Astra fallback in the planning decisions' Task8 dispatch clarifications. Do not claim a reused agent is newly spawned or empty-context. This draft does not ask an execution-mode question or spawn anyone.
- Workspace: `/Users/Adam.Gell/repo/ProxmoxVEAutopilot/.worktrees/codex-rust-controller-design`; code commands run in its `rust-controller` directory. Verify the dispatch-time HEAD and dirty state before editing; preserve unrelated work and all existing assertions.
- “The first enabled execution stages are exactly Clone, DiskCapacity, ConfigurePe.” Preserve all sixteen `OsDeployStage::ALL` entries and seven PVE mappings. Fourth/later execution entry returns `CapabilityUnavailable` before execution writes; declaration reads and all-stage cancellation are not stage execution admission.
- “Historical generic OSDeploy rows are readable history, not adoptable execution records.” Preserve registration errors, generic guards, `NativeController`, `NativeDispatchPermit`, `ProvisioningFakePort`, `DomainSignal` and existing native/generic semantics.
- “No execution table, row, or event is created by `enqueue_osdeploy`.” Migration creates tables; declaration registration creates no execution rows. Preserve immutable shared VM and global agent reservations.
- “Initial lease interval is 30 seconds, capped at the original scope; no zero-length lease.” “Heartbeat is requested at most every 10 seconds.” One logical attempt, number 1, original activation/deadline, no automatic resend or replacement attempt.
- Fixed policy: one-second sweeps; at most 32 candidates per batch; Waiting two seconds; Unknown five seconds; unavailable delays 2/4/8/10 seconds, count capped at four. Every nonnull next-check is capped at the original deadline. Missing-task-receipt Unknown has null next-check, never guessed task lookup.
- Existing read/collection/call ceilings are 2/6/24 seconds, additionally capped by remaining original lease/scope time. No clock/budget reset during heartbeat, restart, callback, reclaim or receipt capture. DB time at/after scope deadline cannot create new success.
- New authority-bearing transactions take authority FOR SHARE → run → optional family cap → all sixteen operations sorted UUID → affected attempts/leases in that order → projections. No network/host observation under locks. Receipt capture takes shared authority without current-generation ownership, then run/all operations; it grants no continuation.
- Initial table/decision JSON object limit 65536 octets; request/evidence limit 1048576 octets. Exact closed variants, required nulls, duplicate rejection and canonical/hash/journal equality; no raw-token/error-body payloads or public ownership deserialization.
- “The consuming method moves the permit into its returned future when called, including before that future is first polled.” No Clone/Copy/serde/request getter/conversion on permit or capture. No restore-permit API. Original capture survives only in its original owner and may retry persistence of that same response.
- StartPe/session arming, callback processing, truthful atomic grace activation, guarded stop, disk boot, guest actions, persistent heartbeat/QGA, full sixteen-stage service integration and independent-process recovery/readiness remain required subsequent milestones. No callback bypass or synthetic successful predecessor may enable them in this phase.
- Production and real PVE remain read-only; no deployment/publication/cutover, credentials, network, pulls/prunes or new runtime artifacts from this draft. Final main-owned Linux verification requires its separately approved owned-artifact workflow, not rerunning the frozen candidate scripts against changed source.

## File map and dependency boundaries

All paths below are relative to `rust-controller/` unless prefixed `docs/`. Keep responsibilities separate; no wholesale native/store reorganization.

| Files | Responsibility / first owner |
| --- | --- |
| `crates/postgres-store/migrations/0005_osdeploy_durability.sql`; `src/store.rs` | Nine-table migration and explicit migrate hook / Task 1 |
| `crates/postgres-store/src/osdeploy/execution.rs`, `execution/wire.rs`, `execution/load.rs`, `execution/history.rs` | Closed value API, strict wire and execution reload, historical physical proof / Tasks 2, 4 |
| `crates/postgres-store/src/osdeploy.rs`, `src/lib.rs` | Minimal module/export declarations / owning task |
| `crates/postgres-store/src/scheduler/osdeploy.rs`, `osdeploy/transaction.rs`, `osdeploy/lifecycle.rs`, `osdeploy/transition.rs` | Locked load, atomic append and proof-bearing lifecycle / Task 3 |
| `crates/postgres-store/src/scheduler/osdeploy/pve.rs`; `osdeploy/receipt.rs` | Evidence/context/request advice, committed dispatch and original capture / Tasks 4–5 |
| `crates/postgres-store/src/scheduler/osdeploy/decision.rs`, `osdeploy/recovery.rs`, `osdeploy/discovery.rs` | Evaluation/parking, reconciliation/cancellation/reaping, bounded scan/repair / Tasks 6–7 |
| `crates/postgres-store/src/scheduler.rs` | One module declaration and narrow shared append extraction only; old policy validation stays unchanged / Task 3 |
| `crates/postgres-store/tests/osdeploy_durability.rs`; `tests/osdeploy_execution_support/mod.rs` | New behavioral harness and test-only physical world/SQL assertions / Tasks 1–7 |
| `crates/postgres-store/tests/osdeploy_support/mod.rs` | Only add nine new tables to owned snapshots/corruption allowlist; preserve existing assertions / Task 1 |
| `crates/operation-controller/src/osdeploy.rs`, `osdeploy/collect.rs`, `osdeploy/send.rs`, `osdeploy/admission.rs`; `src/lib.rs` | Bounded sibling controller, observation, consuming send and admission / Task 8 |
| `crates/operation-controller/tests/postgres_osdeploy.rs`; `Cargo.toml` | Real family integration; add existing `osdeploy-adapter` as a dev-only path dependency for registration fixture / Task 8 |
| `evidence/poc-readiness-tracker.md` | Exact milestone/evidence and remaining full-goal gates / Task 9 |

Do not change Cargo.lock or registry dependency versions except Task8's explicitly selected single existing-package dependency edge: add osdeploy-adapter to operation-controller's lock dependency list alongside the required dev-only local path dependency. No new package/version/checksum/source/feature or network resolution. `operation-controller` already depends on Tokio, store and pve-port; postgres-store already depends on pve-port and needs no production Tokio dependency. `crates/scheduler/src/lib.rs` already re-exports the real Scheduler, so inherent new methods require no facade implementation.

## Cross-task interface ledger

These signatures are pinned for this draft. All named new public values have private fields, no public constructors/Deserialize and only the listed getters. Errors are payload-free. `OsDeployProgress` is the public observation result enum, not a caller-supplied state input. Use current imported `controller_domain::{RunId,OperationId,AttemptId,EventId,ExecutionState}`, `chrono::{DateTime,Utc}`, existing pve-port values and existing `LeaseGrant`.

```rust
// Task 2, exported from postgres-store::lib.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum OsDeployExecutionError {
    #[error("osdeploy_execution_validation_failed")] Validation,
    #[error("osdeploy_execution_conflict")] Conflict,
    #[error("osdeploy_execution_fence_lost")] FenceLost,
    #[error("osdeploy_execution_capability_unavailable")] CapabilityUnavailable,
    #[error("osdeploy_execution_storage_unavailable")] StorageUnavailable,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OsDeployProgress { Idle, Waiting, Decided(ExecutionState) }
pub struct OsDeployOperationSnapshot {
    operation_id: OperationId, run_id: RunId, revision: i64, state: ExecutionState,
    cancelled: bool, plan: OsDeployOperationPlanV1, attempt_id: Option<AttemptId>,
    activated_at: Option<DateTime<Utc>>, deadline_at: Option<DateTime<Utc>>,
    next_check_at: Option<DateTime<Utc>>, dispatch: Option<ProvisioningDispatchV1>,
    receipt: Option<ProvisioningReceiptV1>,
}
impl OsDeployOperationSnapshot {
    pub fn operation_id(&self) -> OperationId;
    pub fn run_id(&self) -> RunId;
    pub fn revision(&self) -> i64;
    pub fn state(&self) -> ExecutionState;
    pub fn cancelled(&self) -> bool;
    pub fn plan(&self) -> &OsDeployOperationPlanV1;
    pub fn attempt_id(&self) -> Option<AttemptId>;
    pub fn activated_at(&self) -> Option<DateTime<Utc>>;
    pub fn deadline_at(&self) -> Option<DateTime<Utc>>;
    pub fn next_check_at(&self) -> Option<DateTime<Utc>>;
    pub fn dispatch(&self) -> Option<&ProvisioningDispatchV1>;
    pub fn receipt(&self) -> Option<&ProvisioningReceiptV1>;
}
impl PgStore {
    pub async fn load_osdeploy_operation(&self, operation: OperationId)
        -> Result<OsDeployOperationSnapshot, OsDeployExecutionError>;
}

// Task 3: a fresh, non-authorizing timing/CAS observation with private fields.
pub struct OsDeployLeaseStatus {
    grant: LeaseGrant, revision: i64, checked_at: DateTime<Utc>,
}
impl OsDeployLeaseStatus {
    pub fn grant(&self) -> &LeaseGrant;
    pub fn revision(&self) -> i64;
    pub fn checked_at(&self) -> DateTime<Utc>;
    pub fn remaining(&self) -> std::time::Duration; // min(lease,scope)-checked_at, saturating
}
impl Scheduler {
    pub async fn osdeploy_authority_snapshot(&self)
        -> Result<AuthoritySnapshot, OsDeployExecutionError>;
    pub async fn claim_osdeploy_bound(&self, operation: OperationId,
        workflow_sha256: &str, cap: u32)
        -> Result<Option<LeaseGrant>, OsDeployExecutionError>;
    pub async fn start_osdeploy_bound(&self, grant: &LeaseGrant, workflow_sha256: &str)
        -> Result<ExecutionState, OsDeployExecutionError>;
    pub async fn heartbeat_osdeploy_bound(&self, grant: &LeaseGrant, workflow_sha256: &str)
        -> Result<OsDeployLeaseStatus, OsDeployExecutionError>;
    pub async fn continuation_osdeploy_bound(&self, grant: &LeaseGrant, workflow_sha256: &str)
        -> Result<OsDeployLeaseStatus, OsDeployExecutionError>;
}

// Task 4: read-only advice; locked dispatch/evaluation reconstructs it again.
impl PgStore {
    pub async fn load_osdeploy_pve_context(&self, operation: OperationId,
        expected_revision: i64, mode: ProvisioningEvaluationModeV1)
        -> Result<ProvisioningEvaluationContextV1, OsDeployExecutionError>;
    pub async fn record_osdeploy_pve_evidence(&self, operation: OperationId,
        attempt: AttemptId, expected_revision: i64, evidence: &ProvisioningEvidenceV1)
        -> Result<EventId, OsDeployExecutionError>;
    pub async fn prepare_osdeploy_pve_request(&self, operation: OperationId,
        expected_revision: i64, preflight_event: EventId)
        -> Result<ProvisioningMutationRequestV1, OsDeployExecutionError>;
}

// Task 5: exact selected consuming-send/capture boundary.
pub struct OsDeployDispatchPermit { dispatch: ProvisioningDispatchV1 }
pub struct OsDeployResponseCapture { identity: OriginalOsDeployDispatchIdentity }
struct OriginalOsDeployDispatchIdentity {
    run_id: RunId, operation_id: OperationId, attempt_id: AttemptId,
    source: NativeEvidenceSource, workflow_sha256: String, pve_plan_sha256: String,
    request_sha256: String, dispatch_event_id: EventId, dispatch_revision: i64,
    original_generation: i64, preflight_event_id: EventId, dispatched_at: DateTime<Utc>,
}
impl OsDeployDispatchPermit {
    pub async fn submit_fake_once(self, fake: &NativeFakePve)
        -> Result<MutationReceipt, PveWriteError>;
}
impl Scheduler {
    pub async fn begin_osdeploy_pve_dispatch(&self, grant: &LeaseGrant,
        expected_revision: i64, preflight_event: EventId,
        request: &ProvisioningMutationRequestV1)
        -> Result<(OsDeployDispatchPermit, OsDeployResponseCapture), OsDeployExecutionError>;
    pub async fn record_osdeploy_pve_receipt(&self, capture: &OsDeployResponseCapture,
        receipt: &MutationReceipt) -> Result<(), OsDeployExecutionError>;
}

// Tasks 6–7. No target-state parameter in any public operation.
impl Scheduler {
    // Task 6 lands this together with actual durable parking; no stub in Task 3.
    pub async fn resume_osdeploy_bound(&self, operation: OperationId,
        attempt: AttemptId, expected_revision: i64, workflow_sha256: &str, cap: u32)
        -> Result<Option<LeaseGrant>, OsDeployExecutionError>;
    pub async fn decide_osdeploy_pve(&self, grant: &LeaseGrant,
        expected_revision: i64, evidence_event: EventId)
        -> Result<OsDeployProgress, OsDeployExecutionError>;
    pub async fn reconcile_osdeploy_unknown(&self, operation: OperationId,
        original_attempt: AttemptId, expected_revision: i64,
        evidence_event: EventId, workflow_sha256: &str)
        -> Result<OsDeployProgress, OsDeployExecutionError>;
    pub async fn cancel_osdeploy_run(&self, run: RunId) -> Result<(), OsDeployExecutionError>;
    pub async fn reap_osdeploy_expired(&self) -> Result<OsDeployMaintenanceSummary, OsDeployExecutionError>;
    pub async fn repair_osdeploy_schedules(&self, cursor: &mut OsDeployRepairCursor)
        -> Result<OsDeployMaintenanceSummary, OsDeployExecutionError>;
    pub async fn expire_osdeploy_scopes(&self, cursor: &mut OsDeployExpiryCursor)
        -> Result<OsDeployMaintenanceSummary, OsDeployExecutionError>;
    pub async fn discover_osdeploy_due(&self) -> Result<Vec<OsDeployDue>, OsDeployExecutionError>;
}
pub struct OsDeployRepairCursor { after: Option<OperationId> }
pub struct OsDeployExpiryCursor {
    after: Option<(DateTime<Utc>, RunId, String, OperationId)>,
}
// Only Default is public; no setter/Deserialize. Cursor contents are hints.
pub struct OsDeployMaintenanceSummary { examined: u32, changed: u32, rejected: u32 }
impl OsDeployMaintenanceSummary {
    pub fn examined(&self) -> u32;
    pub fn changed(&self) -> u32;
    pub fn rejected(&self) -> u32;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OsDeployDueKind { Waiting, UnknownReconciliation }
pub struct OsDeployDue {
    operation: OperationId, attempt: AttemptId, revision: i64,
    workflow_sha256: String, basis_event: EventId, kind: OsDeployDueKind,
}
impl OsDeployDue {
    pub fn operation_id(&self) -> OperationId;
    pub fn attempt_id(&self) -> AttemptId;
    pub fn revision(&self) -> i64;
    pub fn workflow_sha256(&self) -> &str;
    pub fn basis_event_id(&self) -> EventId;
    pub fn kind(&self) -> OsDeployDueKind;
}

// Task 8, operation-controller exports, gate type remains crate-private.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum OsDeployControllerError {
    #[error("osdeploy_storage_unavailable")] Storage,
    #[error("osdeploy_validation_failed")] Validation,
    #[error("osdeploy_conflict")] Conflict,
    #[error("osdeploy_fence_lost")] FenceLost,
    #[error("osdeploy_capability_unavailable")] CapabilityUnavailable,
    #[error("osdeploy_call_timed_out")] TimedOut,
    #[error("osdeploy_admission_closed")] AdmissionClosed,
}
pub struct OsDeployController {
    store: PgStore, scheduler: Scheduler, fake: std::sync::Arc<NativeFakePve>,
    admission: OsDeploySendAdmission, workers: tokio::sync::Semaphore, cap: u32,
}
impl OsDeployController {
    pub fn new(store: PgStore, scheduler: Scheduler,
        fake: std::sync::Arc<NativeFakePve>, cap: u32) -> Result<Self, OsDeployControllerError>;
    pub async fn open_send_admission(&self) -> Result<(), OsDeployControllerError>;
    pub async fn close_and_drain(&self, bound: std::time::Duration)
        -> Result<(), OsDeployControllerError>;
    pub async fn run_osdeploy_once(&self, operation: OperationId)
        -> Result<OsDeployProgress, OsDeployControllerError>;
    pub async fn run_due_once(&self, due: &OsDeployDue)
        -> Result<OsDeployProgress, OsDeployControllerError>;
}
```

The snapshot's private stored fields are exactly those above. Execution reload may keep additional private internal history separately, not public authenticated constructors. The original capture identity matches the accepted amendment, with no request payload/token.

`OsDeployLeaseStatus::remaining()` is a static duration at DB checked-at, **not a reusable timeout**. Every consumer records `Instant::now()` before starting the DB check, subtracts elapsed monotonic time from that returned interval, and pins one local absolute deadline once. It never recomputes a fresh deadline from the same status and never substitutes client `Utc::now()` for the DB interval. The check-start subtraction conservatively includes DB roundtrip/lock delay. Reads, collection, send and retries use the minimum of that pinned deadline, the original whole-call deadline and their 2/6-second ceiling. Tests in Task 8 must delay the DB roundtrip and move a supplied test wall-clock observation independently; neither may increase the remaining monotonic budget.

The prototype-style method declarations above are a signature ledger, not Rust source to paste as inherent declarations. Every task writes real method bodies. Introducing a callable signature with a temporary fail-closed body solely to compile a new test is allowed; a compiler error is never RED evidence, and no temporary body is committed as a completed behavior.

## Verification command envelope

Every RED/GREEN command below is future execution-only after main's gate. Precheck Docker context using main's approved local fixture procedure, with `DOCKER_CONTEXT=orbstack`, `DOCKER_HOST` absent and `PROXMOXVEAUTOPILOT_LINUX_TEST_DB` absent. The latter exact selector is verified in `proof_support/mod.rs:176`. Preserve cached default Docker behavior. No Linux runtime build or fixture receipt is implied by a macOS command.

Use this finite outer watchdog for each command. Its cleanup follows the separately reviewed bounded group/direct-child pattern in `candidate-evidence-stream.py`: poll the exact owned group even if its leader exited, treat EPERM as uncertainty, and always attempt bounded direct-child reap. It does not prove absence of escaped sessions or Docker-daemon containers; uncertain cleanup is exit125, never a passing test. Save command, UTC start/end, exit and complete output in the task's SDD report. The first numeric argument is the work bound; cleanup has an additional maximum six seconds of polling allowance. Do not broad-kill cargo/docker or claim this process supervisor ends Docker containers.

```bash
python3 -c 'import os,signal,subprocess,sys,time
def exists(pgid):
 try: os.killpg(pgid,0); return True
 except ProcessLookupError: return False
 except PermissionError: return True
def stop_group(p):
 for sig in (signal.SIGTERM,signal.SIGKILL):
  if not exists(p.pid): break
  try: os.killpg(p.pid,sig)
  except (ProcessLookupError,PermissionError): pass
  end=time.monotonic()+2
  while time.monotonic()<end:
   p.poll()
   if not exists(p.pid): break
   time.sleep(.025)
 return not exists(p.pid)
def reap_direct(p):
 for sig in (signal.SIGTERM,signal.SIGKILL):
  if p.poll() is not None: break
  try: os.kill(p.pid,sig)
  except (ProcessLookupError,PermissionError): pass
  end=time.monotonic()+1
  while p.poll() is None and time.monotonic()<end: time.sleep(.025)
 return p.poll() is not None
end=time.monotonic()+int(sys.argv[1])
p=subprocess.Popen(sys.argv[2:],start_new_session=True)
code=124; group_ok=False; child_ok=False
try:
 while time.monotonic()<end:
  status=p.poll()
  if status is not None:
   code=status
   if exists(p.pid): code=125
   break
  time.sleep(.025)
finally:
 try: group_ok=stop_group(p)
 finally: child_ok=reap_direct(p)
 if not group_ok or not child_ok or exists(p.pid):
  print("owned_process_cleanup_unconfirmed",file=sys.stderr); code=125
sys.exit(code)' 180 env DOCKER_CONTEXT=orbstack cargo test --offline --locked -p postgres-store --test osdeploy_durability migration_is_additive_and_registration_stays_declaration_only -- --exact --nocapture --test-threads=1
```

In the tasks below, `Run (180s watchdog):` means execute that exact wrapper with 180 and the displayed argv. Each test has an explicit bounded wait around a fake pause/task. A task must not invent a larger timeout to make a failing assertion pass. Reproduce the narrow failing test serially; preserve unexplained failures separately rather than calling them fixed by a later pass.

### Task 1: Add immutable durability schema without execution side effects

**Files:** Create migration and `tests/osdeploy_durability.rs`; modify `src/store.rs`, `tests/osdeploy_support/mod.rs` as mapped above.

**Interfaces:** Consumes existing `Fixture::new`, `plan`, `PgStore::migrate/enqueue_osdeploy`; produces all nine accepted tables and unchanged migration/registration API. Test harness declares `mod osdeploy_support;` and imports `Fixture`/`plan` explicitly. Add test-only `Fixture::new_before_durability() -> impl Future<Output=Fixture>` for rollback tests: same unique local Container/database ownership and30-second setup bound, primary pool max1, install only the exact checked-in migrations0001–0004, then existing authority bootstrap. Keep `Fixture::new()` and its six-connection default unchanged. The helper is not exported from a production crate.

- [ ] Write the behavioral schema test before migration changes:

```rust
#[tokio::test]
async fn migration_is_additive_and_registration_stays_declaration_only() {
    let f = osdeploy_support::Fixture::new().await;
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_tables WHERE schemaname='rust_controller' AND tablename IN \
        ('osdeploy_decisions','osdeploy_deadlines','osdeploy_attempt_bindings','osdeploy_lease_epochs',\
        'osdeploy_pve_evidence','osdeploy_pve_dispatches','osdeploy_pve_receipts',\
        'osdeploy_run_cancellations','osdeploy_schedule_projection')")
        .fetch_one(&f.pool).await.unwrap();
    assert_eq!(count, 9);
    f.store.enqueue_osdeploy(controller_domain::RunId::new(), &osdeploy_support::plan()).await.unwrap();
    let before = f.snapshot().await;
    f.store.migrate().await.unwrap();
    assert_eq!(f.snapshot().await, before);
    for name in ["osdeploy_decisions", "osdeploy_deadlines", "osdeploy_attempt_bindings",
        "osdeploy_lease_epochs", "osdeploy_pve_evidence", "osdeploy_pve_dispatches",
        "osdeploy_pve_receipts", "osdeploy_run_cancellations", "osdeploy_schedule_projection"] {
        let count: i64 = sqlx::query_scalar(&format!("SELECT count(*) FROM rust_controller.{name}"))
            .fetch_one(&f.pool).await.unwrap();
        assert_eq!(count, 0, "{name}");
    }
}
```

- [ ] Run (180s watchdog): `env DOCKER_CONTEXT=orbstack cargo test --offline --locked -p postgres-store --test osdeploy_durability migration_is_additive_and_registration_stays_declaration_only -- --exact --nocapture --test-threads=1`. Expected behavioral RED: table count 0, expected 9.
- [ ] Implement accepted schema literally: all columns/FKs/checks/indexes from proposal lines 13–33, including operation-bound typed preflight/epoch uniques. Define every initial action with its resolution/attempt-null check; leave `grace_wait_activated` out. Use the existing `reject_native_mutation()` trigger function for UPDATE/DELETE/TRUNCATE on eight immutable tables; independently check row and statement trigger installation on replay. Add migration after 0004:

```rust
let mut osdeploy_migration = self.pool.begin().await?;
sqlx::raw_sql(include_str!("../migrations/0005_osdeploy_durability.sql"))
    .execute(&mut *osdeploy_migration).await?;
osdeploy_migration.commit().await?;
```

The existing `migrate` executes each old SQL file directly against `self.pool`; preserve 0001–0004 behavior exactly. Only new 0005 uses the SQLx-owned transaction above, with no raw BEGIN/COMMIT in the SQL file. This ensures Drop tracks rollback on SQL failure or future cancellation rather than returning an untracked open transaction to the pool. Schema ordering: decisions first; deadlines referencing decisions; attempt bindings; epochs; evidence; dispatches; receipts; cancellations; scheduling projection. Name the typed decision FKs `osdeploy_deadline_anchor_decision_fk`, `osdeploy_binding_activation_decision_fk`, `osdeploy_epoch_acquisition_decision_fk`, `osdeploy_dispatch_decision_fk`, `osdeploy_cancellation_decision_fk`, `osdeploy_schedule_basis_decision_fk`; declare these DEFERRABLE INITIALLY DEFERRED for the accepted typed-reference boundary. Immediate OP/AT/EV and binding FK targets must precede row insertion. No existing migration edit.
- [ ] Extend snapshot's closed table array with all nine names; retain existing `assert_fresh_counts`. Add assertions that all new execution counts are zero. Add SQL tests for all immutable trigger forms, migration replay restoring each separately removed test-owned trigger, wrong-operation FK references, duplicate dispatch/attempt binding, null-attempt allowed actions and disallowed actions, oversize object and invalid scope/action/counter/time rows. Add `migration_failure_rolls_back_and_reuses_clean_connection` using `Fixture::new_before_durability()`: create an intentionally incompatible owned `osdeploy_pve_dispatches(broken integer)` before migrate so the later receipt FK fails; reacquire the primary pool's same backend PID, prove no partial 0005 objects/untracked transaction, execute a normal query, drop only that exact owned obstruction, then migrate successfully. Add `cancelled_migration_rolls_back_and_reuses_clean_connection` with an owned test event trigger at `ddl_command_end`, gated to creation of `rust_controller.osdeploy_decisions` only, waiting on a unique advisory key held by a second owned connection. Observe the exact 0005 backend wait, drop the pending migrate future, then release the barrier before reacquiring the primary connection so queued SQLx rollback can drain. Prove that same backend PID has no partial 0005 objects/untracked transaction and can execute a normal query; remove only that exact test event trigger/function and migrate successfully. Bound all barrier waits/reacquisition and do not mistake a 0001–0004 wait for cancellation inside 0005. The pre-migration fixture cannot use the full execution-table snapshot before those tables exist; query the catalog and existing tables explicitly. No public arbitrary-migration runner or production failpoint is added. Test-owned invalid rows/barriers are corruption/atomicity inputs, never executable predecessor fixtures.
- [ ] Run same exact test GREEN, then (300s watchdog) the complete `--test osdeploy_durability` and `--test osdeploy_registration` harnesses serially. Record counts and any helper repetitions.
- [ ] Commit only Task 1 paths: `git commit -m "feat(rust-controller): add immutable osdeploy durability schema"`; independent spec/code review before Task 2.

### Task 2: Closed execution values and strict journal-backed reload

**Files:** Create `src/osdeploy/execution.rs`, `execution/wire.rs`, `execution/load.rs`; modify `src/osdeploy.rs`, `src/lib.rs`, durability tests.

**Interfaces:** Consumes nine tables, current declaration loader; produces `OsDeployExecutionError`, `OsDeployProgress`, `OsDeployOperationSnapshot`, `PgStore::load_osdeploy_operation` exactly as ledger. Private loader is `pub(crate) async fn load_execution(tx: &mut Transaction<'_, Postgres>, operation: OperationId) -> Result<OsDeployOperationSnapshot, OsDeployExecutionError>`.

- [ ] Add callable signature returning a declaration-only snapshot temporarily, then this test (not a compiler-failure RED):

```rust
#[tokio::test]
async fn unactivated_reload_rejects_an_unbound_attempt() {
    let f = osdeploy_support::Fixture::new().await;
    let ids = f.store.enqueue_osdeploy(controller_domain::RunId::new(), &osdeploy_support::plan()).await.unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state) VALUES($1,$2,1,'pending')")
        .bind(controller_domain::AttemptId::new().as_uuid()).bind(op.as_uuid())
        .execute(&f.pool).await.unwrap();
    assert!(matches!(f.store.load_osdeploy_operation(op).await,
        Err(postgres_store::OsDeployExecutionError::Validation)));
}
```

- [ ] Run (180s watchdog): `env DOCKER_CONTEXT=orbstack cargo test --offline --locked -p postgres-store --test osdeploy_durability unactivated_reload_rejects_an_unbound_attempt -- --exact --nocapture --test-threads=1`. Expected RED: ignored unbound attempt is incorrectly accepted.
- [ ] Implement repeatable-read read-only public reload and locked private reload. Decode the full admitted registration, all attempts, binding/scopes, journal revisions/event types and indexed decisions/evidence/dispatch/receipt, compare every immutable hash/time/foreign identity. Require exactly zero attempts when unactivated and exactly one number-1 attempt when bound; selected attempt times equal binding; selected nonnull/non-ready resolution agrees with latest corresponding state event. Distinguish control/receipt revision from selected state proof. Never trust generic EvidenceRecorded JSON as typed provenance.
- [ ] Implement wire structs from every initial action row in the accepted proposal. Root and nested fields are closed objects; use required nullable deserializers, no `serde(default)` for mandatory Option fields, and reject duplicate keys before conversion to `Value`. Canonicalize validated objects with existing event-journal digest semantics. Map errors exactly to `osdeploy_execution_validation_failed`, `osdeploy_execution_conflict`, `osdeploy_execution_fence_lost`, `osdeploy_execution_capability_unavailable`, `osdeploy_execution_storage_unavailable`; no input payloads. Make the execution module/helpers crate-visible only where the scheduler sibling needs them. The essential required-null shape is:

```rust
use serde::Deserialize;
fn required_nullable<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where D: serde::Deserializer<'de>, T: serde::Deserialize<'de> {
    Option::<T>::deserialize(d)
}
// Apply #[serde(deserialize_with="required_nullable")] to each required nullable field.
// Decode action detail through its closed variant struct, never a map of arbitrary values.
```

- [ ] Add unit wire tests for missing/null/non-null/unknown/duplicate/array values, action-resolution pairing, source, hash case, all initial reasons and size boundaries; integrate corruption tests for extra attempts, changed immutable time/hash, wrong event kind/revision/index, missing selected decision and receipt bound to another dispatch. Public ownership values remain non-Deserialize; compile-fail checks supplement, never replace behavioral tests.
- [ ] Run exact RED test GREEN, then (300s watchdog) full durability and registration harnesses and `cargo test --offline --locked -p postgres-store --lib`. Commit Task 2 paths with `feat(rust-controller): restore strict osdeploy execution history`; independent review.

### Task 3: Actual activation, same-attempt lease lifecycle and private append proofs

**Files:** Create scheduler OSDeploy module, transaction/lifecycle/transition files; modify scheduler declaration and execution loader/tests.

**Interfaces:** Consumes strict loader; produces all Task 3 ledger methods and `OsDeployLeaseStatus`. Task 2 names its private validated common wire object `wire::DecisionEnvelope`; its closed detail variant is selected by the schema action, not arbitrary JSON. Private interfaces are `async fn locked_execution(tx: &mut Transaction<'_, Postgres>, operation: OperationId) -> Result<OsDeployOperationSnapshot, OsDeployExecutionError>`, `async fn append_osdeploy_decision(tx: &mut Transaction<'_, Postgres>, event: EventId, semantic_key: &str, decision: &wire::DecisionEnvelope) -> Result<i64, OsDeployExecutionError>`, and `async fn append_osdeploy_transition(tx: &mut Transaction<'_, Postgres>, proof: OsDeployTransitionProof) -> Result<i64, OsDeployExecutionError>`. The locked loader takes run/all-operation locks after authority. Append helpers write journal, outbox and projection in that same passed transaction; decision carries original operation/attempt/before-revision/evaluated-at, and the transition proof carries the exact decision event/current/target/revision/attempt/time. All proof fields/constructors remain inside the OSDeploy scheduler module.

- [ ] Introduce fail-closed callable lifecycle bodies for test compilation, then write:

```rust
#[tokio::test]
async fn activation_creates_one_attempt_with_policy_deadline() {
    let f = osdeploy_support::Fixture::new().await;
    let p = osdeploy_support::plan();
    let ids = f.store.enqueue_osdeploy(controller_domain::RunId::new(), &p).await.unwrap();
    let op = ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let g = f.scheduler().claim_osdeploy_bound(op, ids.workflow_sha256(), 1).await.unwrap().unwrap();
    assert_eq!(g.attempt_number(), 1);
    assert_eq!((*g.deadline_at() - *g.acquired_at()).num_seconds(), 300);
    let s = f.store.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(s.attempt_id(), Some(g.attempt_id()));
    assert_eq!(s.state(), controller_domain::ExecutionState::Leased);
    assert!(f.other_scheduler().claim_osdeploy_bound(op, ids.workflow_sha256(), 1).await.unwrap().is_none());
    f.scheduler().start_osdeploy_bound(&g, ids.workflow_sha256()).await.unwrap();
    let starts: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'")
        .bind(op.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert_eq!(starts, 1);
}
```

- [ ] Run (180s watchdog) exact test `activation_creates_one_attempt_with_policy_deadline`. Expected RED is no grant/incorrect persisted lifecycle, not a missing symbol.
- [ ] Implement authority/run/cap/sorted operations/attempt-lease locking and sampled DB T. Initial grant's `acquired_at=T` is also attempt activation time; scope ends T+policy. Insert in the accepted order. Cap counts all unexpired OSDeploy leases including old generations. Reject historical generic/fourth-stage/cancelled/invalid hash rows with fixed mappings; ineligible predecessor/cap returns None. Recheck strict clock/lease/deadline before committing an active lease.
- [ ] Start emits first actual `attempt_started` once, legal Leased→Running plus `evaluation_started` for this epoch. Heartbeat updates one epoch's live lease only, journals refresh with new revision, and returns a rebuilt grant/status; continuation performs no writes and reports current DB checked-at. Task 6 adds actual Waiting resume/start alongside its parking test; do not commit a resume stub in Task 3. Task 7 adds the recovered-Pending claim branch alongside safe-reclaim tests. No generic claim helper.
- [ ] Private proof enum must include `UnactivatedScopeExpired`, `ActivatedScopeExpired`, `ExpiredUnstartedSameAttempt`, `ExpiredReadOnlyEvaluation`, `CancelledUnexposed`, `CancelledExposed`, `OriginalDispatchReconciliation`. Each constructor validates closed facts under locks, not public target state. Preserve old `TransitionPolicy` semantics; extract a private low-level journal/state persistence function only if needed, with old validated callers unchanged and new proof constructor gating its OSDeploy caller. Do not add future grace proof/action yet.
- [ ] Add independent-pool same-operation and last-cap races, stale token/worker/generation/CAS, deadline equality, heartbeat no extension, failed activation rollback at every statement boundary, all thirteen disabled-stage entries/no writes, start replay exactly one event. Reclaim/park behaviors get end-to-end coverage in Tasks 6–7 rather than manufactured success rows.
- [ ] Run exact test GREEN and all durability tests (300s watchdog), plus scheduler postgres regression (300s watchdog). Commit `feat(rust-controller): activate osdeploy attempts with fenced short leases`; independent review.

### Task 4: Typed physical collection advice, provenance and historical request reconstruction

**Files:** Create `execution/history.rs`, scheduler `pve.rs`, `tests/osdeploy_execution_support/mod.rs`; modify exports/loader/durability tests.

**Interfaces:** Produces the Task 4 ledger methods. Test support declares `pub struct Scenario { pub db: osdeploy_support::Fixture, pub ids: OsDeployWorkflowIds, pub fake: Arc<NativeFakePve> }`, `Scenario::new(mutation_seconds:u32, grow:bool) -> impl Future<Output=Self>`, `Scenario::started(stage:OsDeployStage) -> impl Future<Output=LeaseGrant>`, `Scenario::collect(&ProvisioningEvaluationContextV1) -> impl Future<Output=ProvisioningEvidenceV1>`. `started` uses real claim/start only and cannot satisfy predecessors. Methods are test-only in the test crate.

- [ ] Build Scenario with the following exact owned world; import `crate::osdeploy_support`, pve-port types, chrono::Utc, serde_json::json and std::sync::Arc in the test support module. No fabricated target provenance, ownership or predecessor. Derive reads from this world, not expected postcondition scripts:

```rust
impl Scenario {
    pub async fn new(mutation_seconds: u32, grow: bool) -> Self {
        let db = osdeploy_support::Fixture::new().await;
        let node = NodeName::parse("node-a").unwrap();
        let source = ProvisioningVmConfigV1::from_wire(node.clone(), Vmid::new(900).unwrap(),
            NativeEvidenceSource::FakePve, json!({"node":"node-a","vmid":900,
            "digest":"owned-template-1","name":"blank-template","cores":2,"memory":2048,
            "scsi0":"disk-store:vm-900-disk-0,size=80G",
            "smbios1":"uuid=33333333-3333-4333-8333-333333333390",
            "net0":"virtio=02:00:00:00:09:00,bridge=vmbr0,firewall=0",
            "bios":"seabios","cpu":"host","balloon":0,"agent":"enabled=0,type=virtio",
            "boot":"order=scsi0","template":1}), Utc::now()).unwrap();
        let hash = source.template_fingerprint().unwrap();
        let plan = osdeploy_support::altered(|v| {
            v["template_config_sha256"] = json!(hash);
            v["policy"]["mutation_seconds"] = json!(mutation_seconds);
            v["disk"]["requested_gib"] = json!(if grow {120} else {80});
            v["disk"]["effective_bytes"] = json!(if grow {128849018880_u64} else {85899345920_u64});
            v["disk"]["growth_required"] = json!(grow);
        });
        let fake = Arc::new(NativeFakePve::new());
        fake.insert_provisioning_vm(source, PowerState::Stopped).unwrap();
        fake.set_node_status(NodeStatus::from_wire(node.clone(), json!({"uptime":100}), Utc::now()).unwrap());
        fake.set_storage_status(StorageStatus::from_wire(node.clone(), StorageName::parse("disk-store").unwrap(),
            json!({"active":1,"enabled":1,"content":"images","avail":999999999999_u64}), Utc::now()).unwrap());
        fake.set_bridges(BridgeInventory::from_wire(node.clone(),
            json!([{"type":"bridge","iface":"vmbr0","active":1}]), Utc::now()).unwrap());
        for (storage, volid) in [("media-store","media-store:iso/deploy.iso"),("drivers","drivers:iso/virtio.iso")] {
            fake.set_provisioning_media(ProvisioningMediaInventoryV1::new(node.clone(),
                StorageName::parse(storage).unwrap(), vec![volid.to_owned()], ProvisioningCoverageV1::Complete, Utc::now()).unwrap());
        }
        let ids = db.store.enqueue_osdeploy(controller_domain::RunId::new(), &plan).await.unwrap();
        Self { db, ids, fake }
    }
    pub async fn started(&self, stage: osdeploy_adapter::OsDeployStage) -> postgres_store::LeaseGrant {
        let g = self.db.scheduler().claim_osdeploy_bound(self.ids.operation(stage), self.ids.workflow_sha256(), 1).await.unwrap().unwrap();
        self.db.scheduler().start_osdeploy_bound(&g, self.ids.workflow_sha256()).await.unwrap();
        g
    }
}
```

Scenario collection is a test-local adaptation of the complete existing `pve-port/tests/provisioning_support/world.rs:67` collector: replace its fixed `vm()` with `context.facts().plan.expected().vm()`, fixed node with that VM node, fixed media names with the two storages parsed from the registered expectation's exact ISO volids, and its world's fixed binding with the supplied context's binding. Read inventory plus at most32 identity rows, source/target config+power, the exact original receipt's task UPID if present; qga remains None. Wrap the whole owned async collection in a six-second timeout. Keep the existing source file untouched and do not call its fixed-ID `World::prepare/submit` helpers, which have no durable family authority.
- [ ] Add fail-closed new method bodies for compilation and write:

```rust
#[tokio::test]
async fn typed_evidence_keeps_original_event_fence() {
    use osdeploy_adapter::OsDeployStage;
    use pve_port::ProvisioningEvaluationModeV1;
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(OsDeployStage::Clone).await;
    let snap = s.db.store.load_osdeploy_operation(g.operation_id()).await.unwrap();
    let c = s.db.store.load_osdeploy_pve_context(g.operation_id(), snap.revision(), ProvisioningEvaluationModeV1::Preflight).await.unwrap();
    let e = s.collect(&c).await;
    let event = s.db.store.record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &e).await.unwrap();
    let saved: i64 = sqlx::query_scalar("SELECT aggregate_revision FROM rust_controller.journal_events WHERE event_id=$1")
        .bind(event.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(e.facts().binding.evidence_fence(), (saved - 1) as u64);
    let again = s.db.store.record_osdeploy_pve_evidence(g.operation_id(), g.attempt_id(), snap.revision(), &e).await.unwrap();
    assert_eq!(again, event);
}
```

- [ ] Run (180s watchdog) exact `typed_evidence_keeps_original_event_fence`; expected RED is rejected valid evidence or duplicate event/fence mismatch.
- [ ] Build context with `ProvisioningBindingV1::new(run,operation,attempt,workflow_sha,pve_plan,current_revision as u64)` and admitted physical history. Clone request marker is its original Clone operation UUID; reconstruct using `serde_json::from_value::<CloneRequest>(json!({"vm":registered_plan.vm(),"operation_id":clone_operation,"request_marker":clone_operation.as_uuid()}))`, mapping decode failure to Validation. Here `registered_plan: &OsDeployPlanV1` and `clone_operation: OperationId` come from the reloaded registration. Replay selected Clone through `ProvisioningCloneOwnershipV1::from_satisfied_clone` and predecessors through `ProvisioningStageBaselineV1::from_satisfied` at original selected time/context, never at a new invented historical time. The existing baseline constructor permits preflight ObservedNoChange.
- [ ] Evidence transaction locks/reloads, enforces original fence and full bindings, atomically inserts journal/outbox/provenance index/projection. Equivalent replay checks existing semantic key before current CAS; a different admitted input cannot occupy its key. Generic shape-matching JSON without provenance index is rejected by consumers.
- [ ] Request advice uses actual indexed preflight and before-state: Clone→`CloneProvisioningRequestV1::new`, EnsureCapacity→`GrowDiskRequestV1::new`, ConfigurePe→`ConfigureProvisioningRequestV1::new`. All use completed typed inputs and selected history; other actions reject. Locked begin repeats construction at DB now and compares the entire request/digest, not advice identity. Returning inspectable request data is not returning a permit.
- [ ] Add wrong workflow/stage/attempt/source/fence/event tests, generic-evidence spoof, duplicate changed bytes, intervening observation/CAS, request before-state mismatch, source-template mismatch and hash corruption. Historical successful-chain acceptance is exercised after real Task 6 decisions exist; never create fake Satisfied predecessors to make Task 4 runnable.
- [ ] Run exact test GREEN and full durability harness (300s watchdog). Commit `feat(rust-controller): bind osdeploy physical evidence to durable history`; independent review.

### Task 5: Commit consuming dispatch and original-response capture

**Files:** Create `scheduler/osdeploy/receipt.rs`; modify `pve.rs`, execution values/loader/exports, tests.

**Interfaces:** Produces exact selected permit/capture methods in ledger. Test support adds `pub struct Ready { pub grant: LeaseGrant, pub event: EventId, pub revision:i64, pub request:ProvisioningMutationRequestV1 }` and `pub async fn Scenario::ready(&self, stage:OsDeployStage) -> Ready`: start real stage, load context, collect, record evidence, reload current revision, prepare request. No send or decision occurs in this helper. It requires real Ready advice; no-change capacity uses the separate preflight decision path in Task 6.

- [ ] Write callable fail-closed boundary plus test:

```rust
#[tokio::test]
async fn committed_but_unpolled_send_never_yields_a_second_permit() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let r = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler.begin_osdeploy_pve_dispatch(&r.grant, r.revision, r.event, &r.request).await.unwrap();
    drop(permit.submit_fake_once(&s.fake));
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let snap = s.db.store.load_osdeploy_operation(r.grant.operation_id()).await.unwrap();
    assert!(snap.dispatch().is_some());
    assert!(scheduler.begin_osdeploy_pve_dispatch(&r.grant, snap.revision(), r.event, &r.request).await.is_err());
    drop(capture);
}
```

- [ ] Run (180s watchdog) exact named test; expected RED is missing committed dispatch or second permit admitted. Compiler errors do not satisfy this step.
- [ ] Implement begin transaction using current owner/token/CAS/cancellation/deadline/lease, typed preflight/context/full reconstructed request. Require Ready at fresh DB time. Journal dispatch decision, immutable request binding and exact index in one commit; final clock/freshness check before commit. Only then construct the tuple. The consuming method body is exactly one call:

```rust
pub async fn submit_fake_once(self, fake: &NativeFakePve) -> Result<MutationReceipt, PveWriteError> {
    fake.submit_provisioning(self.dispatch.request()).await
}
```

- [ ] Capture locks shared authority without generation-ownership check then run/all operations, compares all original identity fields, validates `ProvisioningReceiptV1::new`, and records first DB time in receipt+event with `accepted_at=recorded_at`. Equivalent retry returns unchanged first event/times; conflict returns Conflict; receipt never schedules/finalizes. Never create capture from public snapshots or return permit on errors.
- [ ] Add real submit once/task receipt and synchronous ConfigurePe capture after Task 6 permits its real predecessor; stale-original generation/lease/cancelled receipt capture; wrong UPID/action/VM/node/synchronous-kind; rollback before every dispatch insertion; commit-before-call crash/drop; paused-send cancellation; error variants OutcomeUnknown/Unauthorized/Rejected/Conflict; no returned permit. Add compile-fail doctests for move-after-call, Clone, Deserialize and request getter on both opaque handles, checking intended diagnostics.
- [ ] Run exact test GREEN, durability harness (300s watchdog) and store docs (180s watchdog). Commit `feat(rust-controller): consume osdeploy dispatch once and capture original receipts`; independent review.

### Task 6: Durable evaluation, parking and genuine three-stage history

**Files:** Create scheduler `decision.rs`; extend `history.rs`, lifecycle resume and durability support/tests.

**Interfaces:** Produces `decide_osdeploy_pve`, real parking; consumes Tasks 2–5 and existing pure evaluators. Support adds `Scenario::finish_stage(stage) -> impl Future<Output=ExecutionState>`: call ready; consuming submit/capture; reload Outcome context; collect/record; decide, looping only read-only pending observations with bounded waits on the original dispatch. For no-change capacity, collect preflight and decide Satisfied directly without calling ready/request/send. The helper uses no direct SQL state writes.

- [ ] Add fail-closed decision body and this behavioral test:

```rust
#[tokio::test]
async fn real_no_growth_baseline_enables_configure_without_capacity_dispatch() {
    let s = osdeploy_execution_support::Scenario::new(300, false).await;
    for stage in [osdeploy_adapter::OsDeployStage::Clone,
        osdeploy_adapter::OsDeployStage::DiskCapacity, osdeploy_adapter::OsDeployStage::ConfigurePe] {
        assert_eq!(s.finish_stage(stage).await, controller_domain::ExecutionState::Satisfied);
    }
    let capacity = s.db.store.load_osdeploy_operation(s.ids.operation(osdeploy_adapter::OsDeployStage::DiskCapacity)).await.unwrap();
    assert!(capacity.dispatch().is_none());
    assert_eq!(s.fake.recorded_provisioning_submissions().len(), 2);
    assert!(matches!(s.db.scheduler().claim_osdeploy_bound(s.ids.operation(osdeploy_adapter::OsDeployStage::StartPe), s.ids.workflow_sha256(), 1).await,
        Err(postgres_store::OsDeployExecutionError::CapabilityUnavailable)));
}
```

- [ ] Run (180s watchdog) exact test; expected RED is valid decision rejected/real successor not admitted, not a seeded predecessor.
- [ ] Under locks reconstruct context at current DB now and evaluate indexed evidence. Preflight Ready is handled only by begin; preflight Satisfied/ObservedNoChange is persisted with selected preflight context and no dispatch. Waiting writes decision/state/attempt/schedule and removes matching lease atomically; terminal result writes decision/state/completed-at and removes matching lease/schedule. Implement `resume_osdeploy_bound` against current Waiting/basis/due/attempt with fresh token/epoch and original times, and add legal Waiting→Running start without repeating attempt_started. Sample selected decision time once and keep it for historical restoration. Unavailable preflight is Unknown without dispatch and remains non-recovering. At/after original deadline use exact deadline Unknown, including late apparent success.
- [ ] Add real growth prefix (three submissions), genuine first-three reload and historical old-deadline proof, task-running→Waiting→due resume→same-attempt completion, one attempt/start event across epochs, accepted task fails, synchronous response loss, changed config/identity conflict and stale late evidence. Block evaluation/dispatch races with independent pools and bounded FakePause; never wait on a pause without also observing task completion/error under an outer bound.
- [ ] Run exact test GREEN and all durability tests (600s watchdog), plus pve-port provisioning evaluation/fake harnesses (300s watchdog). Commit `feat(rust-controller): persist osdeploy outcomes and same-attempt waits`; independent review.

### Task 7: Recovery, cancellation and starvation-free due/deadline repair

**Files:** Create `recovery.rs`, `discovery.rs`; extend execution values/loader/exports, transition proofs and durability tests.

**Interfaces:** Produces all Task 7 recovery/discovery methods and cursor/result types in ledger. `discover_osdeploy_due` returns at most 32 combined due values, sorted `(next_check_at,operation_id)`; modes retain separate eligibility. All mutation methods use selected original history, not a due value as authority.

- [ ] Write cancellation test first with callable fail-closed body:

```rust
#[tokio::test]
async fn cancellation_fences_all_sixteen_without_manufactured_attempts() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let g = s.started(osdeploy_adapter::OsDeployStage::Clone).await;
    s.db.scheduler().cancel_osdeploy_run(s.ids.run_id()).await.unwrap();
    for stage in osdeploy_adapter::OsDeployStage::ALL {
        let snap = s.db.store.load_osdeploy_operation(s.ids.operation(stage)).await.unwrap();
        assert!(snap.cancelled());
        assert_eq!(snap.state(), controller_domain::ExecutionState::Blocked);
        assert_eq!(snap.attempt_id(), (stage == osdeploy_adapter::OsDeployStage::Clone).then_some(g.attempt_id()));
    }
    let before = s.db.snapshot().await;
    s.db.scheduler().cancel_osdeploy_run(s.ids.run_id()).await.unwrap();
    assert_eq!(s.db.snapshot().await, before);
}
```

- [ ] Run (180s watchdog) exact named test; expected RED is absent atomic fence/outcomes. Implement cancellation with terminal-preserve→elapsed-scope→exposure classification and run control decision, then run GREEN before moving to reaper cases.
- [ ] Write/run RED for each independent recovery behavior: real unstarted expired lease reclaimed twice with same attempt/times; expired reclaimed Pending→Unknown; read-only Running/resumed-Waiting repark; dispatch-present expired observer→Unknown; matching terminal residual lease revocation versus foreign-attempt rejection; original-dispatch Unknown→Satisfied/Failed/Conflicted only before deadline; missing original task receipt not polled; receipt repair re-enables bounded observation only. Exercise synchronous receiptless reconciliation through completed evaluator only. No mutation retry, no new lease on Unknown reconciliation.
- [ ] Implement exact private proofs and atomic ledger/outbox/projection changes for those cases. Reaper uses no-row-lock bounded discovery, validates original epoch/token/activity under authority/run/all operations/leases, then chooses safe reclaim/repark/uncertain/expiry or fixed rejected count. Missing/inconsistent activity never creates an exposure fiction. Expiry semantic key pins original scope and attempt-or-unactivated; Unknown→Unknown expiry appends at most one decision and clears schedule without redundant state event. Future inherited scope membership is implemented as a closed mapping, but real anchors remain unreachable before callback integration; test wrong/unopened anchors reject and private classification vectors without introducing an execution bypass or claiming a live callback proof.
- [ ] Implement keyset projection repair from validated selected decision/control chain, scanning registered Waiting/Unknown in pages of 32; receipt/evidence only advance watermark. Implement separate expiry cursor `(deadline_at,run_id,scope_key,operation_id)`, at most 32 examined operation candidates per sweep, excluding cancelled/exact-adjudicated members. Advance after every examined valid/rejected/stale candidate, wrap on end for next sweep; locked validation decides actions. No projection-based permission or cursor persistence authority.
- [ ] Add one-second-scope owned tests for strict equality using DB-time barrier/transaction sequencing, never shortening stored attempt clocks in a passing execution test. Add >32 registered operations, invalid early candidate, adjudicated early scopes, missing projections, behind-cursor arrival and restart/wrap tests. For corruption, disable only exact owned immutable trigger within a test transaction, restore it before leaving; do not silently repair poisoned history. Check per-sweep examined≤32 and eventual later-member progress across wraps. Test cancellation races with dispatch, park, receipt and collector; expired scope wins before cancellation only when already elapsed, while existing terminal decisions remain selected.
- [ ] Run full durability harness (900s watchdog) and native/scheduler regressions (600s watchdog each), preserve exact failures. Commit `feat(rust-controller): recover osdeploy waits and fence cancelled runs`; independent review.

### Task 8: Existing-controller integration and owned send/drain lifecycle

**Prerequisite now accepted:** local Docker storage A/B, Bsource f7c9000 and main acceptance bf0fc82. Apply the complete Task8 dispatch clarifications in `next-durable-planning-decisions.md` before implementing. In particular, awaited authority/drain stay serialized by lifecycle, but synchronous closure publication at close entry/timeout must not await it. Open captures an unconsumed watch-version marker under state mutex before lifecycle wait, checks it atomically with opening after fresh authority/zero activity, and never refreshes invalidated work. Close always publishes true even when already true; timeout drops owned work, republishes closure and never proves quiescence. Require all three named deterministic lifecycle race cases there, in addition to the tests below. This precise ruling controls the broader lifecycle sentence below.

**Unknown timing interface amendment:** apply the planning decisions' complete Task8 Unknown observation timing ruling. The original no-grant reconciliation path lacked a DB-time interval, so Task8 additionally owns exactly store execution.rs opaque OsDeployPveObservation, store lib.rs export and scheduler/osdeploy/pve.rs additive load_osdeploy_pve_context_with_budget. Old APIs/schema and behavior remain unchanged. Same read-only reconstruction transaction returns DB checked_at/static remaining; caller subtracts monotonic time since check-start and caps all reads by original scope/whole-call deadlines, without a lease or wall-clock inference. Tests remain in the new controller harness, with required new-path RED/GREEN, full store library300s/registration600s and docs180s added to the existing final gates. This scoped amendment overrides only the previous eight-path limit; the complete reviewed Task8 range still starts36f73fb.

**Files:** Create operation-controller OSDeploy modules/tests; modify its `lib.rs` and dev-dependencies, plus only the explicitly allowed existing osdeploy-adapter lock dependency edge. Reuse the store test support by path with both `osdeploy_support` and `osdeploy_execution_support` modules in the integration harness; no test helpers enter production libraries.

**Interfaces:** Produces controller API in ledger. Private types: `OsDeploySendObservation::{ReceiptCaptured,Uncertain}`, `OsDeploySendAdmission`, `OsDeployAdmissionGuard`. Private helper has the exact selected signature:

```rust
async fn submit_and_capture_once(
    admission: &OsDeploySendAdmission, scheduler: &Scheduler, fake: &NativeFakePve,
    grant: &LeaseGrant, workflow_sha256: &str,
    permit: OsDeployDispatchPermit, capture: OsDeployResponseCapture,
    whole: tokio::time::Instant,
) -> Result<OsDeploySendObservation, OsDeployControllerError>;
```

Apply the planning decisions' Task8 private send endpoint clarification: `whole` is the original endpoint pinned before the controller's first await, passed unchanged through private phases. Never start a new24second helper budget. This private-only argument correction changes no public API, source scope or capture authority policy; delayed-before-send/capture tests remain required.

- [ ] Add the dev dependency `osdeploy-adapter = { path = "../osdeploy-adapter" }`, callable closed controller bodies and this test:

```rust
#[tokio::test]
async fn closed_start_cannot_activate_or_send() {
    let s = osdeploy_execution_support::Scenario::new(300, true).await;
    let c = operation_controller::OsDeployController::new(s.db.store.clone(), s.db.scheduler(), s.fake.clone(), 1).unwrap();
    let before = s.db.snapshot().await;
    assert!(matches!(c.run_osdeploy_once(s.ids.operation(osdeploy_adapter::OsDeployStage::Clone)).await,
        Err(operation_controller::OsDeployControllerError::AdmissionClosed)));
    assert_eq!(s.db.snapshot().await, before);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    c.open_send_admission().await.unwrap();
    assert_eq!(c.run_osdeploy_once(s.ids.operation(osdeploy_adapter::OsDeployStage::Clone)).await.unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied));
}
```

- [ ] Run (180s watchdog): `env DOCKER_CONTEXT=orbstack cargo test --offline --locked -p operation-controller --test postgres_osdeploy closed_start_cannot_activate_or_send -- --exact --nocapture --test-threads=1`. Expected RED: opened real path still refuses/no success; closed assertions must already hold.
- [ ] Implement controller-owned futures, not detached Tokio tasks: acquire an outer admission/activity guard and use nonblocking `try_acquire` on the worker semaphore before claim/resume; cap exhaustion yields Idle. Pin the24-second whole-call deadline before any await, retain grant only inside that call. A restarted controller observes/reaps durable leases rather than restoring memory grants or send permits. Waiting resumes only when due; Unknown only uses original-dispatch due reconciliation. Do not auto-poll terminal undispatched Unknown. Expose caller-driven due methods so the existing composition can use the one-second maintenance policy without introducing a daemon.
- [ ] Admission contains `lifecycle:tokio::sync::Mutex<()>`, `state:std::sync::Mutex<AdmissionState>`, `drained:tokio::sync::Notify`, and `closed:tokio::sync::watch::Sender<bool>`; private `AdmissionState` fields are `open:bool` and `in_flight:usize`. Hold the lifecycle mutex across the complete open/check or close/drain operation; never hold the state mutex over await. `try_enter` atomically refuses closed or increments; guard Drop decrements and notifies at zero. Register a `notified()` future before checking count to avoid lost wakeup. `open_send_admission` validates current owned Rust authority and only opens when no old activity remains; cap its complete mutex/check/open operation at24seconds. `close_and_drain` atomically closes first, signals queued-unsent owners, then waits for zero with `bound.min(24 seconds)`. Drain timeout leaves closed and returns TimedOut; no handoff claim. The bound includes acquiring the lifecycle mutex, so competing lifecycle callers cannot wait unboundedly. Gate guards never cross into postgres-store.
- [ ] Collection uses completed provisioning ports: node/storage/bridges/inventory, max32 identities, source/target config+power, exact deduplicated media storages, and original captured task only. Store `PveReadError` in `NativeRead`, never substitute expected facts; set partial coverage for failed/over-limit inventory, qga None for prefix. Bound each read at2s and total collection6s capped by remaining lease/scope, measured with monotonic elapsed time. Bind context to current revision and recollect after control-event CAS invalidation; do not rewrite evidence fence.
- [ ] For send, acquire nested send guard; recheck continuation; compute remaining conservatively from returned DB checked-at interval minus monotonic time since starting that check. Cap one consuming call by2s and whole call24s. Hold guard through receipt persistence or Uncertain result. On response, retain the capture/receipt pair and retry persistence at most twice after100ms/250ms only for StorageUnavailable, within original whole-call bound; do not require fresh execution authority to preserve an already returned original receipt. Any other error returns fixed classification. Gate close cancels queued unsent work; prefer completion/capture of already running calls, bounded by their existing deadline. On outer cancellation/timeout drop the owned future (never detach), leaving immutable dispatch uncertainty; every guard stays alive until its corresponding future is gone. No send is retried.
- [ ] Add real three-stage growth/no-growth controller runs, same-world controller reconstruction between stages, task-running park/new controller resume, response-loss/no resend, cancellation/authority change after dispatch-commit barrier before send, and paused submission during close/drain. Use existing `FakeControllerCheckpoint::DispatchCommitted` and `pause_provisioning_submission`; all pause waits are bounded and select against early worker completion. Test the gate lifecycle race, capture-storage failure bounds, current-thread runtime cancellation, unpolled future drop, unavailable preflight, identity overflow and observation timeout. Add `lease_status_roundtrip_delay_consumes_budget` and `client_wall_clock_shift_cannot_extend_local_deadline`: private pure budget conversion takes the returned static interval plus a supplied elapsed Duration, yielding saturating subtraction; the DB integration test delays check completion under an owned lock and proves the same original absolute endpoint is retained. No system clock change or public clock override is needed. Assert dispatch count≤1 per operation and no live send-capable future after successful drain. A failed drain must leave authority unchanged and gate closed.
- [ ] Run exact test GREEN; full new controller harness (600s watchdog), existing `postgres_native` (600s watchdog), and full durability harness (900s watchdog). Commit `feat(rust-controller): orchestrate osdeploy through owned one-shot sends`; independent review.

### Task 9: Complete verification, evidence handoff and continuing-goal accounting

**Files:** Modify only `rust-controller/evidence/poc-readiness-tracker.md` (repository-root-relative path) after evidence exists; task reports remain ignored SDD. Source corrections discovered here return to the owning reviewed task and receive fresh focused/full verification.

**Interfaces:** Consumes all reviewed tasks. Produces exact source SHA, complete command/count/platform evidence and explicit remaining sixteen-stage/service/callback/readiness gates; no production mutation capability or deployment authorization.

- [ ] Run formatting and strict checks from `rust-controller`:

```bash
cargo fmt --all -- --check
cargo clippy --offline --locked --workspace --all-targets --all-features -- -D warnings
cargo deny --offline --locked check --disable-fetch --show-stats
```

- [ ] Run the complete macOS owned-fixture suite with 1200s outer watchdog and no overlapping database suite:

```bash
env DOCKER_CONTEXT=orbstack cargo test --offline --locked -p postgres-store -p scheduler -p osdeploy-adapter -p operation-controller --all-features --no-fail-fast -- --nocapture --test-threads=1
```

Do not add `--include-ignored` on macOS to pretend Linux-only tests ran. Report cfg/platform omissions and repeated shared-helper harness executions separately. Record the preexisting unexplained native failure/interruption as historical unresolved assurance evidence, not a fix inferred from a later pass.
- [ ] Main prepares a fresh reviewed owned Linux artifact/gate at the final source SHA; the full runtime command must include ignored Linux-owned fault tests, preserve every original baseline assertion and include all new durability/controller tests. No reuse of an earlier frozen artifact as proof of this source. Runtime, exported artifact hashes, OCI configuration, cleanup/protected inventory and platform evidence remain separate acceptance claims.
- [ ] Update progress with concrete counts/SHA and name the reachable prefix as intermediate. State that service-driven callback/guest wiring, exact session arming, atomic PeComplete/grace activation, guarded stop, disk/guest/persistent heartbeat/QGA, independent-process restart/fault/restore/rollback and production single-writer readiness are not completed by this phase. Preserve no-production-cutover claim.
- [ ] Commit only the tracker path with `docs: record durable osdeploy prefix verification and remaining gates`. Main accepts reports and review results before dispatching the next callback/service milestone. No push/deployment is part of this plan.

## Self-review and spec coverage

Schema/immutability/additive migration→Task1; closed decoding/exact selected history→Task2; activation/cap/one-attempt/lease proofs→Task3; physical provenance and actual historical baselines→Task4; consuming permit/original capture→Task5; strict evaluation/waiting/no-change prefix→Task6; cancellation/reaper/reconciliation/repair/expiry progress→Task7; owned send/observation bounds/restart/drain→Task8; regression/Linux/evidence/full-goal truth→Task9. Future callback anchors remain closed rather than represented by a fake flag.

No first-three behavior depends on completing a later callback task. New API declarations in this draft have concrete types and owning tasks; their bodies are implementation work, not compile-failure tests. Each reviewable task requires a behavioral RED and GREEN and an exact-path commit after reviewable work, never a bulk unreviewed completion assertion.

Main final-plan review checks the task handoffs, exact SQL/wire expansion from the accepted schema and the synthetic-world tests against the complete spec, then verifies dispatch-time HEAD and authorization. The actual selector, migrate receiver, tracker path and concrete lifecycle serialization mechanism were checked during draft self-review. This planning task used `superpowers:writing-plans`; it performed no source edits, tests, builds, Docker/MCP/network calls, commits or child dispatch.
