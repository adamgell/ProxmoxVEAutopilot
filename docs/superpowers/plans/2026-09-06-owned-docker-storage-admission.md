# Owned Docker Storage Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement deliverable A of the owned fixture storage prerequisite: fixed bounded tmpfs launch plus fail-closed storage admission, proved with compiled non-live tests.

**Architecture:** Keep the existing owned container lifecycle and process supervisor. Extract the actual Docker launch arguments without behavior changes for a compiled regression, then add a fixed PGDATA override and a separate narrow storage projection after existing identity admission. The Linux receipt branch is unchanged; live lifecycle/capacity qualification is deliverable B and is not acceptance of this plan.

**Tech Stack:** Existing Rust workspace, serde_json, Tokio, cached PostgreSQL Docker fixtures; no dependency changes.

**Spec:** `docs/superpowers/specs/2026-09-06-owned-docker-fixture-storage.md`, including its two-deliverable sequencing.

## Global Constraints

- Fixed compiled `--tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=1g,mode=0700`; no caller/environment-selected path, options, size or mount.
- Preserve PostgreSQL defaults, including durability settings; no memory cap, tuning, checkpoint workaround or timeout increase.
- Preserve cached `postgres:16-alpine`, `--pull=never`, local Unix Docker endpoint, generated exact name/label/full-ID ownership, loopback-only dynamic publish and pending guard before create.
- Preserve setup30s, command3s, shared cleanup3s and existing bounded child cleanup allowances. Existing process runner only.
- Preserve exact-ID `rm --force` with no volume deletion. A mount mismatch cannot grant cleanup of a foreign container; an owned mount mismatch must still receive ordinary container cleanup.
- No Linux receipt backend, frozen candidate gates, production module, Cargo dependency/lockfile or existing business-test changes.
- No existing-volume deletion, pruning, Docker reset, production access, key/config changes, publication or new live fixtures in this task.
- Existing local build guard is 8GiB available. Below it, prepare tests only and report NEEDS_CONTEXT; do not compile, implement the behavioral fix or waive RED. Main will explicitly clear runtime work after rechecking resources. Linux artifact guard18GiB is separate.
- Actual filesystem/capacity, SQL, Drop/cancellation daemon lifecycle, exact absence/event and broad regression proofs remain deliverable B. Passing this task alone never permits broad fixture use.
- Reports retain exact source/hash, bounded command/UTC/exit evidence and original failures. Do not claim a synthetic parser test proves daemon behavior. No subagents from the implementer; main owns independent review.

## File structure and boundary

Only modify `rust-controller/proof_support/mod.rs`: constants, the actual launch-argument extraction, a private storage decoder and its startup call, and non-live unit tests. Do not create the later `docker_fixture_storage.rs` harness or observation API yet. Do not modify `process.rs`: its bounded runner already returns an error for output overflow and kills/reaps direct children. Existing helper tests are path-included by multiple crates, so distinguish repeated executions from distinct bodies.

### Task 1: Prevent persistent PGDATA selection and refuse unexpected storage

**Files:**
- Modify/Test: `rust-controller/proof_support/mod.rs`.

**Interfaces:**
- Consumes unchanged `process::docker(args: &[&str]) -> io::Result<std::process::Output>`, `DockerOwned::verified_owned_id(&self, output: &std::process::Output) -> Option<String>` and `Container::start() -> (Self, String)`.
- Produces private `DockerOwned::run_args(&self) -> Vec<String>` and `DockerOwned::storage_admitted(&self, output: &std::process::Output) -> bool`.
- Produces fixed private constants `PGDATA`, `TMPFS_OPTIONS`, `STORAGE_FORMAT`. No new public controller or test observation API.
- Later deliverable B consumes the unchanged Container API and validates the storage projection/actual filesystem independently; it must not treat this boolean as a deletion capability.

- [ ] **Step 1: Extract the actual unchanged launch vector and write regression tests.**

Move the current literal arguments into this private function and pass its borrowed strings to the existing `process::docker` call. For the RED baseline only, retain the original argument list without the two `--tmpfs` entries shown in Step3. Do not alter order, image, DB environment or ownership. Add the storage method initially returning `output.status.success()` to expose the currently absent storage predicate; explicitly label this permissive baseline in the report, not as a production implementation. No Docker command runs during these tests.

```rust
#[cfg(test)]
mod storage_tests {
    use super::*;
    use serde_json::{Value, json};
    use std::os::unix::process::ExitStatusExt;

    fn owned() -> DockerOwned {
        let mut owned = DockerOwned::pending("unix:///unused-test-only".into());
        owned.id = Some("a".repeat(64));
        owned
    }
    fn output(value: Value) -> std::process::Output {
        std::process::Output {
            status: std::process::ExitStatus::from_raw(0),
            stdout: serde_json::to_vec(&value).unwrap(), stderr: vec![],
        }
    }
    fn projection() -> Value {
        json!({"id":"a".repeat(64),
            "tmpfs":{"/var/lib/postgresql/data":"rw,nosuid,nodev,size=1g,mode=0700"},
            "binds":null,"volumes_from":null,"mounts":[]})
    }
    #[test]
    fn actual_launch_selects_fixed_bounded_pgdata_tmpfs() {
        let args = owned().run_args();
        let selected: Vec<_> = args.windows(2)
            .filter(|pair| pair[0] == "--tmpfs").collect();
        assert_eq!(selected.len(), 1);
        assert_eq!(selected[0][1],
            "/var/lib/postgresql/data:rw,nosuid,nodev,size=1g,mode=0700");
        assert!(args.iter().any(|arg| arg == "--pull=never"));
        assert_eq!(args.last().unwrap(), "postgres:16-alpine");
        assert!(!args.iter().any(|arg| matches!(arg.as_str(), "-v"|"--volume"|"--mount")));
    }
    #[test]
    fn ownership_valid_persistent_pgdata_is_refused() {
        let owned = owned();
        let ownership = std::process::Output {
            stdout: format!("{}|/{}|{}|{}", "a".repeat(64), owned.name, owned.name, IMAGE).into_bytes(),
            ..output(json!(null))
        };
        assert_eq!(owned.verified_owned_id(&ownership), owned.id);
        let mut value = projection();
        value["tmpfs"] = Value::Null;
        value["mounts"] = json!([{"Type":"volume","Destination":"/var/lib/postgresql/data","Name":"unexpected"}]);
        assert!(!owned.storage_admitted(&output(value)));
    }
}
```

- [ ] **Step 2: Compile and observe both behavioral failures without Docker.**

Only after main clears the resource guard, run from `rust-controller` under the existing finite process-group watchdog with180s total, TERM then bounded KILL/reap:

`cargo test --offline --locked -p postgres-store --lib storage_tests -- --nocapture --test-threads=1`

Expected two compiled assertion failures: zero selected tmpfs entries and an incorrectly admitted persistent-storage projection. A compiler error is not behavioral RED: correct test/extraction compilation without implementing the repair, then rerun this bounded command. Store the complete command, UTC start, termination/exit and relevant output. Do not run any real fixture for RED.

- [ ] **Step 3: Add the fixed launch option and implement private fail-closed admission.**

```rust
const PGDATA: &str = "/var/lib/postgresql/data";
const TMPFS_OPTIONS: &str = "rw,nosuid,nodev,size=1g,mode=0700";
const STORAGE_FORMAT: &str = r#"{"id":{{json .Id}},"tmpfs":{{json .HostConfig.Tmpfs}},"binds":{{json .HostConfig.Binds}},"volumes_from":{{json .HostConfig.VolumesFrom}},"mounts":{{json .Mounts}}}"#;

impl DockerOwned {
    fn run_args(&self) -> Vec<String> {
        vec!["--host".into(), self.endpoint.clone(), "run".into(),
            "--pull=never".into(), "--detach".into(), "--name".into(), self.name.clone(),
            "--label".into(), format!("{OWNERSHIP_LABEL}={}", self.name),
            "--env".into(), "POSTGRES_PASSWORD=postgres".into(),
            "--env".into(), format!("POSTGRES_DB={}", super::LOCAL_DATABASE_NAME),
            "--publish".into(), "127.0.0.1::5432".into(),
            "--tmpfs".into(), format!("{PGDATA}:{TMPFS_OPTIONS}"), IMAGE.into()]
    }
    fn storage_admitted(&self, output: &std::process::Output) -> bool {
        if !output.status.success() { return false; }
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(&output.stdout) else {
            return false;
        };
        let Some(object) = value.as_object() else { return false; };
        if object.len() != 5 { return false; }
        let Some(id) = self.id.as_deref().filter(|id| valid_id(id)) else { return false; };
        if object.get("id").and_then(|value| value.as_str()) != Some(id) { return false; }
        let Some(tmpfs) = object.get("tmpfs").and_then(|value| value.as_object()) else { return false; };
        if tmpfs.len() != 1 || tmpfs.get(PGDATA).and_then(|value| value.as_str()) != Some(TMPFS_OPTIONS) {
            return false;
        }
        for key in ["binds", "volumes_from"] {
            match object.get(key) {
                Some(serde_json::Value::Null) => {},
                Some(serde_json::Value::Array(values)) if values.is_empty() => {},
                _ => return false,
            }
        }
        let Some(mounts) = object.get("mounts").and_then(|value| value.as_array()) else { return false; };
        if mounts.is_empty() { return true; }
        if mounts.len() != 1 { return false; }
        let mount = &mounts[0];
        mount.get("Type").and_then(|v| v.as_str()) == Some("tmpfs")
            && mount.get("Destination").and_then(|v| v.as_str()) == Some(PGDATA)
            && mount.get("Source").and_then(|v| v.as_str()) == Some("")
            && mount.get("RW").and_then(|v| v.as_bool()) == Some(true)
    }
}
```

At the current launch call, replace only the arguments with the actual extracted vector:

```rust
let args = container.run_args();
let borrowed: Vec<_> = args.iter().map(String::as_str).collect();
let out = process::docker(&borrowed).await.expect("local_database_unavailable");
```

Retain all existing create status/full-ID and ownership checks. Immediately after the existing `local_ownership_unconfirmed` assertion, before publish inspection/DSN yield, add:

```rust
let storage = process::docker(&[
    "--host", &container.endpoint, "inspect", "--format", STORAGE_FORMAT, &id,
]).await.expect("local_storage_unconfirmed");
assert!(container.storage_admitted(&storage), "local_storage_unconfirmed");
```

Do not print the argument vector or raw inspect/environment. A failed/overflowed runner or malformed projection panics with a fixed label while the already established owned guard still performs its original finite cleanup. Do not add storage checks to cleanup ownership: unexpected owned mounts do not remove the existing obligation to clean only that container.

- [ ] **Step 4: Add exact synthetic acceptance/refusal and cleanup-separation coverage.**

Add to `storage_tests` using its existing constructors:

```rust
#[test]
fn both_bounded_tmpfs_representations_are_admitted() {
    let owned = owned();
    assert!(owned.storage_admitted(&output(projection())));
    let mut value = projection();
    value["binds"] = json!([]);
    value["volumes_from"] = json!([]);
    value["mounts"] = json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":true}]);
    assert!(owned.storage_admitted(&output(value)));
}
#[test]
fn storage_projection_rejects_each_wrong_field_and_failed_output() {
    let owned = owned();
    for (key, wrong) in [
        ("id", json!("b".repeat(64))), ("id", json!("a")),
        ("tmpfs", json!(null)), ("tmpfs", json!({})),
        ("tmpfs", json!({"/wrong":"rw,nosuid,nodev,size=1g,mode=0700"})),
        ("tmpfs", json!({PGDATA:"rw"})),
        ("tmpfs", json!({PGDATA:"rw,nosuid,nodev,size=2g,mode=0700"})),
        ("tmpfs", json!({PGDATA:TMPFS_OPTIONS,"/extra":TMPFS_OPTIONS})),
        ("binds", json!(["/outside:/var/lib/postgresql/data"])),
        ("binds", json!("")), ("volumes_from", json!(["foreign"])),
        ("mounts", json!(null)), ("mounts", json!([{"Type":"volume","Destination":PGDATA}])),
        ("mounts", json!([{"Type":"bind","Destination":PGDATA}])),
        ("mounts", json!([{"Type":"tmpfs","Destination":"/extra","Source":"","RW":true}])),
        ("mounts", json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"foreign","RW":true}])),
        ("mounts", json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":false}])),
        ("mounts", json!([{"Type":"tmpfs","Destination":PGDATA,"Source":"","RW":true},{}])),
    ] {
        let mut value = projection(); value[key] = wrong;
        assert!(!owned.storage_admitted(&output(value)), "accepted wrong field {key}");
    }
    for key in ["id", "tmpfs", "binds", "volumes_from", "mounts"] {
        let mut value = projection(); value.as_object_mut().unwrap().remove(key);
        assert!(!owned.storage_admitted(&output(value)));
    }
    for bytes in [b"{".as_slice(), b"null", b"[]", b"\xff"] {
        let mut out = output(projection()); out.stdout = bytes.to_vec();
        assert!(!owned.storage_admitted(&out));
    }
    let mut failed = output(projection());
    failed.status = std::process::ExitStatus::from_raw(256);
    assert!(!owned.storage_admitted(&failed));
    let mut pending = owned; pending.id = None;
    assert!(!pending.storage_admitted(&output(projection())));
}
```

Inside the existing `cleanup_tests` module add this test, using its original closed script (no Docker):

```rust
#[test]
fn storage_refusal_preserves_one_shot_container_only_cleanup() {
    use std::os::unix::process::ExitStatusExt;
    let (mut container, removals, pids) = container(Scenario::Owned);
    container.docker_owned().id = Some("a".repeat(64));
    let bad = std::process::Output {
        status: std::process::ExitStatus::from_raw(0),
        stdout: b"{}".to_vec(), stderr: vec![],
    };
    assert!(!container.docker_owned().storage_admitted(&bad));
    assert_eq!(container.cleanup(), Ok(()));
    let count = pids.lock().unwrap().len();
    drop(container);
    assert_eq!(pids.lock().unwrap().len(), count);
    assert_eq!(*removals.lock().unwrap(), vec!["a".repeat(64)]);
    for pid in pids.lock().unwrap().iter() { process::assert_child_reaped(*pid); }
}
```

Retain existing tests verbatim. This new cleanup test proves refusal does not disable the existing one-shot identity guard; unchanged `remove_owned` source and exact argv diff establish no added volume removal. It does not prove actual daemon volume absence.

- [ ] **Step 5: Run bounded non-live GREEN and shared regression gates.**

With the local resource guard cleared, run serially from `rust-controller`, preserving the original finite watchdog's kill/reap behavior and complete receipts:

-180s: `cargo test --offline --locked -p postgres-store --lib storage_tests -- --nocapture --test-threads=1`.
-180s: `cargo test --offline --locked -p postgres-store --lib local_postgres -- --nocapture --test-threads=1`. Inspect the list first with `-- --list` under180s and prove every selected body is a pure parser/ownership/process/Linux-protocol synthetic helper. If the module name differs, select the exact verified helper prefix; do not widen into DB fixture bodies.
-60s: `cargo fmt --all -- --check`.
-300s: `cargo clippy --offline --locked --workspace --all-targets --all-features -- -D warnings` (compile only, no Docker).
-60s: `git diff --check` from the worktree.

On unexpected timeout, stack abort, resource depletion or cleanup uncertainty, preserve the original result and return to main; do not extend timeouts or silently rerun broad suites. No Rust stack-limit change. Formatting is a normal mechanical tool action permitted in the single authorized file. If formatting affects another file, stop and report rather than overwrite it.

- [ ] **Step 6: Self-review, seal, commit and hand off for independent review.**

Check the exact single-file diff against every global constraint, verify tested source SHA-256 still matches, and keep all non-live helper counts distinct from repeated includers. Record actual start/exit times rather than treating output collection as process end. Commit only:

```text
git add rust-controller/proof_support/mod.rs
git commit -m "fix: bound owned Docker fixture storage and refuse persistent mounts"
```

Run the commit through the existing180s watchdog and normal configured local signing; no credential/config access or override. Report exact BASE/HEAD, original RED and GREEN commands/outputs, source hash, unchanged-path checks, scope limits and resource observations to this plan's `task-1-report.md`. Return only status/commit/test summary/concerns. Main generates the pinned diff package and obtains fresh independent specification and code-quality review before acceptance. Deliverable B remains unimplemented and no actual tmpfs capacity/lifecycle result is claimed by this commit.
