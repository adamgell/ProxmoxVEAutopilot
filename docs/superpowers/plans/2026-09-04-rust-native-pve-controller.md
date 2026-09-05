# Rust Native PVE Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md`

**Goal:** Prove a durable, locally executable Rust workflow that preflights, reserves one VM identity, clones a template, configures that VM, and starts it against an isolated fake PVE, including ambiguous-result reconciliation without blind mutation replay.

**Architecture:** Add typed PVE contracts, additive native intent persistence, and an `operation-controller` crate on the existing journal and fenced scheduler. Each clone, configure, and start is a separate stable operation with immutable input, its own attempt, and authoritative postconditions. The public controller service continues rejecting native mode; the new executable proof owns an in-memory fake and cannot select a real mutation transport.

**Tech Stack:** Rust 1.92 / edition 2024, Tokio, async-trait, serde, reqwest/rustls for observation only, SQLx, PostgreSQL 16, Axum-compatible loopback read fixtures, existing canonical JSON hashing and Docker proof infrastructure.

---

## Global Constraints

Implement in the isolated worktree, starting from the reviewed foundation source `a7d9443`. Recheck the actual starting SHA and working tree before edits; a refreshed artifact does not change the source contract. Do not modify the foundation review/evidence directory. This plan was prepared from local sources only, as requested; no tunnel status, MCP call, production observation, or infrastructure action was performed for planning.

The approved [design](../specs/2026-09-04-rust-controller-replacement-design.md) sections 8–10 and 15–18 govern this slice. It advances sequence items 7 and part of 8. Its acceptance result is **a fake VM with the intended configuration and observed running state**, not an installed OS or ready endpoint.

All production systems, including controller `192.168.2.4` and every real PVE endpoint, remain read-only. This phase makes **zero live PVE requests**, including during automated tests. Authenticated read support is library code tested only against owned loopback servers; adding that capability is not authorization to use it remotely. No SSH, root ticket, arbitrary shell, `cmd_json`, Graph/Entra, deployment, queue claims on the production database, or live mutation enable flag belongs in this work. RustedOutClient remains deferred.

### Decisions that bound this slice

- Use a full clone from one stopped, unlocked template, one target node, one target storage, one `scsi0` boot disk, and one `net0` VirtIO NIC. Preflight rejects unsupported source layouts, missing facts, QEMU `args`, inherited CD-ROMs, or additional NICs. This is an explicit synthetic contract; it does not claim to implement the production Windows template family.
- The final desired fields are SMBIOS UUID, the exact single MAC, bridge, CPU core count, memory MiB, QGA enabled, and `boot=order=scsi0`. No disk resize, media change, firmware/TPM change, power stop/reset, or deletion is exposed.
- The input specifies VMID, UUID, MAC and a synthetic `cluster_key`. There is no `nextid` allocation, VMID collision hopping, or replacement resource creation. Persist a reservation before the first clone request, and retain it indefinitely in this slice.
- PVE can assign UUID/MAC during clone. Clone completion first binds a **fresh intermediate identity** to a matching successful clone UPID, exact reserved node/VMID/name, observed resource set and a unique persisted operation marker returned in fake provenance evidence. The marker binds the clone operation, source VMID, target VMID and request digest; matching VMID/name/task success alone never establishes ownership. Configure changes that bound intermediate identity to the final requested identity. A final-UUID comparison alone cannot prove the clone stage. Do not weaken the existing `observe_clone_outcome` evaluator to accommodate this distinction; introduce separate native evidence types. The fake marker is not an assertion that the live PVE clone API supports such a field.
- If the clone response is lost, an occupied target is insufficient ownership proof. Record `unknown`, observe, and preserve it. Do not configure the occupant, infer ownership from its name, or issue a second clone. A subsequently recovered matching UPID may support reconciliation; a guessed task identifier may not.
- At most **one mutation submission per operation** is allowed in this slice. The design's two-attempt ceiling is an upper bound, not a requirement to retry automatically. Observation retries are bounded; mutation retry authorization and new attempts from `unknown` are deferred. Cancellation prevents new sends; an already dispatched operation remains unknown until a fenced reconciliation establishes its outcome.
- Existing `Unknown` is terminal in `decide_transition`. Add a narrow scheduler-owned reconciliation transaction for native unknown operations. Do not add `Unknown -> Satisfied` to the general domain transition function or loosen `PgStore::append_event` beyond `EvidenceRecorded`.

## File and API map

Paths below are relative to the repository root. Existing adapter files and Python contracts are source references, not implementation targets.

| Files | Responsibility |
|---|---|
| `rust-controller/crates/pve-port/src/credentials.rs`, `preflight.rs` | Validated, redacted observation credentials; typed node/storage/bridge/resource/config/status facts |
| `rust-controller/crates/pve-port/src/observer.rs`, `model.rs`, `lib.rs` | Extend observation routes and exports; preserve verified TLS, no redirects/proxies/retries |
| `rust-controller/crates/pve-port/src/native.rs`, `native_fake.rs` | Immutable native plans, exact request encoders, sealed fake mutation capability and scripted fake outcomes |
| `rust-controller/crates/pve-port/tests/{credentials,preflight,native_contract}.rs` | Loopback read authentication and contract tests; fake mutation tests |
| `rust-controller/crates/controller-domain/src/{command,id}.rs` | Add `PveClone`, `PveConfigure`, `PveStart`, and workflow `NativePveVmBoot` |
| `rust-controller/crates/postgres-store/migrations/0003_native_pve.sql` | Additive plan/reservation/dispatch/receipt/decision persistence and workflow CHECK extension |
| `rust-controller/crates/postgres-store/src/native.rs`, `src/scheduler/native.rs` | Transactional intake, typed reads, fenced dispatch and reconciliation |
| `rust-controller/crates/postgres-store/src/{lib,store,scheduler}.rs` | Module wiring, private transaction helper reuse, native workflow encoding and exclusions |
| `rust-controller/crates/postgres-store/tests/native.rs` | PostgreSQL 16 native transaction, replay, race and recovery tests |
| `rust-controller/crates/operation-controller/Cargo.toml`, `src/{lib,decision,controller}.rs` | Pure evaluation and fake-only orchestration |
| `rust-controller/crates/operation-controller/tests/{decision,postgres_native}.rs` | Decision matrix, durable end-to-end and fault proofs |
| `rust-controller/crates/operation-controller/examples/native_fake.rs` | One bounded executable local proof with no configurable PVE destination |
| `rust-controller/crates/controller-service/tests/service.rs` | Regression that native/real transport startup remain unavailable |
| `rust-controller/{Cargo.lock,README.md}`, `rust-controller/crates/{pve-port,postgres-store}/Cargo.toml` | Dependencies and accurate local proof instructions |

Use the workspace's existing dependency versions. `postgres-store` gains `serde.workspace = true` and `pve-port = { path = "../pve-port" }`; `pve-port` has no store dependency, so this direction creates no cycle. Do not create the other future crates merely because the approved architecture lists them.

### Task 1: Typed authenticated reads and preflight facts

**Interfaces:** Consumes validated `PveBaseUrl` and optional `PveApiToken`; produces typed `PvePreflightReadPort` snapshots and fixed read errors. No scheduler or mutation capability is constructed.

**Files:** Create the credentials/preflight modules and their tests; modify `pve-port/src/{lib,model,observer}.rs` and its manifest only as required.

- [ ] **Step 1: Pin credential and target behavior with failing tests.**

In `tests/credentials.rs`, use this exact synthetic credential test:

```rust
use pve_port::PveApiToken;

#[test]
fn token_validation_and_debug_never_echo_input() {
    let token = PveApiToken::parse("observer@pve!local-proof", "synthetic-secret-123").unwrap();
    let debug = format!("{token:?}");
    assert_eq!(debug, "PveApiToken(<redacted>)");
    for (id, secret) in [
        ("observer@pve!local-proof\r\nInjected: yes", "valid"),
        ("observer@pve!local-proof", "secret\nvalue"),
        ("observer@pve!local-proof", ""),
        ("observer@pve!local-proof", "contains space"),
        ("observer@pve!local-proof", "value=other"),
        ("observer@pve!local-proof", "non-ascii-λ"),
        ("observer@pve!one!two", "valid"),
        ("observer!token", "valid"),
    ] {
        let error = PveApiToken::parse(id, secret).unwrap_err();
        assert_eq!(error.to_string(), "invalid PVE API token");
    }
}
```

Add loopback tests that capture exactly one request and assert its Authorization header is `PVEAPIToken=observer@pve!local-proof=synthetic-secret-123`; no URL, query, form or debug output contains that secret. Test 302 to a second loopback listener: the second listener receives zero requests and the first result is unavailable. Reuse the bounded listener pattern from `pve-port/src/lib.rs`; never echo a captured header in assertion failures.

Run `cargo test --manifest-path rust-controller/Cargo.toml -p pve-port --test credentials`. Expected: compile failure for the missing typed token API.

- [ ] **Step 2: Add the typed credential container and optional observer injection.**

Implement `credentials.rs` as follows; export only the token and fixed error, keeping the header accessor crate-private:

```rust
use std::fmt;
use reqwest::header::HeaderValue;
use thiserror::Error;

#[derive(Clone)]
pub struct PveApiToken(HeaderValue);

#[derive(Clone, Copy, Debug, Error, Eq, PartialEq)]
#[error("invalid PVE API token")]
pub struct InvalidPveApiToken;

impl PveApiToken {
    pub fn parse(id: &str, secret: &str) -> Result<Self, InvalidPveApiToken> {
        fn component(value: &str) -> bool {
            !value.is_empty() && value.len() <= 128
                && value.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        }
        let (user_realm, token) = id.split_once('!').ok_or(InvalidPveApiToken)?;
        let (user, realm) = user_realm.split_once('@').ok_or(InvalidPveApiToken)?;
        if !component(user) || !component(realm) || !component(token)
            || secret.is_empty() || secret.len() > 4096
            || !secret.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        {
            return Err(InvalidPveApiToken);
        }
        let mut header = HeaderValue::from_str(&format!("PVEAPIToken={id}={secret}"))
            .map_err(|_| InvalidPveApiToken)?;
        header.set_sensitive(true);
        Ok(Self(header))
    }

    pub(crate) fn header(&self) -> HeaderValue { self.0.clone() }
}

impl fmt::Debug for PveApiToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("PveApiToken(<redacted>)")
    }
}
```

Add `token: Option<PveApiToken>` to `PveObserverConfig` and `ReqwestPveObserver`, defaulting to `None`, and a consuming `with_api_token(self, token) -> Self` builder. Attach the sensitive header only within `request_data`. The token has no `Serialize`, `Deserialize`, `Display`, public header accessor, environment parser, or CLI flag. The existing observer target gate executes before client construction; preserve that order. This deliberately narrow v1 token grammar fails closed on unsupported credentials.

- [ ] **Step 3: Add typed read contracts and exact preflight parsing.**

Add a separate `PvePreflightReadPort: PveReadPort` trait with async methods `node_status(&NodeName)`, `storage_status(&NodeName, &StorageName)`, `bridges(&NodeName)`, `cluster_vms()`, `native_vm_config(&NodeName, Vmid)`, and `vm_status(&NodeName, Vmid)`. Return named typed snapshots `NodeStatus`, `StorageStatus`, `BridgeInventory`, `ClusterVmInventory`, `NativeVmConfig`, `VmPowerStatus`; every successful snapshot includes `observed_at: DateTime<Utc>`. Give each snapshot a validated constructor and read-only accessors; derive serialization for sanitized facts but deserialize through validated wire conversion.

Define the following exact wire-to-fact contract in `preflight.rs` and implement it in `ReqwestPveObserver`:

| Request under `/api2/json` | Required fact fields and validation |
|---|---|
| `GET /nodes/{node}/status` | Valid response object and positive `uptime`; `NodeStatus { online: true }` only on a valid response |
| `GET /nodes/{node}/storage/{storage}/status` | `active=1`, `enabled=1`, nonnegative `avail`, `content` containing `images`; unknown/malformed values fail parsing |
| `GET /nodes/{node}/network` | Only `type=bridge` entries with validated interface names; require the requested bridge to be present and active |
| `GET /cluster/resources?type=vm` | Validate VMID, node, type `qemu`, template flag, name and power status; reject duplicate VMID records and malformed entries |
| `GET /nodes/{node}/qemu/{vmid}/config` | Required nonempty `digest`, `name`, `cores`, `memory`, `scsi0`; parse UUID, exact `netN` entries, bridge, agent, boot, template and lock; reject malformed identity fields, duplicate keys in comma properties, unknown power-relevant `args`, missing required values |
| `GET /nodes/{node}/qemu/{vmid}/status/current` | Returned VMID equals requested VMID; `status` exactly `running` or `stopped`; explicit `locked` state is preserved where available |

For the resource query, add the fixed query parameter via the URL builder; do not let callers supply query text. The single NIC contract accepts only `net0=virtio=<validated MAC>,bridge=<validated bridge>` plus known inert values `firewall=0` and optional `tag` only if the plan explicitly supports it; v1 plans do not support VLAN tags, so reject tagged source/config fixtures. `boot` must be exactly `order=scsi0` for postcondition satisfaction. Normalize `agent=1` and `enabled=1,type=virtio` to enabled. Store presence of unsupported config as a typed rejection reason, never raw config text in evidence.

Freshness uses a clock sampled after all preflight reads. Accept only `observed_at <= as_of` and age at most 30 seconds. A missing target is an authoritative absence only when a fresh valid cluster inventory excludes its VMID **and** target config returns `NotFound`; a 404 alone is not proof. Read errors are classified using existing fixed `PveReadError` variants. Node/storage/bridge/VM identities come from validated request and response fields, not string interpolation.

Add tests in `tests/preflight.rs` for valid envelopes, wrong VMID/node, duplicate VMIDs, stale/future timestamps, missing digest, malformed MAC/UUID, locked template, inactive storage, missing bridge, 401/403/404/409/500, body decode failure and timeout. Each fixture server binds a literal loopback ephemeral port and owns its complete lifecycle.

- [ ] **Step 4: Verify and commit this task.**

Run `cargo test --locked --manifest-path rust-controller/Cargo.toml -p pve-port`. Expected: all existing observer/network tests and new credential/preflight tests pass. Run formatting and Clippy for `pve-port` with warnings denied. Commit only this task's files as `feat(rust): add typed authenticated PVE preflight reads`.

### Task 2: Immutable native operation plans and fake mutation capability

**Interfaces:** Consumes the typed identifiers/read models from Task 1; produces immutable `NativeVmPlan`/`NativeOperationPlan`, exact typed requests, `NativeFakePve` and a sealed `PveMutationPort`.

**Files:** Create `pve-port/src/{native,native_fake}.rs` and `tests/native_contract.rs`; modify domain enum files, `pve-port/src/lib.rs`, required manifests, and existing exhaustive workflow encoding/decoding matches in `postgres-store` so the workspace remains buildable. Native intake and scheduler routing remain Task 4 work.

- [ ] **Step 1: Write contract rejection and request-shape tests.**

Use serde JSON inputs built from the synthetic values below. Test that unknown fields `cmd`, `args`, `url`, `token`, `delete`, `ssh`, `new_vmid` and `extra_parameters` fail decoding; zero/out-of-range numeric values and invalid names fail through constructors. Changing any mutation-relevant field changes `event_journal::payload_digest` of the serialized plan. Add a dev dependency on `event-journal` for this digest test only.

Pin the exact operation payload shapes:

```json
{
  "contract_version": 1,
  "cluster_key": "fake-local",
  "node": "pve-test",
  "source_vmid": 9000,
  "target_vmid": 9010,
  "name": "native-proof-9010",
  "storage": "local-lvm",
  "bridge": "vmbr0",
  "uuid": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
  "mac": "02:00:00:00:90:10",
  "cores": 2,
  "memory_mib": 2048,
  "minimum_storage_bytes": 17179869184
}
```

Call this type `NativeVmPlan`; its private fields have read-only getters. Enforce version 1, distinct source/target VMIDs, core range 1–128, memory range 512–1048576 MiB divisible by 128, storage budget greater than zero, and names restricted to nonempty ASCII letters/digits/hyphen with a 63-byte ceiling. `cluster_key` is a namespace key with the same grammar; it is never a URL.

Run `cargo test --manifest-path rust-controller/Cargo.toml -p pve-port --test native_contract`. Expected: missing native types/tests fail before implementation.

- [ ] **Step 2: Add native operation schemas and exact encoders.**

Add the domain enum variants and export these native types from `pve-port`:

```rust
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeStep { Clone, Configure, Start }

impl NativeStep {
    pub const fn operation_key(self) -> &'static str {
        match self {
            Self::Clone => "pve.clone.v1",
            Self::Configure => "pve.configure.v1",
            Self::Start => "pve.start.v1",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct NativeOperationPlan {
    step: NativeStep,
    vm: NativeVmPlan,
}
```

`NativeOperationPlan::new(step, vm)` accepts only a validated `NativeVmPlan`; its custom deserializer denies unknown fields and calls the same constructors. Provide `step()` and `vm()` accessors. `NativeVmPlan` deserialization uses `#[serde(deny_unknown_fields)]` on its private wire struct. The common plan contains no attempt, UPID, observed digest or credentials; those are later durable facts, so retry/recovery never changes the plan hash.

Define `CloneRequest`, `ConfigureRequest`, `StartRequest` with private fields and constructors from the validated plan and proven intermediate identity. `CloneRequest` additionally carries a fresh `request_marker: Uuid` and `operation_id: OperationId` as fake-only request metadata included in its canonical request digest. Persist both before sending. The marker is not a live PVE form/header parameter. Do not expose a generic method/path/body request API. Exact encoders return these fixed method, path-segment and form pairs for fake request inspection:

| Step | Method and route | Exact encoded fields |
|---|---|---|
| Clone | `POST nodes/{node}/qemu/{source_vmid}/clone` | `newid`, `name`, `full=1`, `storage` |
| Configure | `PUT nodes/{node}/qemu/{target_vmid}/config` | fresh `digest`, `smbios1=uuid={uuid}`, `net0=virtio={mac},bridge={bridge},firewall=0`, `cores`, `memory`, `agent=enabled=1,type=virtio`, `boot=order=scsi0` |
| Start | `POST nodes/{node}/qemu/{target_vmid}/status/start` | empty form |

Configure never includes `delete`, `args`, media, disk, firmware or arbitrary keys. Its `digest` is the exact last authoritative config digest, not the plan digest. A failed config digest check becomes conflict/unknown observation; never silently refresh and resend. The inherited boot disk is checked and preserved.

- [ ] **Step 3: Implement a sealed mutation trait backed only by a fake.**

```rust
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PveWriteError { Unauthorized, Conflict, Rejected, OutcomeUnknown }

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MutationReceipt { Task(crate::Upid), SynchronousAccepted }

mod sealed { pub trait FakeMutationCapability {} }

#[async_trait::async_trait]
pub trait PveMutationPort: sealed::FakeMutationCapability + Send + Sync {
    async fn clone_vm(&self, request: &CloneRequest) -> Result<MutationReceipt, PveWriteError>;
    async fn configure_vm(&self, request: &ConfigureRequest) -> Result<MutationReceipt, PveWriteError>;
    async fn start_vm(&self, request: &StartRequest) -> Result<MutationReceipt, PveWriteError>;
}
```

Implement that trait only for `NativeFakePve`. It also implements `PveReadPort` and `PvePreflightReadPort`, maintains a typed VM inventory, and records typed requests. `NativeFakePve::new()` has no URL, credential, HTTP-client or process parameter. Script each step using `FakeMutationOutcome::{Accepted, Rejected(PveWriteError), AppliedResponseLost, AcceptedTaskFails, AcceptedTaskDelayed}`; these variants change fake state and return the documented receipt/error, enabling tests to distinguish response loss from rejection. Configure returns `SynchronousAccepted`; clone/start return validated synthetic UPIDs with worker types `qmclone`/`qmstart`. The fake rejects the wrong config digest and an occupied VMID itself, independently of controller preflight. On clone it stores `FakeCloneProvenance { operation_id, request_marker, source_vmid, target_vmid, request_digest }` alongside the created VM, returns that typed provenance in subsequent native config observations, and preserves it through configure/start. The HTTP observer always returns no fake provenance. A missing/mismatched marker yields unknown/conflicted, including a foreign VM with an identical name and a superficially matching task result. Test those cases explicitly. Future real transport requires a separately verified provenance mechanism before this evaluator can authorize configuration of a real clone.

The trait's private supertrait is intentional: downstream crates cannot turn a production HTTP client into a mutation capability. Add a compile-fail doc test proving a consumer cannot implement `PveMutationPort` for `ReqwestPveObserver`. There is no HTTP mutation implementation, network address knob, unsafe escape hatch or feature that removes sealing in this phase.

- [ ] **Step 4: Verify and commit this task.**

Run `cargo test --locked --manifest-path rust-controller/Cargo.toml -p controller-domain -p pve-port`. Expected: old domain transitions remain unchanged; exact forms and fake-state changes pass. Update exhaustive workflow matches with `NativePveVmBoot => "native_pve_vm_boot"` but do not route that workflow through the adapter. Commit as `feat(rust): define immutable fake-only native PVE operations`.

### Task 3: Pure precondition and outcome reconciliation rules

**Interfaces:** Consumes Task 2 plans plus typed observation/dispatch/receipt facts; produces `NativeEvidence`, `NativeDecision`, fixed reasons, and the two pure evaluators reused by the store.

**Files:** Add `operation-controller/Cargo.toml`, `src/{lib,decision}.rs`, `tests/decision.rs`; implement the shared `pve-port/src/native.rs` evaluator and native evidence wire validation consumed by Task 4.

- [ ] **Step 1: Add the crate and pure decision cases.**

```toml
[package]
name = "operation-controller"
version = "0.1.0"
edition.workspace = true
rust-version.workspace = true
license.workspace = true
publish = false

[dependencies]
chrono.workspace = true
controller-domain = { path = "../controller-domain" }
event-journal = { path = "../event-journal" }
postgres-store = { path = "../postgres-store" }
scheduler = { path = "../scheduler" }
pve-port = { path = "../pve-port" }
serde.workspace = true
serde_json.workspace = true
thiserror.workspace = true
tokio.workspace = true

[dev-dependencies]
sqlx.workspace = true
proptest.workspace = true
```

`src/decision.rs` re-exports `evaluate_native_preflight` and `evaluate_native_outcome` from `pve-port`; it does not fork the decision rules. Define `NativeDecision::{Ready, Waiting, Satisfied, Failed, Blocked, Unknown, Conflicted}` and fixed `NativeReason` labels for every matrix row below. `NativeEvidence` contains operation/attempt IDs, canonical plan digest, source `PveApi` or `FakePve`, collection/evaluation timestamps, typed snapshots/read errors, optional matching receipt and bound intermediate identity. Its constructors reject cross-operation/attempt facts and serialization contains only allowlisted typed fields. `FakePve` is never labeled `PveApi`.

Each matrix row is a parameterized test with a complete `NativeEvidence` fixture. A test fixture builder uses the synthetic plan in Task 2 and timestamp `2026-09-04T12:00:00Z`; named setters replace one fact per row, and the test asserts both decision and reason. Run `cargo test --manifest-path rust-controller/Cargo.toml -p operation-controller --test decision`. Expected: missing evaluator tests fail.

- [ ] **Step 2: Implement the deterministic evaluator in the stated precedence order.**

| Condition | Decision and permitted action |
|---|---|
| Wrong run/operation/attempt/plan/UPID or changed bound identity | Conflicted; no send or follow-on |
| Run cancellation set | No new claim/send; in-flight result remains Unknown until outcome proof; do not advance workflow |
| Unauthorized required read | Blocked before send; Unknown after dispatch |
| Missing/malformed/stale/future/unavailable required observation | Unknown; bounded reads only |
| Required node/storage/bridge/layout unsupported | Blocked before send |
| VMID occupied before clone, or UUID/MAC already present in cluster | Conflicted, including same-looking unowned VM |
| Clone target absent by both fresh sources, template stopped/unlocked, capacity sufficient | Ready to reserve/dispatch clone |
| Accepted clone task still running | Waiting; no second clone |
| Clone task failed | Failed only with fresh authoritative terminal task failure and no ambiguous target; otherwise Unknown/Conflicted |
| Matching clone UPID successful + fresh exact target node/VMID/name + one unique config/resource identity + exact persisted fake provenance marker/source/request digest | Satisfied; persist intermediate UUID/MAC and disk/config digest as ownership evidence |
| Clone task/VMID/name match but provenance missing or mismatched | Unknown if missing, Conflicted if mismatched; no configure or start |
| Clone receipt missing after possible send | Unknown, regardless of target absence or name match |
| Configure intermediate identity/disk differs from the clone's bound evidence | Conflicted; do not overwrite |
| Configure owned VM stopped/unlocked and desired fields not yet set | Ready with fresh digest |
| Synchronous config receipt without fresh exact desired config | Waiting/Unknown, never satisfied from HTTP success |
| Config receipt and fresh desired UUID/MAC/bridge/cores/memory/agent/boot, same VM/disk | Satisfied |
| Start requires satisfied configure, fresh final identity/config, stopped/unlocked VM | Ready |
| Matching start task success + fresh power running + final identity/config still match | Satisfied |
| Start task succeeded but power stopped; power running but task missing/running | Unknown or Waiting; never infer success from one source |
| Mutation deadline expired or transport lost after dispatch | Unknown; retain receipt/evidence and observe |
| Late matching outcome under unchanged decision/attempt/digest fence | Eligible for fenced reconciliation |
| Late outcome after newer conflict/attempt/decision | Evidence only; current decision preserved |

An existing configuration already equal to desired may satisfy the configure operation **without a mutation** only when clone ownership and all fresh final fields match. Likewise an already running VM may satisfy start without sending only when it was observed running in an uninterrupted, owned workflow before the start operation's first dispatch; record reason `AlreadySatisfied`, no fabricated UPID, and no readiness beyond power state. After any possible start dispatch, the matching task requirement applies.

Write the freshness predicate once and test its exact boundary:

```rust
pub fn is_fresh(
    observed_at: chrono::DateTime<chrono::Utc>,
    as_of: chrono::DateTime<chrono::Utc>,
) -> bool {
    observed_at <= as_of && as_of - observed_at <= chrono::Duration::seconds(30)
}

#[test]
fn freshness_rejects_future_and_accepts_exact_boundary() {
    use chrono::{Duration, Utc};
    let now = "2026-09-04T12:00:00Z".parse::<chrono::DateTime<Utc>>().unwrap();
    assert!(is_fresh(now - Duration::seconds(30), now));
    assert!(!is_fresh(now - Duration::seconds(30) - Duration::milliseconds(1), now));
    assert!(!is_fresh(now + Duration::milliseconds(1), now));
}
```

The evaluator takes `as_of` explicitly. Production-capable store decisions always provide database time; pure tests provide the fixed clock. A returned `Ready` is advice, not a send capability: only the fenced store transaction can mint `NativeDispatchPermit`.

- [ ] **Step 3: Add property invariants and verify the shared transaction path.**

Use proptest to vary one UUID/MAC/VMID/node/attempt/digest at a time and prove no contradictory identity returns Ready or Satisfied. Vary evidence age across the boundary and prove stale evidence never authorizes a send. Enumerate all `ExecutionState` variants to prove unknown/conflicted/failed operations cannot return a new dispatch permit. A valid matching UPID alone never satisfies clone or start.

Run `cargo test --locked --manifest-path rust-controller/Cargo.toml -p operation-controller -p postgres-store -p scheduler -p pve-port`. Expected: pure decisions and existing foundation regressions pass; Task 4 will exercise these same rules under database locks. Commit this crate, shared evaluator and its tests as `feat(rust): evaluate native PVE postconditions and recovery`.

### Task 4: Durable reservation, dispatch and fenced native decisions

**Interfaces:** Consumes Task 2 plans and Task 3 evaluators plus existing `PgStore`/`Scheduler`; produces immutable workflow IDs/snapshots, one-use dispatch permits, fenced decisions and durable run cancellation.

**Files:** Add migration `0003_native_pve.sql`, `postgres-store/src/native.rs`, `postgres-store/src/scheduler/native.rs`, and `postgres-store/tests/native.rs`; modify store/scheduler wiring and manifests.

- [ ] **Step 1: Write PostgreSQL race and crash tests before extending the store.**

Use the disposable PostgreSQL 16 pattern from `scheduler/tests/postgres.rs`: Docker binds its random published port only to `127.0.0.1`, each test database has its own authority row, and container IDs are retained for bounded cleanup. Do not accept an arbitrary DSN override in these new tests. Add tests named:

```rust
// Each test owns its database and uses two independent pool connections for races.
// The bodies exercise the public APIs specified in Steps 2 and 3.
// Required assertions are listed here so no race is reduced to a sequential test.
// intake_same_digest_is_idempotent: same run returns the same three operation IDs.
// intake_changed_digest_conflicts: no extra command, plan, reservation or operation.
// concurrent_identity_reservation_has_one_winner: two runs race on VMID, UUID, then MAC.
// dispatch_is_single_use: concurrent begin_native_dispatch calls permit exactly one send.
// cancelled_or_stale_grant_cannot_dispatch: zero dispatch rows after rejected capability.
// generic_scheduler_cannot_execute_native: ordinary claim/start/finalize reject native rows.
// crash_after_dispatch_never_grants_second_send: reload returns submitted-without-receipt.
// public_append_remains_evidence_only: DecisionRecorded and state events remain rejected.
// native_decision_checks_revision_and_generation: one of two competing decisions commits.
// late_success_preserves_newer_conflict: evidence appends, conflicting decision remains.
```

Implement each body with typed synthetic plans from Task 2 and fresh run/operation UUIDv7 values. Capture before/after row counts and revision/outbox values; assert invariants, not merely returned errors. Run `cargo test --manifest-path rust-controller/Cargo.toml -p postgres-store --test native`. Expected: missing native API compilation failures.

- [ ] **Step 2: Add schema without rewriting prior migrations.**

Extend the existing workflow CHECK to include `native_pve_vm_boot` while preserving all four previous values. In migration 0003 create these structures:

```sql
CREATE TABLE rust_controller.native_vm_reservations (
    cluster_key text NOT NULL,
    vmid integer NOT NULL CHECK (vmid > 0),
    vm_uuid uuid NOT NULL,
    mac text NOT NULL,
    run_id uuid NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (cluster_key, vmid),
    UNIQUE (cluster_key, vm_uuid),
    UNIQUE (cluster_key, mac),
    UNIQUE (run_id)
);

CREATE TABLE rust_controller.native_operation_plans (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.operations(operation_id),
    predecessor_id uuid REFERENCES rust_controller.native_operation_plans(operation_id),
    step text NOT NULL CHECK (step IN ('clone', 'configure', 'start')),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    plan jsonb NOT NULL,
    CHECK (predecessor_id IS NULL OR predecessor_id <> operation_id)
);

CREATE TABLE rust_controller.native_dispatches (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.native_operation_plans(operation_id),
    attempt_id uuid NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    dispatch_revision bigint NOT NULL CHECK (dispatch_revision > 0),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    request_marker uuid NOT NULL UNIQUE,
    preflight_event_id uuid NOT NULL,
    dispatched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (attempt_id, operation_id)
      REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (preflight_event_id, operation_id)
      REFERENCES rust_controller.journal_events(event_id, operation_id)
);

CREATE TABLE rust_controller.native_receipts (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.native_dispatches(operation_id),
    receipt_kind text NOT NULL CHECK (receipt_kind IN ('task', 'synchronous')),
    upid text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((receipt_kind = 'task') = (upid IS NOT NULL))
);

CREATE TABLE rust_controller.native_decisions (
    operation_id uuid NOT NULL REFERENCES rust_controller.native_operation_plans(operation_id),
    decision_revision bigint NOT NULL CHECK (decision_revision > 0),
    attempt_id uuid NOT NULL,
    evidence_event_id uuid NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    decision text NOT NULL CHECK (decision IN ('satisfied', 'failed', 'blocked', 'unknown', 'conflicted')),
    reason text NOT NULL,
    PRIMARY KEY (operation_id, decision_revision),
    FOREIGN KEY (attempt_id, operation_id)
      REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (evidence_event_id, operation_id)
      REFERENCES rust_controller.journal_events(event_id, operation_id)
);

CREATE TABLE rust_controller.native_run_cancellations (
    run_id uuid PRIMARY KEY REFERENCES rust_controller.native_vm_reservations(run_id),
    clone_operation_id uuid NOT NULL REFERENCES rust_controller.native_operation_plans(operation_id),
    decision_event_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (decision_event_id, clone_operation_id)
      REFERENCES rust_controller.journal_events(event_id, operation_id)
);
```

Add BEFORE UPDATE OR DELETE and BEFORE TRUNCATE rejection triggers to all six tables. Intake/dispatch/receipt/decision/cancellation rows are immutable. Evidence continues using the existing journal and outbox; do not store raw HTTP payloads in these tables. The source/intermediate identity and evaluated facts live in typed sanitized evidence payloads, referenced by event ID. No lease token or credential is persisted in new event payloads.

Intake validates the full plan and digests, reserves `(cluster_key, VMID/UUID/MAC)`, creates clone/configure/start operation IDs and immutable plans with predecessor links in one transaction. Use a run-scoped advisory lock followed by sorted identity locks to make the three uniqueness checks deterministic. Existing identical run and digest returns existing IDs; a differing digest or reserved identity rolls back the entire transaction. Refactor the private body of `append_command` into a transaction helper so native intake reuses canonical command/idempotency behavior without nested commits; the public signature and old tests remain unchanged.

- [ ] **Step 3: Add narrow typed store/scheduler APIs.**

Implement the following interfaces; all value fields are private with read-only getters. `NativeStoreError` has fixed labels for validation, conflict, stale evidence, fence mismatch, cancellation, already dispatched and storage unavailable; never wrap raw SQL errors into user-visible strings.

```rust
// In postgres-store::native
pub struct NativeWorkflowIds {
    run_id: controller_domain::RunId,
    clone_id: controller_domain::OperationId,
    configure_id: controller_domain::OperationId,
    start_id: controller_domain::OperationId,
}

// New PgStore methods:
// enqueue_native_vm(run_id: RunId, plan: &NativeVmPlan)
//   -> Result<NativeWorkflowIds, NativeStoreError>
// load_native_operation(operation_id: OperationId)
//   -> Result<NativeOperationSnapshot, NativeStoreError>
// record_native_evidence(operation_id: OperationId, attempt_id: AttemptId,
//   expected_revision: i64, facts: &NativeEvidence)
//   -> Result<EventId, NativeStoreError>

// New Scheduler methods, implemented inside src/scheduler/native.rs:
// claim_native_bound(operation_id: OperationId, fingerprint: &str, cap: u32)
//   -> Result<Option<LeaseGrant>, NativeStoreError>
// start_native_bound(grant: &LeaseGrant, fingerprint: &str)
//   -> Result<ExecutionState, NativeStoreError>
// begin_native_dispatch(grant: &LeaseGrant, expected_revision: i64,
//   preflight_event: EventId, request_digest: &str, request_marker: uuid::Uuid)
//   -> Result<NativeDispatchPermit, NativeStoreError>
// record_native_receipt(permit: &NativeDispatchPermit, receipt: &MutationReceipt)
//   -> Result<(), NativeStoreError>
// decide_native(grant: &LeaseGrant, evidence_event: EventId, expected_revision: i64)
//   -> Result<ExecutionState, NativeStoreError>
// reconcile_native_unknown(operation_id: OperationId, attempt_id: AttemptId,
//   evidence_event: EventId, expected_revision: i64, fingerprint: &str)
//   -> Result<ExecutionState, NativeStoreError>
// cancel_native_run(run_id: RunId)
//   -> Result<(), NativeStoreError>
```

`NativeOperationSnapshot` contains the immutable plan, operation/attempt state, predecessor state, revision, dispatch, receipt and latest typed evidence/decision. Its decoder validates the stored plan against the persisted canonical command digest. `NativeDispatchPermit` is an opaque store-created, single-use capability carrying operation, attempt, request digest and fence; its debug output redacts lease-token material. No public constructor or deserializer exists.

All native claim/start/dispatch/decision/reconciliation/cancellation transactions acquire the same run advisory lock after authority and before operation/lease locks, and read cancellation only after acquiring that run lock. Authority uses `FOR SHARE` in the existing implementation and alone does not serialize these transactions. If multiple operations must be locked, use operation-ID order consistently; never child-then-predecessor opposite cancellation ordering. Intake uses the same run key and its extracted transaction helper, without three separate public append commits. The final pre-send check does not eliminate the documented post-commit external send window.

Revision arguments are explicit: `record_native_evidence` takes the current pre-append aggregate revision, while dispatch/decision/reconciliation take the current post-append revision. Load the referenced event's actual revision and binding; an idempotent append may return an older event ID, so never assume its revision is the supplied revision plus one. Add a private transactional DecisionRecorded helper with NULL `execution_state` and chain its returned revision into any following state event. Cancel-before-claim uses NULL attempt on its journal event and does not insert that event into `native_decisions`, whose attempt is required. AlreadySatisfied requires claim/start to have created the attempt even though dispatch/receipt rows remain absent.

`claim_native_bound` uses the same authority/cap/lease locks as existing claims, filters exactly the requested operation and digest, and requires its predecessor to be satisfied. Ordinary `claim_next`/`claim_next_bound` exclude `NativePveVmBoot`; ordinary `start`/`start_bound`/`finalize` reject it even if supplied a native grant. Native methods reuse private scheduler helpers, not a public bypass boolean. This prevents a caller from finishing a native operation through the adapter's outcome-only finalizer.

`begin_native_dispatch` locks authority -> run -> operations in ID order -> lease, verifies owner/generation/token/attempt/deadline, immutable plan and predecessor, confirms state Running and no cancellation, and validates the persisted typed preflight against database time. It writes the dispatch row and scheduler-owned decision/event/outbox atomically before returning the permit. A uniqueness conflict never returns a permit. The dispatch row deliberately means **send may have happened**: a crash between its commit and actual send stays unknown on recovery.

`record_native_receipt` permits evidence capture after lease expiry but only for the exact persisted dispatch/attempt/request binding; it performs no state transition. A duplicate identical receipt is idempotent, a differing receipt conflicts. Clone/start require task receipts and validate UPID node, worker type and worker ID against their own operation semantics. For clone, worker ID is the **source VMID**; the target is bound by the persisted request. Configure requires synchronous acceptance. Receipt checks do not infer success.

`decide_native` evaluates the referenced typed evidence under the same transaction's authority/operation/lease locks. It checks evidence revision, operation, attempt and plan digest, with the freshness clock taken from PostgreSQL. If a dispatch exists, require its exact binding and the step-specific receipt/outcome rules. If no dispatch exists, only the preflight evaluator can decide: Blocked/Conflicted/Unknown for an unmet precondition, or Satisfied with reason AlreadySatisfied for configure/start whose owned postcondition is already fresh and exact. Clone can never be satisfied without dispatch and matching receipt/provenance. The no-dispatch branch records `dispatch_present=false`, does not fabricate a receipt or insert a dispatch row, and cannot decide success through a caller-supplied boolean. Append DecisionRecorded, state event, projection, attempt finalization and outbox in the same transaction. The caller cannot supply an arbitrary desired state. Test both valid no-dispatch satisfactions and attempts to use the no-dispatch branch to bypass a missing clone receipt.

`reconcile_native_unknown` is a read/decision capability of the current configured scheduler generation; it never creates a lease or dispatch permit. It requires the operation and latest attempt both unknown, no live lease, no newer attempt, exact expected revision and plan digest, then evaluates fresh evidence for the original dispatch. It allows only Unknown -> Satisfied/Failed/Conflicted or an evidence-only no-change Unknown result. Cancellation blocks downstream work even if an in-flight operation is later satisfied. A different current generation can reconcile old facts but the decision records its current generation and the original dispatch generation separately in the journal payload.

`cancel_native_run` locks current authority, then the same run advisory lock as intake, then the run's operations in operation-ID order. It requires a persisted native reservation and all three operation/plan bindings. On first request append a scheduler-owned `DecisionRecorded` event on the clone operation with the fixed payload `{"action":"cancel_native_run","contract_version":1}`, update its revision/projection/outbox, and insert the cancellation row referencing that event in the same transaction. Repeated cancellation is idempotent. Every native claim, dispatch and decision transaction checks the cancellation table; no caller can clear it. Unstarted operations remain pending but unclaimable and appear as cancelled-run pending work in native snapshots; running/waiting operations enter Cancelling through the existing scheduler transition helper and then Unknown when their execution stops or the lease expires. A late outcome may settle an already dispatched operation only through reconciliation, while the persistent run marker continues preventing configure/start claims. Cancel-before-first-claim, cancel-during-clone, duplicate cancel and concurrent cancel-versus-dispatch tests must assert the row/event/outbox counts and zero downstream sends. All methods acquire authority before run/operation locks to avoid an authority/run lock-order inversion.

Put the native pure evaluator required by these store methods in `pve-port/src/native.rs` initially, then keep it there as the shared authority used by `operation-controller`; the store must not depend on `operation-controller`. Task 3 defines the already-tested complete decision rules. Reuse those rules without a second implementation.

- [ ] **Step 4: Verify and commit this task.**

Run `cargo test --locked --manifest-path rust-controller/Cargo.toml -p postgres-store -p scheduler`. Expected: new native races and all old idempotency, immutable-plan, evidence-only append, authority/ABA and reaper tests pass. Confirm migration from an existing foundation DB and fresh DB both pass. Commit as `feat(rust): persist fenced native PVE intents and decisions` once its transaction tests and Task 3's shared evaluator tests pass.

### Task 5: A bounded durable fake-only native controller

**Interfaces:** Consumes Task 4 store/scheduler APIs and the concrete Task 2 fake; produces bounded `run_once`/`reconcile_once` behavior and a measured local proof result.

**Files:** Add `operation-controller/src/controller.rs`, `tests/postgres_native.rs`, and `examples/native_fake.rs`; wire `src/lib.rs`.

- [ ] **Step 1: Write the vertical-slice and crash scenarios.**

The integration harness owns one `NativeFakePve`, one disposable PostgreSQL 16 database, and two scheduler worker identities. It seeds Rust authority generation 1 locally, enqueues the validated synthetic plan, then drives clone -> configure -> start. Assert three stable operation IDs, one attempt and at most one dispatch per operation, one reservation, final exact identity/config, running power, and three satisfied operation projections. Assert the fake recorded exactly clone/configure/start in that order, with no other mutation methods available.

Add named scenarios for each boundary:

| Scenario | Required persisted result / fake request count |
|---|---|
| Normal full slice and duplicate intake | Same operation IDs; 3 total sends |
| Two workers compete | One reservation and one send per operation; loser never sends |
| Crash after dispatch commit, before fake call | Unknown; 0 sends for that step; restarting does not send |
| Fake applies clone, loses response | Clone unknown; 1 clone, 0 configure/start |
| Crash after receipt commit | Reload receipt, observe task/resource, no replay |
| Configure applied but response lost | Unknown until exact fresh configuration can be reconciled under original dispatch; no second configure |
| Start accepted, delayed task success after deadline | Unknown then fenced satisfied; 1 start |
| Stale config then fresh config | Reads retry within bound, send only after fresh preflight |
| Foreign VMID, copied name/task with missing marker, or changed UUID/MAC/disk | Unknown/conflicted as specified; preserve fake foreign VM exactly |
| Config digest conflict or lock | No resend; observe and retain conflict/unknown |
| 401 or malformed read before dispatch | Blocked/unknown; 0 sends |
| Cancellation before start and during clone | No new step; in-flight unknown until observed; run remains cancelled |
| Authority flip or lease expiry just before dispatch | No permit and 0 sends |
| Authority flip after dispatch | Old worker cannot decide; current reconciler can append bound late facts |
| Late success after a conflicting decision | Evidence retained; conflict state unchanged |

Configure response loss can be satisfied from exact postconditions because the synchronous endpoint has no task identity; this exception is specific to configure and requires the prior owned VM/config and original dispatch. It does not weaken clone/start receipt requirements.

Run `cargo test --manifest-path rust-controller/Cargo.toml -p operation-controller --test postgres_native`. Expected: controller API missing until the next step.

- [ ] **Step 2: Implement the bounded execution loop.**

Expose `NativeController::new(store: PgStore, scheduler: Scheduler, fake: Arc<NativeFakePve>) -> Self`, `run_once(operation_id: OperationId) -> Result<NativeProgress, NativeControllerError>`, and `reconcile_once(operation_id: OperationId) -> Result<NativeProgress, NativeControllerError>`. The constructor deliberately accepts the concrete fake, not arbitrary `PveMutationPort` or a URL. `NativeProgress` is `Idle`, `Waiting`, or `Decided(ExecutionState)`; errors are fixed reason labels.

Implement this order exactly in `run_once`:

```text
load and validate immutable native plan + prerequisite/cancellation facts
claim_native_bound for the exact operation and digest
start_native_bound with the same digest
collect bounded fresh typed preflight; append evidence
evaluate preflight
  blocked/conflicted/unknown/already-satisfied -> fenced native decision; return
  ready -> build exact typed request and canonical request digest
begin_native_dispatch -> commit durable dispatch and obtain single-use permit
recheck cancellation/authority immediately before the fake call
  lost -> record unknown, return without send; dispatch remains possibly-sent
invoke exactly one fake mutation method, bounded to 2 seconds
  receipt -> record_native_receipt immediately
  error/timeout -> append classified evidence; decide unknown/blocked as appropriate
collect outcome observations, append typed evidence
decide_native under current lease or leave reconciliation work after fence loss
```

`reconcile_once` only loads the original plan/attempt/dispatch/receipt, collects authoritative observations and invokes `reconcile_native_unknown`. It never calls a mutation method. Between short reads, heartbeat the lease; stop issuing further work if heartbeat/continuation rejects authority or cancellation. Use a 2-second per-read timeout, no more than three read attempts with 100ms then 250ms delays, and the existing database deadline as the hard bound. The executable proof loops `run_once`/`reconcile_once` at 100ms up to 30 seconds, then exits nonzero with a fixed failure label; it never waits indefinitely.

A fence check cannot atomically cancel a request already accepted by PVE; the durable dispatch record and unknown/reconciliation semantics handle that window. Do not claim generation fencing prevents every possible external side effect. The fake fault hooks must exercise the window between the last authority check and send.

- [ ] **Step 3: Implement the standalone local proof.**

`examples/native_fake.rs` owns the Docker PostgreSQL container using the same fixed image, loopback publish and cleanup pattern as the integration harness, creates its database/authority, constructs `NativeFakePve::new()`, enqueues the Task 2 plan and drives the workflow. The example takes **no arguments or environment variables for DB/PVE destinations, tokens, real-mode permission or production settings**. A caller-provided database connection is not necessary for this proof.

On success print only this structured result; derive every count from the actual fake/store, not constants:

```json
{"transport":"in_memory_fake","native_operations":3,"satisfied_operations":3,"mutation_requests":3,"reservations":1,"final_power":"running","os_readiness_proven":false}
```

Exit nonzero unless the expected result and zero foreign-resource changes hold. Drop/cleanup removes only the exact disposable container ID owned by this invocation. There is no HTTP service or enqueue endpoint in the example.

- [ ] **Step 4: Verify and commit this task.**

Run `cargo test --locked --manifest-path rust-controller/Cargo.toml -p operation-controller` and `cargo run --locked --manifest-path rust-controller/Cargo.toml -p operation-controller --example native_fake`. Expected: all fault cases pass and the proof prints its measured success summary. Commit as `feat(rust): prove durable fake native VM boot workflow`.

### Task 6: Boundary regression, source mapping and proof handoff

**Interfaces:** Consumes completed native code and foundation regression tools; produces tested boundary assertions, source-to-proof mapping and an exact handoff with no activation.

**Files:** Modify `controller-service/tests/service.rs` and `rust-controller/README.md`; refresh `rust-controller/Cargo.lock` using Cargo and any existing fixture manifest only if its governed fixtures actually change.

- [ ] **Step 1: Pin the unchanged activation boundary.**

Add service startup cases that set native mode, real transport, remote literal/IPv4-mapped PVE endpoints, remote DB endpoints, and remote hostnames. Native and real transport must still fail before opening a client, binding a listener or touching a database. Preserve existing observe permission behavior; do not repurpose its read opt-in as a mutation permission. Repeat the remote-target denial tests with the new API token present and assert zero requests/client builds, avoiding token-bearing failure output.

Add a source-level API compile-fail check for calling fake mutation methods through `ReqwestPveObserver`. Assert all new proof/example entry points have no live-target selection. No test actually connects to any denied address.

- [ ] **Step 2: Run the proportionate local and CI checks.**

From the repository root:

```bash
cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
cargo clippy --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
cargo test --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast
cargo deny --manifest-path rust-controller/Cargo.toml check
cargo run --locked --manifest-path rust-controller/Cargo.toml -p operation-controller --example native_fake
python3 rust-controller/scripts/test_producer_contract.py
python3 rust-controller/scripts/test_proof_wait.py
python3 rust-controller/scripts/test_proof_coordination.py
```

Expected: formatting clean, no Clippy warnings, all test binaries and compile-fail tests pass, policy checks pass, native proof succeeds, foundation producer/proof tests pass. Tests own local PostgreSQL and fake state. A missing tool/Docker prerequisite is a failed proof gate, not permission to contact a configured server.

The existing Linux AMD64 foundation Compose proof must still pass from its documented build/run sequence. Extend the existing Docker test build to include the new example via Cargo's workspace build/test targets if the image does not already include it; execute the new native test binary within the isolated proof environment only if that environment supports the example's Docker ownership model. Otherwise run the Rust fake-only decision/contract tests on Linux and report PostgreSQL native end-to-end as macOS-only in this slice; do not expose a Docker socket to make the example work. A separate Linux native PostgreSQL proof with an injected **internally owned** container connection is a later artifact gate, not a fabricated pass.

- [ ] **Step 3: Update README with a precise source/verification mapping.**

Replace the foundation-only opening with an accurate statement that the library has fake-only native operations while the service native/real-transport activation remains unavailable. Document the single example command and the measured outcome. Include this mapping, filling the final evidence column with exact test names/results produced by this implementation:

| Contract / approved requirement | Local source used | Verification in this slice |
|---|---|---|
| Clone form and target identity | `autopilot-proxmox/roles/proxmox_vm_clone/tasks/clone_vm.yml` | `native_contract` exact clone form; reserved VMID collision tests |
| Typed config and separate root capability | `autopilot-proxmox/roles/proxmox_vm_clone/tasks/update_config.yml` | fixed seven-field config form; no args/delete/SSH; digest and identity conflict tests |
| Clone/config/start order | `autopilot-proxmox/roles/proxmox_vm_clone_linux/tasks/main.yml` | three separate operations and predecessor checks |
| API token wire header | `autopilot-proxmox/web/proxmox_client.py` | local captured-header, rejection and redaction tests; verified TLS is retained |
| Observation freshness and task identity | `rust-controller/crates/pve-port/src/{lib,model,observer}.rs` | old observer suite plus native outcome matrix |
| Immutable plan and fenced authority | `rust-controller/crates/postgres-store/src/{store,scheduler}.rs` | original immutable-plan/ABA tests plus native dispatch/decision races |
| No public execution-event injection | `PgStore::append_event` in `store.rs` | existing and native EvidenceRecorded-only tests |
| Unknown and late results | Design sections 8–10 | persisted dispatch crash scenarios and narrow unknown reconciliation |
| Production no-touch | Design sections 16, 20–21 | no live transport implementation; startup denial and zero-request assertions |

The local Python/YAML sources establish compatibility intent, not independent confirmation of every PVE response schema. Mark new node/storage/network/resource read envelopes, clone `qmclone` source-worker binding and an eventual real clone provenance mechanism as synthetic contracts requiring official API-schema or separately authorized read-only verification before a future real-PVE artifact. The fake-only provenance marker has no live wire representation in this plan. Do not silently turn the fake's behavior into a live compatibility claim. No raw source inventory, production payload, credential reference or secret is copied into fixtures.

- [ ] **Step 4: Commit and hand off evidence without activating anything.**

Commit the boundary tests/docs/lockfile as `test(rust): verify fake native controller boundaries`. Report source SHA, dirty-tree state, exact passing/failing gates, measured example result, and the supported platform actually exercised. Do not deploy, publish, modify infrastructure, ask an execution-choice question, or infer approval for a live PVE test from completion of this plan.

## Explicit next slices

This plan leaves the following approved program work visible and separate:

1. Official/sanitized read contract verification, a `pve-observer` crate extraction if warranted, node/storage/bridge/artifact preflight expansion and authenticated observation service integration.
2. Real PVE HTTP write implementation behind a separately reviewed capability boundary; operation-specific retry authorization up to the design's two-attempt cap; reservation disposition policies. There is no activation path for these here.
3. Media attachment/detachment, boot variants, firmware/TPM, QGA milestone evaluation and the narrow root-only capability protocol; no general shell.
4. One native OSDeploy vertical slice with unchanged callbacks, then agent/build-host and CloudOSD migration. A running fake VM does not complete this item.
5. Python/Rust shared executor fencing, immutable macOS/Linux artifacts, Linux native database integration, fault/differential/restore proof and separate authorization for a disposable PVE proof.
6. Separately approved production cutover/rollback and deferred product tracks; RustedOutClient remains outside this program.

## Plan self-review checklist

- [ ] Each mutation has its own stable operation, immutable digest, attempt and dispatch evidence.
- [ ] Clone intermediate identity is distinct from final configured identity; lost clone receipt cannot adopt an occupant.
- [ ] Every execution decision is recomputed under the store's current fence; public evidence append remains non-authoritative.
- [ ] No generic scheduler route, deserializer, CLI setting, feature or transport trait implementation can activate live native mutation.
- [ ] Unknown/restart/cancellation and the dispatch/send ambiguity window are proven without mutation replay.
- [ ] Schema changes are additive; original adapter claims and the reviewed foundation tests remain intact.
- [ ] Source-derived assumptions, synthetic proof and future live compatibility gates are explicitly separated.
