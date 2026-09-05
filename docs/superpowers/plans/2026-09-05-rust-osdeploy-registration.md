# Durable OSDeploy Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist and independently reload the complete admitted sixteen-stage OSDeploy workflow atomically in the existing journal store, without exposing a generic scheduling bypass.

**Architecture:** Add validated restoration to osdeploy-adapter and an additive registration branch to the existing PgStore. Reuse the shared VM reservations, commands, operations and existing lock namespace. Registration is durable input, not execution: dedicated attempts, parked waits, PVE dispatch, callbacks and service integration follow on this exact manifest under the continuing full PoC goal.

**Tech Stack:** Existing Rust1.92 workspace, serde/serde_json, SQLx/PostgreSQL16, existing event-journal hashing and cached local proof helpers. macOSARM64 development and linuxAMD64 verification.

**Spec:** docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md sections5-9,13-14,18; approved OsDeploy contract and provisioning plans. Main decisions in .superpowers/sdd/2026-09-05-rust-provisioning-port/durable-osdeploy-main-checkpoint.md, callback-main-decisions.md and registration-compatibility-checkpoint.md bind this work. Proposals are supporting analysis, not override authority.

**Dispatch gate:** Preparation only while provisioning Linux source849766d is frozen. No task starts until main independently accepts that proof, ends its source freeze and supplies a clean baseline. User already selected Astra subagent execution; no new execution-choice prompt is needed.

## Global Constraints

- “Do not make Rust a generic consumer of arbitrary `cmd_json` commands.”
- “Only sanitized structural observations belong in fixtures or documentation. Raw VM inventories, tenant identifiers, credential references, and job payloads must not be copied into the repository.”
- “Duplicate key plus identical digest returns the existing command outcome.”
- “Duplicate key plus different digest becomes `conflicted` and performs no mutation.” In this registration API a conflict is a fixed error with transaction rollback, not a partial operation-state edit.
- “Attempts never replace the stable operation identity.” Registration creates no attempts, leases, authority rows, callbacks or dispatches.
- Operation IDs are UUIDv7, with existing semantic uniqueness `(workflow_kind,run_id,operation_key,contract_version)`; never deterministic UUIDv5.
- One existing orchestration authority and one VM identity reservation namespace. Keep old three-stage native contracts, serialization, migration files and evidence unchanged.
- Preserve all sixteen OsDeployStage::ALL keys, order, kinds and dependencies. Only PeEnsureStopped has the special grace dependency; registration does not grant that exception.
- Full workflow SHA, stage command SHA, optional PVE operation SHA and future request SHA are distinct. Shared native reservation plan_digest remains the NativeVmPlan digest.
- Real PVE and192.168.2.4 remain read-only. No helper/MCP/live endpoints/credentials/tenant actions/publication/deployment. Only explicitly owned local cached PostgreSQL test fixtures may mutate after dispatch; no external DSN, image pulls, resource pruning or source cleanup.
- One source implementer at a time, Astra, TDD with executed behavioral RED separated from missing-API compilation. Independent task review, final registration integration review and proportional platform proof. Preserve all SDD evidence.

## Files and interfaces

Task1 owns osdeploy-adapter/src/restore.rs, private restore/wire.rs if needed, lib.rs exports/fixed error, tests/restore.rs and tests/fixtures/plan-v1.json. The fixture is the exact independently hashed literal already in tests/plan.rs expected_snapshot; move that literal to the JSON fixture and have the existing test read it, preserving its independent constructor comparison and golden hash. No other old tests change.

Task2 owns postgres-store/src/osdeploy.rs and focused osdeploy/registration.rs, osdeploy/records.rs, osdeploy/stage.rs; lib.rs/Cargo.toml exports/dependency; migrations/0004_osdeploy_registration.sql; store.rs migration/raw-intake guard; scheduler.rs generic guards; native.rs explicit cross-family conflict; tests/osdeploy_registration.rs and tests/osdeploy_support/mod.rs. Focused existing test adjustments are scheduler/tests/postgres.rs per-kind-cap fixture and postgres-store/tests/native.rs historical-upgrade fixture. No service, controller, API route or PVE implementation changes.

New store dependency is osdeploy-adapter path only; preserve no reverse adapter→store or pve→adapter dependency. Cargo.lock may change only the local package dependency edge; no package versions or remote dependency additions.

### Task 1: Named, fully validated plan restoration

**Produces:**

```rust
pub fn restore_osdeploy_plan_v1(
    canonical_json: &str,
    expected_workflow_sha256: &str,
) -> Result<OsDeployPlanV1, ContractError>;
// Add fixed ContractError::InvalidPersistedPlan:
// "persisted deployment plan is invalid"
```

- [ ] Create tests/fixtures/plan-v1.json from the existing exact expected_snapshot literal and change that one fixture reader to `serde_json::from_str(include_str!("fixtures/plan-v1.json")).unwrap()`. Keep golden47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9 and all existing constructor comparisons unchanged.
- [ ] Add tests/restore.rs. Start with these actual roundtrip and duplicate-admission assertions; add compiling minimal restoration that fails intentionally before the real decoder so RED is behavioral:

```rust
use osdeploy_adapter::{restore_osdeploy_plan_v1, ContractError};
const PLAN: &str = include_str!("fixtures/plan-v1.json");
const SHA: &str = "47035f6731b8a27dceabbe9fb2ff63aa952204dfa3b6420d94a9a35c1fe3e5a9";
#[test]
fn restores_complete_admitted_plan() {
    let plan = restore_osdeploy_plan_v1(PLAN, SHA).unwrap();
    assert_eq!(plan.fingerprint().unwrap(), SHA);
    assert_eq!(serde_json::to_value(plan).unwrap(), serde_json::from_str::<serde_json::Value>(PLAN).unwrap());
}
#[test]
fn rejects_duplicate_fields_before_value_normalization() {
    let duplicated = PLAN.replacen("\"contract_version\": 1", "\"contract_version\": 1, \"contract_version\": 1", 1);
    assert_eq!(restore_osdeploy_plan_v1(&duplicated, SHA), Err(ContractError::InvalidPersistedPlan));
}
```

- [ ] Run `cargo test --offline --locked -p osdeploy-adapter --test restore` and record actual failed assertions, not only compilation errors.
- [ ] Reject text above65,536UTF8bytes before parsing, invalid/lowercase-noncanonical expectedSHA (exact64lowercasehex), trailing JSON, nonobject/positional objects, unknown/missing/duplicate keys at every nested level, missing required nulls, nonstring discriminators and malformed values with the one fixed error. Whitespace and key order are immaterial; altered field values are not. Detect duplicate keys while reading text, before building a Value. Use bounded existingserde recursion; never disable recursion limits.
- [ ] Decode private wire structures with required fields and deny_unknown_fields. Object-only nested decoding must also wrap reused NativeVmPlan; its legacy decoder cannot broaden the new persisted shape. String-only fields include fixed constants and artifact image_index_source. Required nullable fields are artifact.output_image_index and each payload.sha256. Do not derive Deserialize on admitted outputs.
- [ ] Reconstruct using NativeVmPlan's validated decoder, DeploymentNames::new, DeclaredArtifact::new(DeclaredArtifactInput), DiskCapacity::new, PhasePolicy::new, PayloadDeclaration::new and OsDeployPlanV1::new. Recompute serialization and compare the entire normalized original object with the reconstructed object, then compare its recomputed fingerprint with expectedSHA. Matching two caller-supplied hashes is insufficient. All16fixed fields and nested derived values participate. No lookup of artifact bytes, secrets or callback profiles.
- [ ] Add independent mutation tables for all35top-level keys and nested keys: duplicate/missing/unknown/array shapes; fixed constants; name normalization; effective/growth capacity; artifact apply index/source/evidence labels; payload hash/null/evidence labels; identity/version/resource validation; wrong fullSHA even when allstructural fieldsvalid. For altered invalid-derived fields also recompute the inputJSONdigest and prove restoration stillrejects. Canonical valid source-fallback artifact/nullpayload, nonintegral retained diskcapacity and boundary phasepolicy must roundtrip. Preserve broad21byteserial restoration; only executable downwardconversion/registration rejects it.
- [ ] Verify payload-free errors with a secret sentinel, requirednull rejection at each nested nullable, size bound and trailingdata. Existing admitted-type compile-fail noDeserialize tests remain passing.
- [ ] Run full osdeploy-adapter tests/docs, focused formatting, then workspacefmt and strict offline locked alltargets/allfeaturesClippy. Commit exact paths with `feat(osdeploy-adapter): validate persisted deployment plans`; write task report with RED/GREEN, actual counts, sourceSHA and unchanged artifact/service boundary. Main reviews before Task2.

### Task 2: Atomic registration, complete reload and generic-family isolation

**Consumes:** Task1 restore_osdeploy_plan_v1; OsDeployPlanV1, OsDeployStage::ALL/kind/dependency, pve_expectations; existing CommandEnvelope/SemanticOperationKey and PgStore::append_command_tx.

**Produces:** Private-field, Clone/Debug/Eq/PartialEq outputs with getters, no Deserialize or caller-selected operation constructors:

```rust
pub enum OsDeployStoreError { Validation, Conflict, StorageUnavailable }
pub struct OsDeployWorkflowIds {
    run_id: RunId,
    operations: [OperationId; 16],
    workflow_sha256: String,
}
// run_id()->RunId; operation(stage:OsDeployStage)->OperationId;
// operations()->&[OperationId;16]; workflow_sha256()->&str.
pub struct OsDeployOperationPlanV1 {
    contract_version: u16,
    workflow_sha256: String,
    stage: OsDeployStage,
    pve: Option<ProvisioningOperationPlanV1>,
}
// derive(&OsDeployPlanV1,OsDeployStage)->Result<Self,OsDeployStoreError>;
// fingerprint()->Result<String,OsDeployStoreError>; borrowing getters.
pub struct OsDeployRegistrationV1 {
    ids: OsDeployWorkflowIds,
    plan: OsDeployPlanV1,
    stages: [OsDeployOperationPlanV1;16],
}
// ids()->&OsDeployWorkflowIds; plan()->&OsDeployPlanV1;
// stage(OsDeployStage)->&OsDeployOperationPlanV1.
impl PgStore {
    pub async fn enqueue_osdeploy(&self, run:RunId, plan:&OsDeployPlanV1)
        -> Result<OsDeployWorkflowIds,OsDeployStoreError>;
    pub async fn load_osdeploy_registration(&self, run:RunId)
        -> Result<OsDeployRegistrationV1,OsDeployStoreError>;
}
```

Fixed errors: "OSDeploy registration validation failed", "OSDeploy registration conflict", "OSDeploy registration storage unavailable". Unique violations→Conflict, unavailableDB→StorageUnavailable, malformedpersistedrows→Validation; never include raw SQL or payload. Add fixed public StoreError::TypedWorkflowRequired for rejected rawOSDeploycommands without changing prior validation order.

- [ ] Create pure stage mapping/hash tests before DB work. Exact mapping: Clone→Clone, DiskCapacity→EnsureCapacity, ConfigurePe→ConfigurePe, StartPe→StartPe, PeEnsureStopped→EnsureStopped, ConfigureDisk→ConfigureDisk, StartDisk→StartDisk; other9stages pve=None. Stage fingerprint is canonical JSON of exactly contract_version1,workflow_sha256,stage,pve (requirednull). PVEplan derives only via pve_expectations and fullplan; no callerhashinput. Pin literal golden stage hash and prove nonPVEinput changes allstagehashes but not PVEsubset.
- [ ] Add migration0004 with three immutable tables, using current reject_native_mutation function for UPDATE/DELETE andTRUNCATE guards without altering0003:

```text
osdeploy_runs: run_id PK/FK native_vm_reservations(run_id), contract_version=1,
workflow_sha256 lowercase64hex, plan_canonical_json text (object,<=65536octets),
created_at timestamptz default clock_timestamp().
osdeploy_operation_plans: operation_id PK/FK operations, run_id FK osdeploy_runs,
ordinal smallint0..15, stage closed16snake_case names, predecessor_id nullable,
dependency_kind intake/satisfied/grace_gate, command_sha256 lowercase64hex,
pve_plan_sha256 nullablelowercase64hex.
UNIQUE(run_id,ordinal), UNIQUE(run_id,stage), UNIQUE(run_id,operation_id).
Composite(predecessor_id,run_id) FK(operation_id,run_id), no selfpredecessor.
osdeploy_agent_reservations: expected_agent_id text PK, run_id UNIQUE/FKosdeploy_runs,
created_at timestamptz default clock_timestamp().
```

Ordinal/stage/dependency/PVEpresence exact consistency must be checked both DB constraints where rowlocal and reload. Stage0hasnullpredecessor/intake; stage7referencesstage6/grace_gate; everyotherstage referencesprevious/satisfied. DBtextJSONcheck does not claim duplicate detection; the named textdecoder does. No attempts/deadlines/dispatch/callback placeholdertables in this migration.
- [ ] Pair migration entry with genericisolation in this same task before any public registration exposure. claim_next/claim_next_bound returnNone for allOsDeploy after existingcap/bindingvalidation. Generic start/cancel/finalize rejectOsDeploy; continuation/heartbeat rejectit before state/lease writes. Generic reaper excludesOsDeploy in discovery and rechecks family underlocks. Oldnative behavior unchanged. Publicappend_command rejectsOsDeploy after existingdigest/versionchecks and beforeDBwrites; internalappend_command_tx remains private for atomicregistration. No publicbypassboolean. Genericappend_event remainsEvidenceRecordedonly and cannot causeexecution.
- [ ] Registration validates/serializes/restores fullplan and downwardconversion before any DB transaction writes, including desired diskserial<=20. Begintransaction, acquire existingnative:run lock, then existing sorted VMID/UUID/MACidentitylocks and an additional sorted agentidentitylock. Never introduce inverse lock order with oldnative. Use canonical agentlabel and matching existing VMreservationNativeVmPlandigest.
- [ ] Same run+samecompleteplan reloads and validates exactly16existingIDs and returns them. Changedfullplan, runalreadyownedbyoldnative/otherfamily, VMID/UUID/MACcollision or reservedagent collision returnsConflict with fullrollback. Update old enqueue_native_vm to explicitly returnConflict for OSDeploy-owned run evenwhenVMdigestmatches. No automatic release/reassignment on cancellation or timeout.
- [ ] Allocate16OperationId::new() values only for freshregistration. Derive allsemantic keys via manifest, workflowOsDeploy,version1. Idempotencykey is `osdeploy:{run_uuid}:{operation_key}`; commanddigest is stagefingerprint. Append commands with existingprivatehelper, store immutablefullplan/rows/reservation in same transaction. All16operationsPending/revision0, noattempt/lease/dispatch, zerojournal/outbox writes just as existing commandintake; do not claim otherwise. Deferred FK or insertionorder must keep transactionvalid, with no partialvisiblemanifest.
- [ ] Reload in REPEATABLE READ READ ONLY transaction: restore canonicaltext+fullhash, downwardconversion, complete16operation/command/stagerows, exactsemantic keys/UUIDv7 IDs/digests/ordinal/dependencies/PVEmapping, nativeVMreservation and globalagentreservation. No extra rows for run, crossfamily adoption or partialgraph. Recompute everydigest; never deserialize an authoritywrapper. Registration reload must remainvalid when laterauthorized execution changes operationstate; do not pinPending as immutableplanmetadata.
- [ ] Build tests on explicitly owned cached localPG fixtures through existingproof_support, noexternalDSN. Core admission assertions:

```rust
let first = fixture.store.enqueue_osdeploy(run, &plan).await.unwrap();
let again = fixture.other.enqueue_osdeploy(run, &plan).await.unwrap();
assert_eq!(first, again);
assert_eq!(first.operations().len(), 16);
let loaded = fixture.store.load_osdeploy_registration(run).await.unwrap();
assert_eq!(loaded.plan(), &plan);
assert_eq!(loaded.ids(), &first);
for stage in OsDeployStage::ALL {
    assert_eq!(loaded.stage(stage).stage(), stage);
}
assert!(fixture.scheduler().claim_next(WorkflowKind::OsDeploy, 1).await.unwrap().is_none());
```

Define fixture in tests/osdeploy_support/mod.rs using existing localPostgresContainer ownership/setupbounds and PgStore::migrate, with two independent pools and scheduler generation1. Plan comes from Task1fixedJSON+goldenrestoration. Expose testonly counts for16operations/16commands/1VMreservation/16stageplans/1run/1agentreservation/0attempts/0leases/0events/0outbox; assertion helpers compare exact counts before/after rejected calls. No sharedproductionconfiguration.
- [ ] Execute behavioralRED for registration/reload/isolation separately. Test concurrentduplicate registration returnsoneIDset; changednonPVEfullinputconflicts despite sameVM/PVEhash; sameVMID/UUID/MAC or normalizedagent collisions across differentruns; old/newfamilyconflictsbothdirections; invalid21byteserialnoinsert; canonical20bytesucceeds; repeatedmigration/nohistorymutation; rollbackonlate commandcollision; missing/extra/wrongordinal/stage/key/dependency/hash/reservation reloadrejects. Corruption tests may modify ownedtestDB only via explicit fixtureguard; never add productionrepair API or disableimmutability in normalstorepaths.
- [ ] Test rawappend/claim/start/cancel/finalize/heartbeat/continuation/reaper failclosed with no writes, including forged oldgenericOsDeploy attempt/lease fixture. Preserve cap0 rejection and supportedgeneric families. Update per-kindcaptest to TaskSequence as unrelatedfamily, retain everycapassertion, addOsDeployspecifictest. Historicalupgradefixture must seed prior-schema rows explicitly instead of using newlyforbiddenpublicintake; verifyallfourpriorfamilyrecords survive/read unchanged and oldOsDeploycannotexecute. Existing three-stagenative registration/dispatch/recovery tests staypassing.
- [ ] Run focused tests during work, then all postgres-store and scheduler tests plus osdeploy-adapter tests/docs, affectedoperation-controllerdecision/nativeproof suites, workspacefmt and strictalltargets/allfeaturesClippy, cachedofflinepolicy. DBtests useonlyownedcachedfixtures with one testthread; nooverlappingimagebuild. Report exact runnable/skipped counts and resourcespreserved. Main coordinates LinuxDBproportionategate separately rather than claiming MacDBsuccesscoversLinux.
- [ ] Commit exacttaskpaths with `feat(postgres-store): register guarded OSDeploy workflows`; preserveoldmigrationfiles, untrackeduserchanges and SDD. ReportfullAPI/SQLcontract, RED/GREEN/counts, sourceSHA, warnings and remaining executiongates. Main independentreview then fullregistrationintegrationreview.

## Full-goal continuation after this deliverable

Registration is not the fullverticalslice and is not a productioncandidate. Next implementation consumes this exactmanifest into durable same-attempt/absolute-deadline parking, sevenPVEdispatch/receiptreload and historicalownership, guardedgrace-stop, compatible callback/guestclaims/results/runtimeheartbeat, existingservice intake/dueworkers/shutdown, independentfakeprocessrestart and fullsixteenstageproof. Agent/build-host/CloudOSD/legacyWinPE, Python/Rustsinglewriterfencing, remainingcompatibility/differential/fault/restore/rollback and immutablecandidate/readinessassessment remain mandatory under the originalgoal. Production/nonproduction externalmutation gates remainseparate.

## Main self-review gate

Main checked Task1 against the current complete35field plan, artifact/input constructors and independent golden fixture. Task1 is implementation-ready after accepted Linux gate0fd33da; sourcefreeze ended. Its output/error/function names are pinned above. Before Task2 dispatch main must finalize its concrete fixture/error/getter/SQL contract against acceptedTask1, with a complete task brief and actual testhelper definitions. The interfaces and constraints above are fixed; this is a staged consistency gate, not permission to invent alternate executionauthority. No Task2 implementation is authorized by Task1 dispatch.
