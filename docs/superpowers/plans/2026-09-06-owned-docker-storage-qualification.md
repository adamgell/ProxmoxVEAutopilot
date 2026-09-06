# Owned Docker Storage Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete deliverable B: prove the accepted owned-fixture tmpfs policy on the actual local Docker backend, retaining SQL semantics, lifecycle cleanup, bounded observations and affected-workload evidence.

**Architecture:** Add a test-only read-only audit module and a dedicated macOS live harness. The existing Container owns creation/removal; the audit handle can only inspect its previously established identity. A bounded external observer samples exact identities emitted by the running workloads, without adding background fixture tasks or changing cleanup.

**Tech Stack:** Existing Rust/Tokio/SQLx/serde_json/chrono workspace and cached local Docker/PostgreSQL; existing Python subprocess orchestration for bounded verification.

**Spec:** `docs/superpowers/specs/2026-09-06-owned-docker-fixture-storage.md`, including the complete deliverable B design checkpoint. Its recorded event-history limitations are binding.

## Global Constraints

- Fixed compiled `--tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=1g,mode=0700`; no caller/environment-selected path, options, size or mount.
- Exact1GiB actual tmpfs capacity and at least128MiB available at observed checkpoints; no observed ENOSPC/OOM. Samples are not an exhaustive peak-memory proof.
- Preserve PostgreSQL defaults and durability settings; no buffer/WAL reduction, forced checkpoint, cgroup memory cap, timeout increase or workload reduction.
- Preserve cached image/no-pull, local Unix endpoint, exact name/label/image/full-ID identity and loopback publication. Original pending ownership guard and exact-container-only cleanup remain unchanged.
- Setup30s, each Docker command3s, shared cleanup3s and existing bounded child-reap allowance remain unchanged. Each dedicated live invocation has180s external work bound; its internal body uses150s to leave time for ordinary unwinding.
- At most two simultaneous owned fixtures. No new live body in shared helper; dedicated live harness is macOS-only and explicitly ignored unless selected.
- No volume deletion/pruning, old-volume attribution, Docker reset, source/dependency/Linux-backend/production changes outside listed paths, credentials/configuration changes, publication or real infrastructure mutation.
- Local8GiB build guard and later18GiB Linux artifact-start guard are independent and must be rechecked. No current proof substitutes for fresh exact-source Linux or full16-stage service/recovery/single-writer readiness.
- Compact event history is corroboration only. Require the exact earlier-witness/endpoint/ordering predicate; failed/missing/overflowed history is uncertainty, not success. Do not generate canaries, sort away anomalies or retry indefinitely.
- Main owns acceptance and independent review. Implementer has no subagents and does not edit main ledgers/spec/plan/trackers. Use apply_patch for source/report edits and keep original failures.

## Files and ownership

- Modify `rust-controller/proof_support/mod.rs` only for test-only module/API bridge and sanitized pending/created identity lines.
- Create `rust-controller/proof_support/docker_storage_probe.rs` for closed read-only audit operations and synthetic decoders/tests.
- Create `rust-controller/crates/postgres-store/tests/docker_fixture_storage.rs` for the dedicated live cases.
- No `process.rs` change. Use its existing bounded runner/output cap.
- Full implementation/verification receipts belong in this plan's ignored `task-1-report.md`. A bounded observer script may be created via apply_patch in this plan's ignored workspace solely to execute Step7's fixed verification protocol; it is not a production source or a generic Docker-management tool.

### Task 1: Actual tmpfs lifecycle and workload qualification

**Files:** the three source paths above; tests are the new probe module and dedicated harness.

**Interfaces:**
- Consumes unchanged `Container::start() -> (Self, String)`, `Container::cleanup(&mut self) -> Result<(), ()>`, private `DockerOwned::verified_owned_id`, accepted `storage_admitted`, existing `process::docker` and `PgStore::migrate`.
- Produces test-only `Container::storage_audit(&self) -> Result<DockerStorageAudit, AuditError>`.
- Produces `DockerStorageAudit::sample(&mut self) -> Result<StorageSample, AuditError>`, `confirm_removed(&self) -> Result<(), AuditError>`, and `identity(&self) -> (&str, &str)`.
- No arbitrary identity/endpoint/wire constructors or cleanup/command interface. Audit errors are fixed and payload-free. Public controller APIs are unchanged.

- [ ] **Step 1: Write the synthetic decoder tests and observe compiled RED.**

Create the new probe module with the types below, decoder functions initially returning the corresponding fixed error, and these tests. Add only its cfg(test) module declaration to the existing helper for this RED. Run `cargo test --offline --locked -p postgres-store --lib docker_storage_probe -- --nocapture --test-threads=1` under180s. Valid-input assertions must fail after compilation, before implementing decoders or live operations. Correct compilation-only errors without implementing the repair. No real fixture for RED.

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditError { Ownership, Process, Storage, Capacity, Memory, History, Absence }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StorageSample { pub capacity_bytes: u64, pub available_bytes: u64, pub memory_bytes: u64 }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capacity_requires_actual_fixed_tmpfs_and_reserve() {
        assert_eq!(capacity("tmpfs|4096|262144|65536\n"), Ok((1073741824,268435456)));
        for input in ["ext2/ext3|4096|262144|65536\n", "tmpfs|4096|524288|65536\n",
            "tmpfs|4096|262144|1\n", "tmpfs|18446744073709551615|2|2\n",
            "tmpfs|0|0|0\n", "tmpfs|4096|262144|262145\n", "tmpfs|4096|262144\n"] {
            assert_eq!(capacity(input), Err(AuditError::Capacity));
        }
    }
    #[test]
    fn memory_requires_numeric_complete_non_oom_counters() {
        assert_eq!(memory("12345\nlow 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"), Ok(12345));
        for input in ["123\noom 1\noom_kill 0\n", "123\noom 0\noom_kill 1\n",
            "123\noom 0\n", "123\noom 0\noom 0\noom_kill 0\n", "x\noom 0\noom_kill 0\n"] {
            assert_eq!(memory(input), Err(AuditError::Memory));
        }
    }
    #[test]
    fn retained_events_require_a_bracket_without_attributed_mounts() {
        assert_eq!(events("[1,0]\n[3,1]\n[4,2]\n",2), Ok(()));
        let mut full = String::from("[1,0]\n[3,1]\n");
        for _ in 0..253 { full.push_str("[3,0]\n"); }
        full.push_str("[4,2]\n");
        assert_eq!(events(&full,2), Ok(()));
        for input in ["[3,1]\n[4,2]\n", "[2,0]\n[3,1]\n[4,2]\n",
            "[1,0]\n[4,2]\n[3,1]\n", "[1,0]\n[3,1]\n[3,1]\n[4,2]\n",
            "[1,0]\n[3,1]\n[4,2]", "[1,0]\n[3,1,0]\n[4,2]\n",
            "[-1,0]\n[3,1]\n[4,2]\n", "[1,0]\n[3,5]\n[4,2]\n"] {
            assert_eq!(events(input,2), Err(AuditError::History));
        }
        full.push_str("[5,0]\n");
        assert_eq!(events(&full,2), Err(AuditError::History));
        for class in [3,4] {
            assert_eq!(events(&format!("[1,0]\n[3,1]\n[3,{class}]\n[4,2]\n"),2), Err(AuditError::Storage));
        }
    }
}
```

- [ ] **Step 2: Implement the bounded decoders.**

The functions consume sanitized fixed-query output, never raw environment or table data. Keep input cap/newline checks so a clipped record cannot silently qualify.

```rust
fn capacity(text: &str) -> Result<(u64,u64), AuditError> {
    let fields: Vec<_> = text.trim().split('|').collect();
    if fields.len()!=4 || fields[0]!="tmpfs" { return Err(AuditError::Capacity); }
    let block = fields[1].parse::<u64>().map_err(|_| AuditError::Capacity)?;
    let total = fields[2].parse::<u64>().map_err(|_| AuditError::Capacity)?;
    let available = fields[3].parse::<u64>().map_err(|_| AuditError::Capacity)?;
    let total = block.checked_mul(total).ok_or(AuditError::Capacity)?;
    let available = block.checked_mul(available).ok_or(AuditError::Capacity)?;
    if total!=1073741824 || available<134217728 || available>total { return Err(AuditError::Capacity); }
    Ok((total,available))
}
fn memory(text: &str) -> Result<u64, AuditError> {
    let mut lines=text.lines();
    let current=lines.next().ok_or(AuditError::Memory)?.parse::<u64>().map_err(|_| AuditError::Memory)?;
    let mut values=std::collections::BTreeMap::new();
    for line in lines {
        let pair:Vec<_>=line.split_whitespace().collect();
        if pair.len()!=2 { return Err(AuditError::Memory); }
        let value=pair[1].parse::<u64>().map_err(|_| AuditError::Memory)?;
        if values.insert(pair[0],value).is_some() { return Err(AuditError::Memory); }
    }
    if values.get("oom")!=Some(&0) || values.get("oom_kill")!=Some(&0) { return Err(AuditError::Memory); }
    Ok(current)
}
fn events(text: &str, created: i64) -> Result<(), AuditError> {
    if text.len()>8192 || !text.ends_with('\n') || created<=0 { return Err(AuditError::History); }
    let rows=text.lines().map(serde_json::from_str::<(i64,u8)>).collect::<Result<Vec<_>,_>>()
        .map_err(|_| AuditError::History)?;
    if !(3..=256).contains(&rows.len()) || rows[0].0>=created
        || rows.iter().any(|(time,class)| *time<=0 || *class>4)
        || rows.windows(2).any(|pair| pair[0].0>pair[1].0) { return Err(AuditError::History); }
    if rows.iter().any(|(_,class)| matches!(class,3|4)) { return Err(AuditError::Storage); }
    let starts:Vec<_>=rows.iter().filter(|(_,class)| *class==1).collect();
    let ends:Vec<_>=rows.iter().filter(|(_,class)| *class==2).collect();
    if starts.len()!=1 || ends.len()!=1 || starts[0].0<created || starts[0].0>ends[0].0 {
        return Err(AuditError::History);
    }
    let start=rows.iter().position(|(_,class)| *class==1).ok_or(AuditError::History)?;
    let end=rows.iter().position(|(_,class)| *class==2).ok_or(AuditError::History)?;
    if start>=end { return Err(AuditError::History); }
    Ok(())
}
```

- [ ] **Step 3: Add the closed read-only audit handle and bridge.**

In the new probe module, use the following implementation. No read method changes the fixture. Its copied `DockerOwned` has no Drop implementation and is never used for removal. Query output errors stay fixed; never log a complete Docker inspect.

```rust
use super::{DockerOwned, OWNERSHIP_FORMAT, PGDATA, STORAGE_FORMAT, process};
use std::time::Duration;

pub struct DockerStorageAudit { owned: DockerOwned, created_ns: Option<i64> }
fn text(output: std::process::Output) -> Result<String, AuditError> {
    if !output.status.success() { return Err(AuditError::Process); }
    String::from_utf8(output.stdout).map_err(|_| AuditError::Process)
}
impl DockerStorageAudit {
    pub(super) fn new(owned:&DockerOwned)->Result<Self,AuditError> {
        let id=owned.id.as_deref().ok_or(AuditError::Ownership)?;
        if id.len()!=64 || !id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || owned.cleanup_script.is_some() { return Err(AuditError::Ownership); }
        Ok(Self { owned:DockerOwned { id:Some(id.into()),name:owned.name.clone(),
            endpoint:owned.endpoint.clone(),cleanup_script:None },created_ns:None })
    }
    pub fn identity(&self)->(&str,&str) { (&self.owned.name,self.owned.id.as_deref().unwrap()) }
    async fn verify(&self)->Result<(),AuditError> {
        let out=process::docker(&["--host",&self.owned.endpoint,"inspect","--format",OWNERSHIP_FORMAT,self.identity().1])
            .await.map_err(|_| AuditError::Process)?;
        if self.owned.verified_owned_id(&out).as_deref()!=Some(self.identity().1) { return Err(AuditError::Ownership); }
        Ok(())
    }
    async fn initialize(&mut self)->Result<(),AuditError> {
        let fmt=r#"{"created":{{json .Created}},"image":{{json .Image}}}"#;
        let raw=text(process::docker(&["--host",&self.owned.endpoint,"inspect","--format",fmt,self.identity().1])
            .await.map_err(|_| AuditError::Process)?)?;
        let value:serde_json::Value=serde_json::from_str(&raw).map_err(|_| AuditError::Storage)?;
        let created=chrono::DateTime::parse_from_rfc3339(value["created"].as_str().ok_or(AuditError::Storage)?)
            .map_err(|_| AuditError::Storage)?.timestamp_nanos_opt().filter(|v| *v>0).ok_or(AuditError::Storage)?;
        let image=value["image"].as_str().ok_or(AuditError::Storage)?;
        let digest=image.strip_prefix("sha256:").ok_or(AuditError::Storage)?;
        if digest.len()!=64 || !digest.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(AuditError::Storage);
        }
        let raw=text(process::docker(&["--host",&self.owned.endpoint,"image","inspect","--format","{{json .Config.Volumes}}",image])
            .await.map_err(|_| AuditError::Process)?)?;
        let volumes:serde_json::Value=serde_json::from_str(&raw).map_err(|_| AuditError::Storage)?;
        if volumes!=serde_json::json!({(PGDATA):{}}) { return Err(AuditError::Storage); }
        eprintln!("owned_storage_image {} {} {}",self.identity().0,self.identity().1,image);
        self.created_ns=Some(created);
        Ok(())
    }
    pub async fn sample(&mut self)->Result<StorageSample,AuditError> {
        tokio::time::timeout(Duration::from_secs(24),async {
            self.verify().await?;
            if self.created_ns.is_none() { self.initialize().await?; }
            let out=process::docker(&["--host",&self.owned.endpoint,"inspect","--format",STORAGE_FORMAT,self.identity().1])
                .await.map_err(|_| AuditError::Process)?;
            if !self.owned.storage_admitted(&out) { return Err(AuditError::Storage); }
            let raw=text(process::docker(&["--host",&self.owned.endpoint,"exec",self.identity().1,"stat","-f","-c","%T|%S|%b|%a",PGDATA])
                .await.map_err(|_| AuditError::Process)?)?;
            let (capacity_bytes,available_bytes)=capacity(&raw)?;
            let raw=text(process::docker(&["--host",&self.owned.endpoint,"exec",self.identity().1,"cat",
                "/sys/fs/cgroup/memory.current","/sys/fs/cgroup/memory.events"])
                .await.map_err(|_| AuditError::Process)?)?;
            let memory_bytes=memory(&raw)?;
            let raw=text(process::docker(&["--host",&self.owned.endpoint,"inspect","--format",
                "{{.State.OOMKilled}}|{{.HostConfig.Memory}}",self.identity().1])
                .await.map_err(|_| AuditError::Process)?)?;
            if raw.trim()!="false|0" { return Err(AuditError::Memory); }
            let sample=StorageSample{capacity_bytes,available_bytes,memory_bytes};
            eprintln!("owned_storage_sample {} {} {:?}",self.identity().0,self.identity().1,sample);
            Ok(sample)
        }).await.map_err(|_| AuditError::Process)?
    }
    pub async fn confirm_removed(&self)->Result<(),AuditError> {
        tokio::time::timeout(Duration::from_secs(12),async {
            let created=self.created_ns.ok_or(AuditError::History)?;
            for filter in [format!("id={}",self.identity().1),format!("name=^/{}$",self.identity().0)] {
                let raw=text(process::docker(&["--host",&self.owned.endpoint,"ps","--all","--no-trunc","--filter",&filter,"--format","{{.ID}}"])
                    .await.map_err(|_| AuditError::Process)?)?;
                if !raw.trim().is_empty() { return Err(AuditError::Absence); }
            }
            let template=r#"[{{.TimeNano}},{{if and (eq .Type "container") (eq .Actor.ID "OWNED_ID")}}{{if eq .Action "create"}}1{{else if eq .Action "destroy"}}2{{else}}0{{end}}{{else if and (eq .Type "volume") (eq (index .Actor.Attributes "container") "OWNED_ID")}}{{if eq .Action "mount"}}3{{else if eq .Action "unmount"}}4{{else}}0{{end}}{{else}}0{{end}}]"#
                .replace("OWNED_ID",self.identity().1);
            let raw=text(process::docker(&["--host",&self.owned.endpoint,"events","--since","1970-01-01T00:00:00Z","--until","0s","--format",&template])
                .await.map_err(|_| AuditError::Process)?)?;
            events(&raw,created)?;
            eprintln!("owned_storage_removed {} {} bounded_recorded_event_bracket_only",self.identity().0,self.identity().1);
            Ok(())
        }).await.map_err(|_| AuditError::Process)?
    }
}
```

Keep all source compilation corrections within the declared scope, and distinguish them from behavioral failures. The unchanged `verified_owned_id` checks the image selector; the probe needs no separate selector import.

Bridge in `proof_support/mod.rs`:

```rust
#[cfg(test)]
#[allow(dead_code, reason="read-only audit is used by the dedicated macOS storage harness")]
pub mod docker_storage_probe;
#[cfg(test)]
impl Container {
    #[allow(dead_code, reason="only the dedicated storage harness obtains read-only audit handles")]
    pub fn storage_audit(&self)->Result<docker_storage_probe::DockerStorageAudit,docker_storage_probe::AuditError> {
        if self.cleanup_result.is_some() { return Err(docker_storage_probe::AuditError::Ownership); }
        match &self.backend {
            Backend::Docker(owned)=>docker_storage_probe::DockerStorageAudit::new(owned),
            #[cfg(target_os="linux")]
            Backend::Linux(_)=>Err(docker_storage_probe::AuditError::Ownership),
        }
    }
}
```

In the existing Docker start branch, emit `eprintln!("owned_fixture_pending {}", container.name);` under `#[cfg(test)]` after obtaining the pending owned guard and before run. Emit `eprintln!("owned_fixture_created {} {}", container.name, id);` under `#[cfg(test)]` after existing ownership assertion and before storage admission. Neither line contains environment/DSN or grants authority to an observer; the observer must reverify identity. No cleanup changes.

- [ ] **Step 4: Implement the dedicated macOS-only live harness.**

```rust
#![cfg(target_os="macos")]
#[path="../../../proof_support/mod.rs"]
mod local_postgres;
const LOCAL_DATABASE_NAME:&str="storage_qualification_test";
use local_postgres::Container;
use postgres_store::PgStore;
use sqlx::{PgPool,postgres::PgPoolOptions};
use std::time::{Duration,Instant};

async fn fresh()->(Container,PgPool,String) {
    assert!(std::env::var_os("PROXMOXVEAUTOPILOT_LINUX_TEST_DB").is_none());
    tokio::time::timeout(local_postgres::SETUP_BOUND,async {
        let (container,dsn)=Container::start().await;
        let pool=tokio::time::timeout(Duration::from_secs(15),async {
            loop {
                if let Ok(pool)=PgPoolOptions::new().max_connections(3).acquire_timeout(Duration::from_secs(1)).connect(&dsn).await {
                    break pool;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }).await.expect("owned_storage_database_timeout");
        PgStore::new(pool.clone()).migrate().await.expect("owned_storage_migration");
        (container,pool,dsn)
    }).await.expect("owned_storage_setup_timeout")
}
async fn sql_proof(pool:&PgPool,dsn:&str) {
    let independent=PgPoolOptions::new().max_connections(2).acquire_timeout(Duration::from_secs(1))
        .connect(dsn).await.expect("owned_storage_independent_pool");
    sqlx::query("CREATE TABLE rust_controller.fixture_storage_probe (id bigint PRIMARY KEY)").execute(pool).await.unwrap();
    let mut tx=pool.begin().await.unwrap();
    sqlx::query("INSERT INTO rust_controller.fixture_storage_probe VALUES (1)").execute(&mut *tx).await.unwrap();
    tx.commit().await.unwrap();
    let count: i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_storage_probe").fetch_one(&independent).await.unwrap();
    assert_eq!(count,1);
    let mut tx=pool.begin().await.unwrap();
    sqlx::query("INSERT INTO rust_controller.fixture_storage_probe VALUES (2)").execute(&mut *tx).await.unwrap();
    tx.rollback().await.unwrap();
    let count: i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.fixture_storage_probe").fetch_one(&independent).await.unwrap();
    assert_eq!(count,1);
    for setting in ["server_version_num","data_directory","shared_buffers","max_connections","max_wal_size",
        "min_wal_size","checkpoint_timeout","fsync","synchronous_commit","full_page_writes"] {
        let value:String=sqlx::query_scalar("SELECT current_setting($1)").bind(setting).fetch_one(pool).await.unwrap();
        if matches!(setting,"fsync"|"synchronous_commit"|"full_page_writes") { assert_eq!(value,"on"); }
        if setting=="data_directory" { assert_eq!(value,"/var/lib/postgresql/data"); }
        if setting=="server_version_num" { assert!((160000..170000).contains(&value.parse::<u32>().unwrap())); }
        eprintln!("owned_storage_setting {setting}={value}");
    }
    let bytes:i64=sqlx::query_scalar("SELECT pg_database_size(current_database())").fetch_one(pool).await.unwrap();
    assert!(bytes>0); eprintln!("owned_storage_database_bytes {bytes}");
    independent.close().await;
}
#[tokio::test]
#[ignore="explicit owned Docker storage qualification only"]
async fn owned_tmpfs_sql_and_explicit_cleanup() {
    tokio::time::timeout(Duration::from_secs(150),async {
        let (mut first,first_pool,first_dsn)=fresh().await;
        let (mut second,second_pool,second_dsn)=fresh().await;
        let mut a=first.storage_audit().unwrap(); let mut b=second.storage_audit().unwrap();
        assert_ne!(a.identity().1,b.identity().1);
        a.sample().await.unwrap(); b.sample().await.unwrap();
        sql_proof(&first_pool,&first_dsn).await; sql_proof(&second_pool,&second_dsn).await;
        a.sample().await.unwrap(); b.sample().await.unwrap();
        first_pool.close().await; second_pool.close().await;
        assert_eq!(first.cleanup(),Ok(())); a.confirm_removed().await.unwrap();
        assert_eq!(second.cleanup(),Ok(())); b.confirm_removed().await.unwrap();
    }).await.expect("owned_storage_sql_lifecycle_timeout");
}
#[tokio::test(flavor="current_thread")]
#[ignore="explicit owned Docker storage qualification only"]
async fn owned_tmpfs_drop_cleanup() {
    tokio::time::timeout(Duration::from_secs(150),async {
        let (container,pool,_dsn)=fresh().await;
        let mut audit=container.storage_audit().unwrap(); audit.sample().await.unwrap();
        pool.close().await; drop(container); audit.confirm_removed().await.unwrap();
    }).await.expect("owned_storage_drop_timeout");
}
#[tokio::test(flavor="current_thread")]
#[ignore="explicit owned Docker storage qualification only"]
async fn owned_tmpfs_current_thread_cancellation() {
    tokio::time::timeout(Duration::from_secs(150),async {
        let (container,pool,_dsn)=fresh().await;
        let mut audit=container.storage_audit().unwrap(); audit.sample().await.unwrap();
        pool.close().await;
        let start=Instant::now();
        assert!(tokio::time::timeout(Duration::from_millis(40),async move {
            let _owned=container; std::future::pending::<()>().await;
        }).await.is_err());
        assert!(start.elapsed()<Duration::from_millis(3600));
        audit.confirm_removed().await.unwrap();
    }).await.expect("owned_storage_cancellation_timeout");
}
```

- [ ] **Step 5: Run synthetic GREEN, inspect selection and strict compile before live qualification.**

Use existing finite Python process-group watchdog: print UTC/argv/bound; wait bound; TERM then at most5s wait; KILL if needed then at most5s final wait; report unreaped state rather than hang. Do not change work bounds. From `rust-controller` run:

```text
# Each180s, no Docker:
cargo test --offline --locked -p postgres-store --lib docker_storage_probe -- --nocapture --test-threads=1
cargo test --offline --locked -p postgres-store --lib local_postgres -- --nocapture --test-threads=1
cargo test --offline --locked -p postgres-store --test docker_fixture_storage -- --list
#60s:
cargo fmt --all -- --check
#300s, compile only:
cargo clippy --offline --locked --workspace --all-targets --all-features -- -D warnings
```

Inspect the list and cfg boundaries: three dedicated ignored bodies on macOS, none on Linux. Shared synthetic repeats are not additional distinct tests. Formatting may use rustfmt with `skip_children=true` on exact authorized files; do not edit other source. On errors preserve the first result and fix only the task's source/test defect; policy/time-bound changes require main's ruling.

- [ ] **Step 6: Run exactly the three dedicated live cases serially.**

Recheck disk>=8GiB; assert no DOCKER_HOST or Linux DB selector. Use existing cached orbstack context, verify its endpoint is local Unix and cached image is present without pulling. Record sanitized baseline of exact protected containers and any already-running labeled fixtures; do not touch them. Run each command separately under180s with `DOCKER_CONTEXT=orbstack`:

```text
cargo test --offline --locked -p postgres-store --test docker_fixture_storage owned_tmpfs_sql_and_explicit_cleanup -- --ignored --exact --nocapture --test-threads=1
cargo test --offline --locked -p postgres-store --test docker_fixture_storage owned_tmpfs_drop_cleanup -- --ignored --exact --nocapture --test-threads=1
cargo test --offline --locked -p postgres-store --test docker_fixture_storage owned_tmpfs_current_thread_cancellation -- --ignored --exact --nocapture --test-threads=1
```

Retain pending names, verified full IDs, immutable image IDs/declarations, settings, capacity/memory samples, exact absence and event-bracket result per case. Do not replace ambiguous event history with a global volume count. On actual low capacity/OOM/ENOSPC, timeout, cleanup or history uncertainty stop qualification and return evidence to main; do not change policy, rerun indefinitely, remove any volume or proceed to broader fixtures.

- [ ] **Step 7: Qualify existing heavy workloads with externally owned bounded sampling.**

Run this step only after all three live cases pass. The observer is a separate host-owned process/control loop, not a task added to Container. Launch each exact Cargo command with inherited `DOCKER_CONTEXT=orbstack`, process-group ownership and its existing work deadline; capture/forward stdout/stderr so the `owned_fixture_pending` and `owned_fixture_created` lines are retained. Correlate created identities only with names emitted by this invocation; require generated name syntax and64 lowercase hex ID. Do not treat another concurrently labeled fixture as owned by this command.

For each selected heavy case, attempt samples while its exact fixture is alive (target cadence1s; record actual times/gaps). Before every sample, a bounded exact inspect must match full ID, generated name, equal ownership label and `postgres:16-alpine`; then verify A's exact storage map/mount policy. Use these fixed read-only commands with the captured, validated endpoint/ID—never a caller-selected path or full inspect dump:

```text
docker --host ENDPOINT inspect --format '{{.Id}}|{{.Name}}|{{index .Config.Labels "io.proxmoxveautopilot.native-proof"}}|{{.Config.Image}}' FULL_ID
docker --host ENDPOINT inspect --format STORAGE_FORMAT FULL_ID
docker --host ENDPOINT exec FULL_ID stat -f -c '%T|%S|%b|%a' /var/lib/postgresql/data
docker --host ENDPOINT exec FULL_ID cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.events
docker --host ENDPOINT inspect --format '{{.State.OOMKilled}}|{{.HostConfig.Memory}}' FULL_ID
```

Here ENDPOINT and FULL_ID are data validated as just described, and STORAGE_FORMAT is the exact accepted private constant, not a free-form input. This independent observer deliberately checks the same fixed facts outside the fixture; do not expose its internal command composition as a public audit API. Every Docker subprocess gets at most3s including its bounded termination handling and at most8192 captured bytes; use the existing main-approved2.7s work plus0.2s kill/reap envelope where the Python observer creates its own subprocess. The existing Rust process runner retains its original3s work and bounded child-reap allowance. The Cargo deadline is unchanged and must include all observation scheduling: check it before and after reads, and terminate/reap its owned process group on expiration. Do not start a new observation after Cargo is known terminal. A read already in flight when Cargo terminates must finish or be killed/reaped within its original bound and be reported as overlapping termination, not an in-workload capacity sample. No detached sampling child, unlimited wait, or raw environment output.

Parse/check exactly the same type/capacity/reserve/OOM fields as the Rust audit, retaining the sanitized numeric sample and UTC/monotonic timing. A disappearing fixture can produce an observation gap: confirm exact absence with a successful bounded daemon listing, record the gap and do not call it a passing sample. Preserve source identity, per-case coverage and minimum observed reserve; a workload with no qualifying sample is not capacity-qualified. Failed ownership/storage/capacity/OOM or unresolved daemon errors stop the run and return to main. Do not make work smaller to facilitate sampling.

First execute these named heavy regressions, each under the existing durability900s or registration600s envelope as appropriate, adding `-- --exact --nocapture --test-threads=1`:

```text
cargo test --offline --locked -p postgres-store --test osdeploy_registration registration_and_reload_are_atomic_and_idempotent
cargo test --offline --locked -p postgres-store --test osdeploy_durability task7_expiry_cursor_crosses_poison_pages_and_wraps_without_starvation
cargo test --offline --locked -p postgres-store --test osdeploy_durability task7_expiry_cursor_finds_late_visible_original_scope_after_wrap
cargo test --offline --locked -p postgres-store --test osdeploy_durability task7_new_mutations_rollback_every_actual_row_write_and_deferred_commit
```

Then run full serial gates, all with `-- --nocapture --test-threads=1`, preserving the exact source and sample coverage:

```text
#900s:
cargo test --offline --locked -p postgres-store --test osdeploy_durability
#600s each:
cargo test --offline --locked -p postgres-store --test osdeploy_registration
cargo test --offline --locked -p postgres-store --test native
cargo test --offline --locked -p operation-controller --test postgres_native
cargo test --offline --locked -p scheduler --test postgres
#300s:
cargo test --offline --locked -p postgres-store --lib
```

The native-persistence harness is the verified existing `crates/postgres-store/tests/native.rs`; use the exact `--test native` command above.

Run the two actual service database cases with180s external bounds, retaining their unchanged55s/85s internal limits, exact snapshots/audit/HTTP/child assertions:

```text
cargo test --offline --locked -p controller-service --test service observation_service::authenticated_service_is_visible_but_never_execution_ready_or_a_writer -- --exact --nocapture --test-threads=1
cargo test --offline --locked -p controller-service --test service observation_service::infrastructure_service::selected_node_service_is_independent_and_never_a_writer -- --exact --nocapture --test-threads=1
```

Finish with60s formatting/whitespace and300s strict all-target/all-feature offline/locked Clippy. Do not claim these macOS runs as Linux execution. Preserve any original failures even when a later justified correction passes. No redundant whole-suite reruns after unchanged documentation edits.

- [ ] **Step 8: Seal, self-review, commit and hand off.**

Record exact BASE/HEAD and SHA256 of all three tested source files, complete commands/UTC/exit/timeout results, real vs synthetic test counts and repeats, capacity/OOM observations and gaps, per-fixture cleanup/event limitations, and unchanged protected inventory. Read the complete diff against the spec. No actual data is destroyed outside the ordinary verified fixture lifecycle; explicitly state temporary database data is volatile and no historical volume was removed.

Stage only the three source paths and use a bounded180s normal local commit:

```text
git add rust-controller/proof_support/mod.rs rust-controller/proof_support/docker_storage_probe.rs rust-controller/crates/postgres-store/tests/docker_fixture_storage.rs
git commit -m "test: qualify owned Docker fixture storage and lifecycle"
```

Use existing configured signing with no key/config access or overrides; no push/merge/publication. Return concise status/commit/test summary/concerns and the complete report path. Main independently reviews source/evidence and obtains a fresh Astra specification/code-quality review before accepting B or dispatching controller Task8. Readiness of the full replacement remains unproven.

## Main implementation amendment: precise event cutoff

This amendment supersedes only the Step3 literal `--until 0s` and adds bounded diagnostic retention. Initial implementation commit3ef1d1e24acc9fd8ed416e8b6e98cc195f6da162 and its failed first live result remain evidence, not acceptance. Main's spec cutoff correction and progress-ledger ruling bind this repair. The installed-CLI private-socket probe `cli-until-probe.py` confirmed duration truncation without touching a real daemon.

1. In the existing probe module add the following test and a temporary private `event_cutoff` returning `"0s".to_owned()`; keep the live call unchanged until the test compiles and fails. Run the existing180s non-Docker decoder-filter command and retain the exact failing formatter assertion. Then replace the formatter body with the implementation below and use it for the actual query. Do not commit a completed behavior with the temporary body.

```rust
#[test]
fn event_cutoff_preserves_subsecond_query_boundary() {
    let at = chrono::DateTime::parse_from_rfc3339("2026-09-06T13:42:33.999999999Z")
        .unwrap().with_timezone(&chrono::Utc);
    assert_eq!(event_cutoff(at), "2026-09-06T13:42:33.999999999Z");
}
fn event_cutoff(at: chrono::DateTime<chrono::Utc>) -> String {
    at.to_rfc3339_opts(chrono::SecondsFormat::Nanos, true)
}
```

2. Immediately before the existing events query, compute `let until = event_cutoff(chrono::Utc::now());` and pass `&until` instead of `"0s"`. No future cutoff, sleep, deadline increase, endpoint retry or changed history predicate. Record the retained Created, cutoff and normalized projection before returning a failed history verdict. Use the following fixed diagnostic form after successful bounded query output, instead of printing unvalidated raw stdout:

```rust
let result = events(&raw, created);
eprintln!("owned_storage_history {} {} created_ns={} until={} result={:?}",
    self.identity().0, self.identity().1, created, until, result);
if result.is_err() {
    for line in raw.lines().take(256) {
        match serde_json::from_str::<(i64, u8)>(line) {
            Ok((at, class)) if at > 0 && class <= 4 =>
                eprintln!("owned_storage_history_row {} [{},{}]", self.identity().1, at, class),
            _ => eprintln!("owned_storage_history_invalid_row {}", self.identity().1),
        }
    }
}
result?;
```

The cap/tuple checks above constrain diagnostics only; the existing strict `events` function remains the sole qualification predicate and still rejects overflow, malformed records, missing earlier witness/endpoints, unordered/duplicate endpoints or attributed mount/unmount. Created is retained by the handle before cleanup; its numeric value and chosen cutoff are now reported on both success and failure. Malformed raw output is never exposed.

3. Run the four decoder bodies GREEN, complete existing helper filter, formatting and strict workspace Clippy under the existing bounds. Recheck local resource/backend gates and run exactly one corrected SQL/explicit-cleanup case under its original180s bound. A full pass permits the remaining Drop/cancellation cases and original Step7 workload/observational gates. A new history/cleanup/capacity/timeout ambiguity stops and returns to main; do not loop for a passing result.

4. Append repair receipts, source hashes and final qualification truth to the existing report; preserve the original three RED tests, original live failure and diagnostic limits separately. Commit the scoped correction normally after its covering checks. Main reviews the entire original BASE-to-final source range, including the initial failed implementation and this repair. No Task8 or full readiness acceptance follows until B's complete gates and independent review pass.

## Main verification amendment: bounded absence settlement

The spec's observer ruling applies only to the ignored Step7 observer, not to the Rust audit, fixture lifecycle, storage admission, event bracket or business tests. Preserve the first incomplete full-durability attempt and its missing failure-boundary output explicitly.

1. Reproduce the observer behavior synthetically before repair: a fixed process read fails, exact same-ID presence is briefly returned, then a successful empty listing follows. Pin that the result must be a gap, never a sample; retain the original failing assertion/exception.
2. Implement the spec's single3-second maximum settlement interval inside the original Cargo deadline, bounded exact-ID-only polling, strict raw listing shapes and before/after terminal checks. Do not retry substantive predicates, allocate new fixtures, change source scope or add cleanup authority.
3. Cover same-ID-to-absence, permanently present, malformed/foreign/multiple IDs, failed/timed-out/clipped queries, elapsed settlement/Cargo deadlines and terminal transitions. Retain existing decoder/process/terminal tests. Main inspects the implementation, exact observer hash and complete RED/GREEN receipts before live execution.
4. Once main explicitly authorizes it, select `lifecycle_heartbeat_extends_only_the_existing_short_lease` alone under an external180-second bound using the corrected observer. Record all complete samples and gaps, test output and exact cleanup results. A passing focused result permits resuming the original serial full regression gates under their unchanged bounds. Any new ambiguity returns to main, not an automatic repeated run.
5. Keep this observer correction distinct from the tested Rust source SHA. The final independent review receives the original BASE-to-final source package, governing amendments, final observer hash and all original/corrected receipts. Deliverable B remains unaccepted until the entire original qualification and review requirements are satisfied.
