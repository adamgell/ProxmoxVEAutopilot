# Task 6 report — Typed PVE read port, local fake, and evidence

## Scope and boundary

Implemented Task 6 only in the isolated `codex/rust-controller-design` worktree from base `df07bdf`. The change adds the read-only `pve-port` crate, a deterministic in-process fake/recorder, a certificate-verifying reqwest/rustls observer, typed evidence evaluation, one synthetic UPID fixture, and an executable network-deny test.

No Proxmox mutation trait or arbitrary command surface exists. No production endpoint was contacted, no production state was read or changed, and no token, credential, tenant identifier, VM inventory, or captured response body was persisted. Every HTTP test server binds to `127.0.0.1` on an ephemeral port.

## Contract implemented

- `PveReadPort` exposes only `vm_config`, `task_status`, `storage_content`, and semantically read-only `qga_ping` methods. QGA ping uses the repository-documented Proxmox `POST /nodes/{node}/qemu/{vmid}/agent/ping` endpoint; no mutation method or trait was added.
- `NodeName`, `StorageName`, `Upid`, `Vmid`, `VmUuid`, `MacAddress`, and `PveBaseUrl` validate before entering request construction. Remote plaintext HTTP, embedded credentials, paths, queries, fragments, traversal-shaped names, invalid VMIDs, nil/invalid UUIDs, and malformed MACs fail closed.
- `PveEvidence` is timestamped and source-identified and contains typed task-completion and VM-identity facts. UPID success remains independent from the intended VM UUID/MAC postcondition.
- Evidence health maps unauthorized, timed-out, unavailable, stale, and contradictory observations onto the existing closed `ObservationHealth` domain enum without changing execution state.
- `FakePve` queues per-method typed responses and records typed requests in deterministic order. Queue exhaustion returns `NotFound` rather than inventing a response.
- `ReqwestPveObserver` uses the workspace's `reqwest` dependency with `default-features = false` and `rustls-tls`. Its public API does not accept a caller-supplied client or an insecure-certificate option. Non-loopback URLs require HTTPS.
- `192.168.2.4` is rejected unless the mode is exactly `observe` and explicit production-read permission is true. The rejection happens before the private verified-client builder is called. A request audit proves zero attempted HTTP requests on denial.

## TDD evidence

### Initial RED

The initially planned `wiremock` dev dependency was unavailable in the local Cargo cache. The first offline command stopped with:

```text
error: no matching package named `wiremock` found
location searched: crates.io index
```

It was not fetched. The tests were changed to use a small Tokio loopback-only responder, preserving the fake-service behavior without adding a dependency.

The next offline test run reached the intended RED and failed because the Task 6 API did not exist:

```text
error[E0432]: unresolved imports `super::CloneIntent`, `super::FakePve`,
`super::PveReadPort`, `super::ReqwestPveObserver`, ...
```

The same run also failed because the sanitized `upid-complete.json` fixture did not yet exist. This was the expected absence-of-feature failure.

### Initial GREEN

After the minimal implementation:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p pve-port --lib
```

Result: **11 passed, 0 failed**.

### Security repair RED/GREEN

Self-review found that the first test seam accepted a caller-supplied reqwest client, which could bypass certificate verification. Tests were changed first to require a request audit, a build-observer boundary, and the documented QGA POST route. The repair RED failed with unresolved `PveRequestAudit`, missing boundary API, and the pre-existing GET behavior.

The implementation now keeps the client builder private and crate-owned. Focused repair verification passed the QGA route test and both network-deny tests. The final focused crate run has **13 unit tests and 2 network-deny integration tests**, all passing.

An explicit mutation check changed the implemented QGA method back to `GET`; the focused test failed on the literal required `POST /api2/json/nodes/pve-test/qemu/101/agent/ping` request line. Restoring `POST` made the same test pass.

## Virtual-workspace integration-test harness

Cargo ignores `rust-controller/tests/*.rs` as integration targets because the root manifest is a virtual workspace. The requested canonical file remains at `rust-controller/tests/network_deny.rs`. A minimal member-crate harness at `rust-controller/crates/pve-port/tests/network_deny.rs` contains only:

```rust
include!("../../../tests/network_deny.rs");
```

The `pve-port` manifest declares that harness as the unique `network_deny` test target. Therefore the requested command works unchanged:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --test network_deny
```

## Covered adversarial cases

- successful UPID with absent VM postcondition;
- 401 and 403 mapped to unauthorized without including response bodies in errors;
- 404 and 409 retained as distinct typed read errors and normalized to unavailable evidence;
- delayed loopback response mapped to timed out;
- observations older than the configured maximum age mapped to stale;
- mismatched UUID or MAC mapped to contradicted and never identity-satisfied;
- queued running then late-complete UPID facts without replaying fake responses;
- production denial before client construction and with zero audited requests;
- production observe mode still requiring explicit read permission;
- documented QGA POST method and exact typed endpoint path;
- sanitized completed-UPID fixture decoding.

## Dependency handling

All Cargo resolution and compilation used `--offline`; final checks also use `--locked`. `reqwest` and its rustls dependency graph were already present in the local Cargo cache and were locked without network access. `wiremock` was not present and was removed rather than fetched.

## Final verification

Fresh final commands completed successfully:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p pve-port
# 13 unit + 2 network-deny integration tests passed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --test network_deny
# 2 passed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast
# 99 unit, integration, and doc tests passed; 0 failed

cargo clippy --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
# passed with warnings denied

cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
git diff --check
# both passed
```

The resolved feature tree contains `reqwest` -> `hyper-rustls` -> `rustls` with WebPKI roots and no native-TLS or invalid-certificate feature. A scoped source audit found no mutation trait, process execution, arbitrary command, SSH, Ansible, native-TLS, or invalid-certificate escape hatch in Task 6 files.

## Remaining concern

The approved Task 6 interface does not define token or ticket injection, so this observer deliberately adds no credential transport. It can exercise local anonymous contract servers now; authenticated PVE observation requires a later typed, redacted credential boundary. This task does not claim live-Proxmox readiness, deployment proof, or production acceptance.
