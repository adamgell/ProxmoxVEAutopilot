# Rust Authenticated Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run bounded authenticated PVE cluster-visibility observations from the local Rust service without acquiring any execution capability.

**Architecture:** Reuse the existing verified HTTP client behind a new GET-only capability. Parse visibility into separate sanitized, non-authoritative types; collect only a bounded cluster inventory in this slice. A dedicated credential file is loaded only after mode and destination validation. Service observation health remains distinct from execution readiness and all native/real mutation activation remains closed.

**Tech Stack:** Existing Rust1.92/edition2024 workspace, Tokio, Reqwest/rustls, Serde, Axum, local PostgreSQL16 and owned loopback test servers. No new HTTP stack or observer crate extraction.

**Spec:** `docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md`, sections2,8,11,16–21; predecessor `docs/superpowers/plans/2026-09-04-rust-native-pve-controller.md` explicit next slice1.

## Global Constraints

- "The current production controller at `192.168.2.4` remains unchanged until a separately approved cutover."
- "During local development the Rust controller uses a separate database and namespace. It must never claim production `jobs` rows."
- "Secrets never appear in argv, logs, journal payloads, health details, or fixtures."
- "RustedOutClient is out of scope. It is neither a dependency nor a delivery target for this program."
- Automated tests and this implementation phase make zero real PVE/controller requests. Existing authorization for sanitized read-only research does not require exercising a newly built service remotely.
- Preserve fake native evidence, evaluator, strict execution snapshots, scheduler authority and all accepted foundation/native evidence. No production credential retrieval, vault reuse, SSH, QGA, POST, mutation implementation, tenant access, deployment, publication or schema migration.
- One Astra implementation task at a time, independent review per task, one consolidated final integration review followed by coordinated repairs and scoped re-review. The user has already selected this execution model; do not ask again.
- Begin execution only after predecessor exact-source Linux acceptance. This document is preparatory, not evidence that the next slice is implemented or accepted.

## Scope decisions and interfaces

This is the smallest useful first authenticated service slice: cluster visibility only. Node/storage/bridge/artifact expansion remains a subsequent task family. Existing native preflight parsers must not be made permissive to accommodate observation. Official pinned-source research is retained in the predecessor SDD `official-read-contract-research.md` and `official-permission-coverage-research.md`; these do not verify the user's installed release.

`cluster/resources?type=vm` may contain LXC and QEMU, omit RRD-derived fields, and hide guests lacking VM.Audit. Therefore every report has `coverage: unverified`, and no result grants absence, identity uniqueness, ownership or mutation authority. Do not call permission introspection to imply completeness.

Transport selection uses the new exact value `http-observe`; existing `fake` remains default, and `real` remains denied in every mode. `http-observe` is accepted only with `observe`; a remote destination additionally requires the existing explicit production-read opt-in. No new write selector exists.

Authenticated non-loopback observation additionally requires HTTPS; never send a token over plaintext HTTP to a remote destination. Literal loopback HTTP exists only for owned local fixtures. No insecure TLS flag, custom proxy or redirected credential forwarding is added.

Credential delivery is a dedicated local regular JSON file selected by `RUST_CONTROLLER_PVE_TOKEN_FILE`. Its only fields are `token_id` and `secret`; unknown/duplicate fields fail. The file must be owned by the current effective user, have no group/other permissions, and be at most8192bytes. Open once without following the final symlink and without blocking on a FIFO; validate metadata on the opened descriptor and cap reading at8193bytes. No fallback, live reload, argv secret or raw secret environment variable. These are isolated service credentials, not existing production vault material.

Add only these focused units:

| File | Responsibility |
|---|---|
| `pve-port/src/visibility.rs` | Observation-only sanitized inventory model/parser and narrow trait |
| `pve-port/src/observer.rs` | Bounded authenticated GET through existing verified client |
| `controller-service/src/pve_observation.rs` | Concrete GET-only capability holder and bounded sweep |
| `controller-service/src/pve_credentials.rs` | Private bounded credential-file loader |
| Existing service config/runtime/health | Validated selection, scheduling and honest observation status |
| Focused tests in both crates | Mixed inventory, credential gates, HTTP contracts and no-write proof |

## Task 1: Non-authoritative cluster visibility contracts

**Files:** Create `rust-controller/crates/pve-port/src/visibility.rs` and `rust-controller/crates/pve-port/tests/visibility.rs`; modify `rust-controller/crates/pve-port/src/lib.rs` for explicit exports only.

**Interfaces:** Existing `Vmid`, `NodeName`, `PveReadError`. Produce `GuestKind::{Qemu,Lxc,Unsupported}`, `VisiblePower::{Running,Stopped,Unknown}`, `VisibleGuest` with private VMID/node/kind/optional-template/power fields and read-only getters; `ClusterVisibility` with private observed time/records/rejected count. Serialize sanitized values only; no Deserialize authority or conversion to native types. Public `ClusterVisibility::from_wire(value: serde_json::Value, observed_at: DateTime<Utc>) -> Result<Self,PveReadError>`, `records() -> &[VisibleGuest]`, `rejected_rows() -> usize`, `observed_at() -> DateTime<Utc>`, `coverage() -> &'static str` returning only `unverified`. Define `#[async_trait] pub trait PveVisibilityReadPort: Send + Sync { async fn cluster_visibility(&self) -> Result<ClusterVisibility,PveReadError>; }` with no supertrait.

- [ ] **Step 1: Add runtime parser regressions.** Exact test skeleton:

```rust
#[test]
fn mixed_inventory_preserves_lxc_and_unknown_metadata() {
    let snapshot = ClusterVisibility::from_wire(serde_json::json!([
        {"vmid":101,"node":"pve-test","type":"qemu","status":"running","template":0},
        {"vmid":102,"node":"pve-test","type":"lxc"}
    ]), chrono::Utc::now()).unwrap();
    assert_eq!(snapshot.coverage(), "unverified");
    assert_eq!(snapshot.records().len(), 2);
    assert_eq!(snapshot.records()[1].kind(), GuestKind::Lxc);
    assert_eq!(snapshot.records()[1].power(), VisiblePower::Unknown);
    assert_eq!(snapshot.records()[1].template(), None);
    assert_eq!(snapshot.rejected_rows(), 0);
}
```

Add separate cases for empty visible list still unverified, unknown type preserved as Unsupported, missing status =>Unknown, explicit unknown status=>Unknown, malformed VMID/node rejected and counted, invalid optional template rejected and counted, duplicate valid VMID rejecting the entire response, non-array rejecting, and1025rows rejecting. VMID/node use existing constructors; template accepts only integer0/1; status must be string if present. Drop names and arbitrary raw fields entirely. Missing kind is a rejected row, not QEMU. A rejected row makes the observation degraded in Task4.

- [ ] **Step 2: Run `cargo test --locked --manifest-path rust-controller/Cargo.toml -p pve-port --test visibility`.** First compilation establishes the missing API; rerun after skeletons with the actual parser assertions to obtain behavioral RED before implementation.
- [ ] **Step 3: Implement the bounded parser.** Follow this control flow with typed helpers in the same module:

```rust
let rows = value.as_array().ok_or(PveReadError::InvalidResponse)?;
if rows.len() > 1024 { return Err(PveReadError::InvalidResponse); }
let mut seen = std::collections::BTreeSet::new();
// Parse identity before optional metadata. Insert every valid identity into seen,
// including rows later rejected for metadata; a duplicate returns InvalidResponse.
// Retain only validated fields, increment rejected_rows for any rejected row.
// Preserve accepted rows in wire order. Never fill missing metadata with safety facts.
```

Use private row parsing helpers returning fixed `PveReadError`, never raw JSON/text. `VisibleGuest` getters use `vmid() -> Vmid`, `node() -> &NodeName`, `kind() -> GuestKind`, `template() -> Option<bool>`, `power() -> VisiblePower`.
- [ ] **Step 4: Run focused tests, all pve-port tests and compile-fail native capability tests; fmt/Clippy with warnings denied.** Strict preflight/native source must have no change.
- [ ] **Step 5: Commit exact task files as `feat(rust): add non-authoritative PVE visibility contracts`.** Report fields deliberately omitted and all limits.

## Task 2: Validated observation selection and protected credentials

**Files:** Create `rust-controller/crates/controller-service/src/pve_credentials.rs`; modify service `src/config.rs`, `src/main.rs`, `Cargo.toml`; add unit tests in config/credential modules. Add direct `libc` dependency only at its already locked version for Unix descriptor flags/effective UID; no package upgrade. If unavailable in lock, report the concrete dependency choice before changing policy.

**Interfaces:** Private `PveTransport::{Fake,HttpObserve}` with `as_str() -> &'static str`; extend config parsing without putting credentials into `ControllerConfig` or its Debug representation. Produce `ValidatedObservationConfig` with private token-file path and validated `PveBaseUrl`, constructible only after existing network validation and observe-only mode gate. `load_token(&ValidatedObservationConfig) -> Result<PveApiToken,CredentialFailure>`, with fieldless fixed-label `CredentialFailure`; config itself has no Debug. Use existing `PveApiToken::parse`.

Define `pub(crate) fn observation_config(config: &ControllerConfig) -> Result<Option<ValidatedObservationConfig>, ObservationConfigFailure>` in config.rs, where fieldless `ObservationConfigFailure` retains fixed labels only. It reads the existing transport selector and the new file reference, repeats network validation defensively, and validates mode/HTTPS before returning the private capability. None means fake. Private validated getters expose `base_url() -> &PveBaseUrl` and `token_file() -> &Path` only inside controller-service. Task4 passes this value to the runtime explicitly instead of reparsing environment or reopening a raw configuration path.

- [ ] **Step 1: Test gates before credential I/O.** Cover all3modes, fake/http-observe/real/unknown transport values, literal loopback versus documentation-only remote IPv4/IPv6/hostname, and read opt-in true/false. Also require remote plaintext HTTP rejection even with read opt-in and a valid synthetic credential reference. For denied cases use a private inert loader counter and require zero calls; do not open any target or construct a client. Require a file reference only for accepted http-observe. Fake behavior and all existing error-category tests remain unchanged.
- [ ] **Step 2: Write actual loader regressions.** Owned temp files only: valid0600JSON=>redacted token; oversize, unknown/duplicate fields, malformed JSON, missing file, final symlink, directory, FIFO and group-readable file=>fixed error, no raw contents/path. Bounded FIFO test must not hang: fork an owned child using existing service-test child guard. Capture a synthetic secret canary and assert absent from Debug, stderr and fixed errors. Effective-user mismatch can be tested through a private metadata-validation helper with explicit supplied UID; no chown.
- [ ] **Step 3: Run focused RED then implement.** Open via `OpenOptionsExt::custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)`, validate `metadata.is_file()`, `MetadataExt::uid() == unsafe { libc::geteuid() }`, `mode() & 0o077 == 0`, and length. Read through `Read::take(8193)`; reject more than8192. Parse a private `#[serde(deny_unknown_fields)]` struct with token_id/secret strings; derive neither Debug nor Serialize. Map every failure to a fixed category before returning; no source-chain attachment.
- [ ] **Step 4: Run service config/loader and existing startup tests, formatter/Clippy.** No HTTP activation occurs in this task; runtime still denies non-fake until Task4 deliberately wires http-observe. Tests pin this staged boundary.
- [ ] **Step 5: Commit as `feat(rust): validate protected observation credentials`.** Record direct dependency lock change, if any, and exact no-I/O denial evidence.

## Task 3: Bounded authenticated GET capability

**Files:** Modify `rust-controller/crates/pve-port/src/observer.rs`; create `rust-controller/crates/pve-port/tests/visibility_http.rs`; create service `src/pve_observation.rs` and focused unit tests. Do not alter existing broad read methods or PVE mutation trait.

**Interfaces:** Implement `PveVisibilityReadPort` for `ReqwestPveObserver` using the existing client/token/private endpoint constructor. Add private bounded-body helper only for this new method. Produce service `PveObservation` holding a private `Box<dyn PveVisibilityReadPort>` with no broad `PveReadPort`, scheduler, store, process or mutation field. Production constructor takes validated observation config plus parsed token; test constructor is cfg(test) only. `async fn collect_once(&self) -> ObservationResult`, where fixed enums/status and sanitized counts are the only output; no raw dependency error or guest identifier reaches health.

- [ ] **Step 1: Write loopback HTTP regressions.** Verify exactly one `GET /api2/json/cluster/resources?type=vm`, exact synthetic Authorization header, no other methods/paths. Preserve correct query using `Url::query_pairs_mut`, not concatenated caller URL. Test401/403=>Unauthorized,404=>Unavailable,500=>Unavailable, invalid envelope/data=>Invalid, malformed/rejected rows=>Degraded,1025rows=>Invalid, declared or streaming body greater than1048576bytes=>Invalid, timeout=>TimedOut, dropped collection=>no second request. A302redirect target canary and proxy canary must remain untouched. All server fixtures are owned literal loopback.
- [ ] **Step 2: Run runtime RED then implement bounded read.** Use existing sensitive token header and error mapping. Check Content-Length if present; repeatedly await `response.chunk()` and reject before appending beyond1048576bytes. Deserialize `{"data":...}` only after bounded completion, sample Utc after body collection and call `ClusterVisibility::from_wire`. Set per-request timeout2seconds through existing config; wrap the service collection in3seconds. No automatic retry, per-VM fan-out, permission query or QGA request.
- [ ] **Step 3: Implement sanitized summary.** `ObservationResult` exposes status (`fresh`, `degraded`, `unauthorized`, `unavailable`, `invalid`, `timed_out`), observed_at optional, visible_qemu/lxc/unsupported/rejected counts and coverage fixed `unverified`. Errors have no observation timestamp; a successful empty list has zero counts but no absence assertion. Preserve response-failure categories without displaying errors.
- [ ] **Step 4: Run HTTP tests, all pve-port and service unit tests, strict Clippy/fmt.** Compile-fail examples must show the service-facing visibility trait has neither QGA nor clone/configure/start methods. Do not use source-string assertions as capability proof.
- [ ] **Step 5: Commit as `feat(rust): add bounded GET-only PVE observation`.** Report exact request budget and cancellation behavior.

## Task 4: Observation service health and no-write integration proof

**Files:** Modify service `src/{main,runtime,health}.rs`, existing runtime tests and `tests/service.rs`; modify `rust-controller/README.md`, `rust-controller/Dockerfile.test` only to execute relevant new pure/loopback tests; create isolated integration proof using existing owned fixtures without widening their destinations. Add evidence under a new plan-scoped SDD; historical evidence remains untouched.

**Interfaces:** Construct optional `PveObservation` only from validated http-observe config and token in observe mode. Existing fake and adapter branches stay intact. Add sanitized `pve_observation` health object carrying Task3result and freshness (15seconds, monotonic elapsed time) plus `observation_ready`. Existing `ready` and execution authority checks must not become true merely because an HTTP GET succeeds. Actual `pve_transport` equals `fake` or `http-observe`; evidence for http-observe is `visibility_only_coverage_unverified`. Keep native and literal `real` startup rejection.

- [ ] **Step 1: Add failing integration tests.** Actual service with owned SELECT-only PostgreSQL and loopback HTTP fixture must observe valid mixed inventory and report exact transport, fresh summary, unverified coverage, no raw VM IDs/names/token/path, and no claim of execution readiness from observation alone. Keep current fake readiness assertions. Assert an HTTP failure makes observation_ready false without falsifying database state; stale elapsed time after15seconds also makes it false using an injected test clock, not a long sleep.
- [ ] **Step 2: Prove isolation through runtime behavior.** Snapshot native journal/attempt/lease/dispatch/receipt rows and legacy job status before/after several observation cycles; require exact unchanged contents/counts. Reuse SELECT-only role SQL trace proof and show only allowed read statements with rollback. HTTP server rejects any non-GET or unexpected path and records exactly bounded cycles. No adapter child is spawned; use existing private runner injection/counting seam if available, otherwise keep observer branch typed so it cannot receive a runner and assert actual owned process tree during integration. Do not add a generic process hook to production code.
- [ ] **Step 3: Wire bounded sweeps.** Collect at most once per5seconds with missed ticks skipped, no overlap, and cancellation through existing shutdown signal. Keep existing database compatibility observation separate and cancellation-safe. HTTP snapshot updates are in-memory only and cannot call native evidence ingestion. A failed observation must not overwrite its own last success time with a new success; freshness and current status are evaluated independently.
- [ ] **Step 4: Run full local gates and exact artifact proof.** Full workspace/all-features, Python producer tests, fmt, strict Clippy and cargo-deny with existing warnings reported. Build actual macOS and Linux targets in their supported environments, run loopback visibility tests on both, preserve fake multiworker proof and actual running SHA/transport health checks. Include source manifests and explicit moved/deleted paths if any. No Docker socket mount or live service activation to obtain Linux proof.
- [ ] **Step 5: Commit as `feat(rust): wire isolated authenticated observation service`.** Independent task and whole-slice review, scoped repairs and exact-source evidence precede acceptance. Report a read-only capability, not installed PVE compatibility or production readiness.

## Self-review and remaining scope

- [ ] Cross-check task interfaces and fixed limits:1024rows,1048576body bytes,8192credential bytes,2second request,3second collection,5second interval,15second freshness; do not substitute evaluator thresholds.
- [ ] Confirm no conversion from visibility to execution evidence and no inferred inventory completeness.
- [ ] Verify both mode/target denial and credential denial happen before any network request, file disclosure, process or execution state write.
- [ ] Preserve approved design sections on authority, retries, native execution and existing wire protocols unchanged; this plan does not implement their remaining work.
- [ ] Keep node/storage/bridge/artifact observation expansion, real write provenance/retry/reservation policy, media/firmware/TPM/QGA, OSDeploy/CloudOSD/agent migration, shared Python fencing, restore/non-production proof and separately approved cutover visible as subsequent Rust slices. Deferred product tracks stay after stable controller contracts.
