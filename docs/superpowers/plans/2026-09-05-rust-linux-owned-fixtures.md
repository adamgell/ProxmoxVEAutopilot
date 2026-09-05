# Linux Owned PostgreSQL Fixtures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the actual registration and existing database regression assertions in a strictly owned local Linux PostgreSQL topology without a Docker socket or arbitrary external database attachment.

**Architecture:** Keep the existing private Container interface and default Docker backend. Add an explicitly selected Linux-test-only backend using a host-verified fixed receipt, one isolated PostgreSQL instance and a unique database per fixture. Fixed embedded Python subprocesses use the existing bounded Rust child lifecycle for admission, creation and synchronous cleanup; no service configuration or production capability changes.

**Tech Stack:** Rust1.92, existing controller-domain/serde_json/Tokio dependencies, PostgreSQL16, cached Python3/psycopg2, existing ManagedChild. No dependency or lockfile changes.

**Spec:** `.superpowers/sdd/2026-09-05-rust-osdeploy-registration/linux-fixture-backend-contract.md`, `linux-db-topology-proof.md` and the main rulings below. Registration source53272397cdd386ec05b7ac6332780ba9ef15e62d has clean per-task and whole-phase reviews; current pre-plan HEAD44a94e3 records acceptance.

## Global Constraints

- “Preserve those registration implementations and assertions.”
- “Keep all ten existing shared lifecycle/process test bodies and default backend assertions.” Test plumbing may address the internal backend variant; do not remove or weaken any behavior/assertion.
- “Retain Container::start() -> (Self, String) asynchronously, synchronous cleanup(&mut self) -> Result<(), ()>, Drop, test-only logs(&self) -> Result<String, ()>, and SETUP_BOUND = 30s.”
- “Cleanup sets one absolute Instant deadline at entry, exactly 3 seconds.” Reserve the existing200ms reap grace inside it. Existing async command bound3s and8192byte streaming output cap remain.
- “A missing/bad Linux receipt never falls back to Docker.” No caller DSN, endpoint, database name, script path, subprocess, SQL or ownership bypass.
- “Each full gate gets a fresh owned PG instance, executes the scheduler harness once, and disposes of the instance afterward; any rerun uses a fresh instance.” Preserve the fixed health_reader role and all tests.
- Real PVE and192.168.2.4 remain read-only. No helper/MCP/live calls, credentials, tenant work, deployment, publication, pulls, pruning or unrelated resource changes. Main fulfilled repository MCP docs preflight; implementer uses this pinned local contract.
- One Astra source implementer; no children or overlapping source/artifact builds. Preserve all ignored SDD evidence and dirty user work. Apply patches; commit exact owned paths only, no push.
- Default macOS regression and synthetic Python/Rust tests are authorized. The final Linux artifact/database run is a separate main-coordinated gate after source review/freeze; do not create an ad-hoc Linux runtime or alter the proven topology during source implementation.

## Main concrete rulings

The design's two-operation Python shorthand is extended by one read-only `admit` operation. This lets receipt file/namespace/SQL checks run inside the same bounded managed child instead of adding blocking filesystem work to async setup or synchronous cleanup. `admit` performs no fixture writes. Creation receives the exact admitted receipt; cleanup must compare against that original receipt before any SQL mutation.

Use serde_json::Value plus canonical compact JSON and exact-key/type checks, not new serde dependencies (the scheduler crate has no direct serde dependency). Receipt/request/response parsing requires at most one trailing LF, otherwise exact equality with serde_json::to_string of the parsed value. This rejects duplicate keys, reordered/escaped aliases and extra whitespace instead of silently normalizing them. Receipt values are bounded ASCII strings plus integer version1; no nested arbitrary values are admitted. Python must use duplicate-rejecting object_pairs_hook and the same compact sorted serialization. Synthetic tests pin both implementations' canonical bytes.

The nine receipt fields are exactly: version=1; session=32lowerhex; pg_id=64lowerhex; pg_image=the pinned PostgreSQL sha256 reference; runner_image=sha256 plus64lowerhex; source_git_sha=40lowerhex; system_identifier=nonempty decimal u64; marker=`lf_` plus session; netns=`net:[` plus positive decimal u64 plus `]`. The host gate writes only `/owned-linux-fixture/receipt.json`, at most4096bytes, as canonical compact JSON plus one LF, on an exact read-only bind. This new receipt is not the historical topology-probe receipt. The host independently verifies runner source/image/ELFs; the receipt is not self-attestation.

The fixed PostgreSQL endpoint is host=hostaddr127.0.0.1, port5432, admin databasepostgres, userpostgres and local test passwordlocal-linux-probe. The owned server must be PostgreSQL16 and match the receipt's system identifier and cluster_name marker. Validate receipt regular-file/non-symlink status and exact bounded contents, current network namespace, and absence of Docker sockets before connecting. No endpoint, executable or file path is configurable.

Linux mode rejects presence of DOCKER_HOST/DOCKER_CONTEXT/DOCKER_CONFIG/DOCKER_TLS_VERIFY/DOCKER_CERT_PATH and PGHOST/PGHOSTADDR/PGPORT/PGDATABASE/PGUSER/PGPASSWORD/PGPASSFILE/PGSERVICE/PGSERVICEFILE/PGOPTIONS. Child commands clear the environment and invoke `/usr/bin/python3 -I -B -c <embedded source>` directly. Mode selection remains exact PROXMOXVEAUTOPILOT_LINUX_TEST_DB=owned-v1 only under tests on Linux. Absent selector keeps Docker; malformed/unsupported selection rejects. Pure parser/mode tests may compile under cfg(test) on macOS; the actual Linux backend variant remains cfg(all(test,target_os="linux")). Non-test examples retain Docker only.

The only source-file scope amendment beyond the design is none: use existing serde_json throughout. The Python implementation imports psycopg2 lazily only for actual admitted database work, so its synthetic standard-library tests run on macOS without installing anything.

### Task 1: Implement bounded Linux fixture backend and preserve all regression assertions

**Files, relative to rust-controller:**
- Modify: proof_support/mod.rs — internal backend routing and default Docker ownership preservation.
- Modify: proof_support/process.rs — narrowly typed managed Python calls; existing capture/kill/reap machinery unchanged.
- Create: proof_support/linux_postgres.rs — private receipt/protocol/identity/guard implementation and Rust tests.
- Create: proof_support/linux_postgres.py — embedded fixed admission/create/cleanup program.
- Create: proof_support/test_linux_postgres.py — synthetic standard-library protocol/lifecycle tests, no database access.
- Modify: crates/postgres-store/tests/postgres.rs — only helper/import plumbing and bounded readiness; preserve all21 assertion bodies.
- Modify: crates/scheduler/tests/postgres.rs — only helper/import plumbing and bounded readiness; preserve all27 assertion bodies.
- Optional comment-only correction: crates/postgres-store/tests/osdeploy_support/mod.rs — corruption targets this fixture database, not a claim that bootstrap superuser cannot reach another owned database.
- No migration, application/store/scheduler implementation, runtime/service config, Cargo manifest, lockfile or other source changes.

**Consumes existing interfaces:**

```rust
local_postgres::Container::start().await; // (Container, String)
container.cleanup();                     // Result<(), ()>
process::ManagedChild;                   // existing private Unix lifecycle
// Async output has COMMAND_BOUND=3s; sync output_blocking takes one Instant.
// Existing OUTPUT_CAP=8192; REAP_GRACE=200ms; SETUP_BOUND=30s.
```

**Produces private interfaces (not application exports):**

```rust
// proof_support/process.rs, cfg(test)
pub(super) enum LinuxCommand { Admit, Create, Cleanup }
pub(super) async fn linux_python(
    op: LinuxCommand, request: &str, bound: Duration,
) -> io::Result<Output>;
pub(super) fn linux_python_cleanup(
    request: &str, deadline: Instant,
) -> io::Result<Output>;

// proof_support/linux_postgres.rs, cfg(test); fields/constructors remain private
pub(super) struct Receipt {
    version: u16,
    session: String,
    pg_id: String,
    pg_image: String,
    runner_image: String,
    source_git_sha: String,
    system_identifier: u64,
    marker: String,
    netns: String,
}
pub(super) struct LinuxDatabase {
    receipt: Receipt,
    family: String,
    nonce: String,
    name: String,
    marker: String,
    oid: Option<u32>,
}
pub(super) fn select_mode(is_linux: bool, value: Option<&str>) -> Result<bool, ()>;
pub(super) fn receipt_response(bytes: &[u8]) -> Result<Receipt, ()>;
pub(super) fn database_name(family: &str, nonce: &str) -> Result<String, ()>;
pub(super) async fn admit() -> Result<Receipt, ()>;
pub(super) fn pending(receipt: Receipt, family: &str, nonce: &str)
    -> Result<LinuxDatabase, ()>;
impl LinuxDatabase {
    pub(super) async fn create(&mut self) -> Result<String, ()>; // constructed DSN
    pub(super) fn cleanup(&mut self, deadline: Instant) -> Result<(), ()>;
}
```

The private backend enum holds DockerOwned or the LinuxDatabase; Container owns the one-shot cleanup-attempt state. Allocate its Linux guard before any create child. Pending database creation has no confirmed OID. Only bounded validated create output may set it. The returned DSN is constructed internally for the generated name, never taken from child JSON. Linux logs returns Err rather than fabricate whole-container log coverage; authenticated service/log suites are not selected by this gate.

**Fixed protocol:** argv is operation name and one canonical JSON request, with no shell or other input. Every request includes version1 and budget_ms, an integer derived by the Rust managed invocation from its remaining work deadline, between1and3000. Admit has only those two fields and responds with exactly receipt plus version1. Create/cleanup additionally require receipt,family,nonce,database,marker,expected_oid (required null before confirmation, otherwise positive u32). Create response fields are exactly database,marker,oid,version1. Cleanup response is exactly database,status="absent",version1. Reject extra/missing fields, wrong types, malformed/oversized output and any mismatch to the pending guard. Rust encodes receipt system_identifier as decimal text, matching the wire schema, despite storing its admitted value as u64. Every mutation rechecks the original receipt/namespace/SQL instance. Fixed nonsecret diagnostics only.

Python starts a monotonic budget from budget_ms and declines to start another connect when less than its minimum supported timeout remains. Connection and statement/lock timeouts are subordinate to the remaining child work budget. The Rust deadline remains authoritative and includes startup/capture/kill/reap; Python's relative timer cannot extend it. No caller-configurable timeout widening.

- [ ] Write compiling pure test stubs and execute behavioral RED for mode/receipt/naming before implementing admission. Use these exact initial assertions plus a literal independently authored canonical nine-field receipt wrapped in the admit response:

```rust
#[test]
fn mode_is_explicit_and_platform_scoped() {
    assert_eq!(select_mode(true, None), Ok(false));
    assert_eq!(select_mode(true, Some("owned-v1")), Ok(true));
    assert!(select_mode(false, Some("owned-v1")).is_err());
    assert!(select_mode(true, Some("other")).is_err());
}
#[test]
fn database_identity_is_generated_and_bounded() {
    let nonce = "a".repeat(32);
    assert_eq!(database_name("native_test", &nonce).unwrap(),
        format!("lf_native_test_{nonce}"));
    assert_eq!(database_name("osdeploy_registration_test", &nonce).unwrap().len(), 62);
    assert!(database_name("postgres", &nonce).is_err());
    assert!(database_name("native_test", "../foreign").is_err());
}
#[test]
fn receipt_is_exact_canonical_and_duplicate_free() {
    const GOOD: &str = r#"{"receipt":{"marker":"lf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","netns":"net:[12345]","pg_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","pg_image":"sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777","runner_image":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","session":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_git_sha":"dddddddddddddddddddddddddddddddddddddddd","system_identifier":"7682173388088000546","version":1},"version":1}"#;
    assert!(receipt_response(GOOD.as_bytes()).is_ok());
    let duplicated = GOOD.replacen("{\"receipt\":", "{\"version\":1,\"receipt\":", 1);
    assert!(receipt_response(duplicated.as_bytes()).is_err());
    assert!(receipt_response(format!(" {GOOD}").as_bytes()).is_err());
}
```

Run from rust-controller: `cargo test --offline --locked -p postgres-store --test osdeploy_registration linux_postgres -- --test-threads=1`. Record actual assertion failures separately from missing-API compilation errors.

- [ ] Implement exact mode/receipt/protocol admission. Canonical codec, scalar field validation and name generation are pure and tested on macOS. Allowed families are native_proof,native_test,osdeploy_registration_test,rust_controller_test. Name is `lf_<family>_<32lowerhex nonce>` at most63bytes; marker is `lf:v1:<session>:<family>:<nonce>`. Use RunId::new UUIDv7 simple lowercase hex for real nonces. Reject duplicate/missing/extra keys, arrays, booleans-as-version, oversized text, wrong hashes/namespace/marker, noncanonical text and malformed response OIDs. No process-global environment mutation in tests.
- [ ] Add fixed embedded Python and managed invocation, initially exercising synthetic RED for foreign/unmarked/OID mismatch and active-create lock refusal. Python entry point accepts only admit/create/cleanup and the schema above. Per-fixture session advisory lock is keyed by the complete gate session and nonce using parameterized `pg_try_advisory_lock(hashtextextended($key,0))`; fail if busy, never wait unbounded. Admit verifies fixed receipt and SQL instance without writes. Create requires absent name, uses template0, obtains OID, stamps the exact COMMENT, re-reads OID/comment and emits one canonical success record. Use psycopg2.sql.Identifier for the validated name and parameters for data. Never expose raw SQL exceptions.
- [ ] Implement cleanup under the same lifecycle lock and original receipt. Absent name may confirm absence only after the lock is acquired. A present database needs full matching marker plus confirmed OID when available; a lost final create response may recover OID only from matching name+full marker. Unmarked/foreign/replaced/busy instances remain untouched with Err. DROP DATABASE WITH(FORCE) may disconnect only that owned database; verify absence before success. Failed cleanup is never converted to success because host teardown may discard the instance later.
- [ ] Wire the managed child path. Use existing streaming cap and cancellation/reaping with fixed `/usr/bin/python3 -I -B -c include_str!("linux_postgres.py")`. Async calls may not exceed min(3s, remaining setup budget); sync cleanup gets the caller's one absolute deadline with200ms reserved reap time. Include receipt inspection/connect/query/drop/output/reaping in that bound. No nested runtime blocking, detached create process, shell, arbitrary executable or mutable helper path. Emit existing fixed uncertainty diagnostics if cleanup/reaping cannot be confirmed.
- [ ] Preserve default Docker implementation, override/image/label/name/ID checks and ten lifecycle tests. Bind synthetic Docker cleanup tests directly to DockerOwned regardless of Linux test mode. Add Linux guard one-shot cleanup and failed/cancelled setup tests. For synthetic protocol tests, inject only private fake child output or synthetic Python cursors/connections; do not add a public fault flag. For real Linux failure tests, private tests may select closed internal fixture fault points only after owned receipt admission: before CREATE, after CREATE before COMMENT, after stamped CREATE before response, and while holding the lifecycle lock. No arbitrary SQL/command input. Verify late server work cannot be mistaken for confirmed absence.
- [ ] Replace both historical direct-Docker helpers with this exact shape while keeping their test bodies unchanged:

```rust
#[path = "../../../proof_support/mod.rs"]
mod local_postgres;
const LOCAL_DATABASE_NAME: &str = "rust_controller_test";
const TEST_MAX_CONNECTIONS: u32 = 4; // Use20 in the scheduler test file.
struct PostgresContainer {
    _owned: local_postgres::Container,
    dsn: String,
}
impl PostgresContainer {
    async fn start() -> Self {
        tokio::time::timeout(local_postgres::SETUP_BOUND, async {
            let (owned, dsn) = local_postgres::Container::start().await;
            let result = Self { _owned: owned, dsn };
            drop(result.wait_for_pool().await);
            result
        }).await.expect("local_fixture_setup_timeout")
    }
    fn dsn(&self) -> String { self.dsn.clone() }
    async fn wait_for_pool(&self) -> PgPool {
        tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                if let Ok(pool) = PgPoolOptions::new()
                    .max_connections(TEST_MAX_CONNECTIONS)
                    .acquire_timeout(Duration::from_secs(1))
                    .connect(&self.dsn).await
                { return pool; }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }).await.expect("local_database_timeout")
    }
}
```

Remove only the old direct-Docker imports/custom Drop. Preserve original per-test connection pools and generated-DSN propagation. Do not change the health_reader SQL/assertions or any registration/native/scheduler production code.
- [ ] Add executable synthetic Rust/Python tests for all parser/protocol/ownership states and managed-child hang, overflow, cancellation, kill/reap and deadline sharing. Include a matching-marker/wrong-OID test, lost-final-response recovery, unmarked CREATE retention, foreign receipt/namespace refusal, cleanup with still-open pool, active-create lock refusal, and one-shot cleanup. Specialized owned-Linux cases are cfg(target_os="linux") and explicitly `#[ignore = "requires the owned Linux fixture namespace"]`, so normal Linux Docker tests remain usable. The final admitted Linux gate MUST use --include-ignored and execute all of them with zero ignored results; no baseline assertion may gain an ignore/filter. Never return early from a test as a substitute for executing it. Report compile-time platform exclusions and required ignored-until-owned cases by exact name. The Python synthetic command is `/usr/bin/python3 proof_support/test_linux_postgres.py`; it must require only the standard library.
- [ ] Run default macOS regression with selector absent, DOCKER_HOST absent and DOCKER_CONTEXT=orbstack pinned after checking its explicit local socket. Command: `env DOCKER_CONTEXT=orbstack cargo test --offline --locked -p postgres-store -p scheduler -p osdeploy-adapter -p operation-controller --all-features --no-fail-fast -- --test-threads=1`. Preserve all231 baseline executions: decision24/native35; adapter12/15/3/14/4/docs6; store native40/registration26/foundation21/docs4; scheduler27. New helper inclusion adds repeated executions; count top-level harnesses and distinct bodies separately. Never filter out actual registration or historical assertions.
- [ ] After final edits, run Python synthetic tests, `cargo fmt --all -- --check`, `cargo clippy --offline --locked --workspace --all-targets --all-features -- -D warnings`, and cached `cargo deny --offline --locked check --disable-fetch --show-stats`. Confirm Cargo.lock/manifests and all application implementations are unchanged. Report existing policy warnings distinctly. No pulls or fetch fallback.
- [ ] Self-review exact scope and failure paths, write the full task report in the new SDD directory, and commit exact owned source paths with `test(proof-support): add owned Linux PostgreSQL fixture backend`. Include RED/GREEN, final per-harness counts, conditional Linux cases, process/cleanup bounds and resource limitations. Main dispatches independent spec/quality review before the platform gate.

## Main platform gate after source review

The final gate uses a fresh owned PG instance per full run/rerun, network none, runtime-verified PGDATA tmpfs, no published ports and the cached default max_connections100 (not the topology probe's20). Preserve all pool/concurrency assertions. The fixed health_reader role is owned-cluster residue discarded only with the verified instance, never generic per-fixture cleanup.

Main must pin the final exact source, tracked input allowlist, lock/dependency hashes, new immutable Linux image and executed ELF set. Adapt the proven host gate to the new canonical receipt and actual Rust test commands; no source patch inside a running image. Run the complete four-package suite on Linux with `-- --include-ignored --test-threads=1`, including all admitted helper failure/lifecycle cases, plus Python synthetic checks; zero ignored or filtered cases are required. Preserve source/executable/image/evidence manifests, inventories and logs; no overlapping source edits/builds/database suites. The existing18GiB pre-build guard is rechecked, never satisfied by unapproved pruning.

This phase closes a test-infrastructure prerequisite only. The active goal still requires exact-source registration Linux proof, durable stage execution/deadlines, callback/service/guest compatibility, independent-process sixteen-stage recovery, Python/Rust single-writer transition, fault/restore/rollback and production-candidate assessment. No production/nonproduction external mutation or deployment authorization is inferred.
