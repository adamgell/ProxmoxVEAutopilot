# Task 6 report — Typed PVE read port, local fake, and evidence

## Scope and boundary

Implemented Task 6 only in the isolated `codex/rust-controller-design` worktree from base `df07bdf`. The change adds the read-only `pve-port` crate, a deterministic in-process fake/recorder, a certificate-verifying reqwest/rustls observer, typed evidence evaluation, two synthetic task-status fixtures, and an executable network-deny test.

No Proxmox mutation trait or arbitrary command surface exists. No production endpoint was contacted, no production state was read or changed, and no token, credential, tenant identifier, VM inventory, or captured response body was persisted. Every HTTP test server binds to `127.0.0.1` on an ephemeral port.

## Contract implemented

- `PveReadPort` exposes only `vm_config`, `task_status`, `storage_content`, and semantically read-only `qga_ping` methods. QGA ping uses the repository-documented Proxmox `POST /nodes/{node}/qemu/{vmid}/agent/ping` endpoint; no mutation method or trait was added.
- `NodeName`, `StorageName`, `Upid`, `Vmid`, `VmUuid`, `MacAddress`, and `PveBaseUrl` validate before entering request construction. `Upid` parses the documented node, hexadecimal process/task fields, worker type/id, and authenticated user shape; both clone intent construction and task-status routing reject a node mismatch before recording a request. Remote plaintext HTTP, embedded credentials, paths, queries, fragments, traversal-shaped names, invalid VMIDs, nil/invalid UUIDs, and malformed MACs fail closed.
- `PveEvidence` is timestamped and source-identified and preserves the full `TaskState` in both the evidence record and task fact. The compatibility `task_complete` boolean remains, but `Running` and `CompleteFailure` are no longer observationally collapsed. UPID success remains independent from the intended VM UUID/MAC postcondition.
- Evidence health maps unauthorized, timed-out, unavailable, stale, and contradictory observations onto the existing closed `ObservationHealth` domain enum without changing execution state.
- `FakePve` queues per-method typed responses and records typed requests in deterministic order. Queue exhaustion returns `NotFound` rather than inventing a response.
- `ReqwestPveObserver` uses the workspace's `reqwest` dependency with `default-features = false` and `rustls-tls`. Its public API does not accept a caller-supplied client or an insecure-certificate option. System/environment proxies, redirect following, and automatic retries are disabled. Non-loopback URLs require HTTPS.
- Without explicit observe-plus-read permission, only normalized literal loopback targets are accepted. Literal `192.168.2.4`, its IPv4-mapped IPv6 form, and every hostname/non-loopback address are rejected before the private verified-client builder is called. With the explicit permission, a validated configured HTTPS target may be constructed. A request audit proves zero attempted HTTP requests on denial.

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

### Review round-one repair RED/GREEN

The target policy tests were added before the repair. Their RED run showed three concrete defects: a non-loopback hostname was accepted in adapter mode, the IPv4-mapped form of `192.168.2.4` was accepted, and the bracketed IPv6 loopback URL was rejected as invalid. The focused run reported **2 passed, 3 failed**. The replacement policy then allowed only normalized literal loopback addresses by default and required both observe mode and explicit read permission for every other validated target. The same suite passed with **5 passed, 0 failed**, and a private counting builder independently remained at zero builds for each denied target.

The documented-UPID shape test was added before replacing the printable-prefix validator. Its RED run accepted the malformed value `UPID:pve-test`. The parser now exposes typed node, process ID, process-start, task-start, worker type, optional worker ID, and authenticated-user fields, and rejects malformed structure and route-node disagreement. Separate pre-request tests for clone intent and the HTTP observer now pass.

Task-state evidence assertions were added before the evidence model changed. The RED run failed to compile because `PveEvidence` had no `task_state` field. The evaluator now preserves `Running`, `CompleteSuccess`, and `CompleteFailure`; running/late-complete and terminal-failure focused tests pass.

The redirect test was added before changing the reqwest client. Its RED run followed a 302 to a second loopback server and returned a successful task status. After applying `Policy::none()`, the same test returned a typed transport error and the second server recorded zero requests. A second loopback-only test serves a literal `Location: https://192.168.2.4:8006/...`; it records only the originating loopback request, rejects the 302, and never performs a production hop.

All loopback test responders now retain their task handles, read through the HTTP header terminator with a 16 KiB bound, join on normal completion, abort on bounded cleanup failure/drop, and return deterministic captured-request lists. No test resolves a hostname or contacts a non-loopback target.

### Review round-two repair RED/GREEN

The proxy-environment regression was added before disabling system proxies. It runs in an exact child copy of the current test binary, sets upper- and lower-case `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` to one loopback recorder, clears both `NO_PROXY` forms, and targets a separate loopback recorder. Its RED run failed with `TransportUnavailable` because the proxy recorder received the request. After applying `ClientBuilder::no_proxy()`, the child passed with zero proxy requests and exactly one target request. Environment mutation is confined to a current-thread child process, previous values are restored by an RAII guard, and the parent process environment is never changed.

The retry regression was added before the hardened builder path existed and produced the intended compile RED. It seeds a reqwest builder with a policy that classifies a local 503 response as retryable for two additional attempts. The production builder now overrides all retry behavior with `reqwest::retry::never()`. The GREEN run records exactly one wire request and returns the original local 503 response. The seedable builder wrapper is compiled only for tests; the production observer remains crate-owned and accepts no client or builder from callers.

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
- terminal task failure preserved distinctly from running in evidence and facts;
- documented Proxmox UPID parsing and embedded-node/route-node binding;
- every non-loopback target denied before client construction and with zero audited requests unless observe-plus-read permission is explicit;
- literal production IPv4, IPv4-mapped IPv6, hostname alias, native IPv6 loopback, and IPv4-mapped IPv6 loopback policy cases;
- redirect suppression with a zero-request second hop and a literal redirect-to-production response handled locally;
- proxy-environment suppression with separate loopback proxy and target recorders;
- explicit never-retry behavior under a locally retry-classified 503 response, with one wire attempt;
- documented QGA POST method and exact typed endpoint path;
- sanitized completed and failed task-status fixture decoding.

## Dependency handling

All Cargo resolution and compilation used `--offline`; final checks also use `--locked`. `reqwest` and its rustls dependency graph were already present in the local Cargo cache and were locked without network access. `wiremock` was not present and was removed rather than fetched.

## Final verification

Fresh final commands completed successfully:

```text
cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p pve-port
# 22 unit + 5 network-deny integration tests passed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --test network_deny
# 5 passed

cargo test --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-features --no-fail-fast
# 111 unit, integration, and doc tests passed; 0 failed

cargo clippy --offline --locked --manifest-path rust-controller/Cargo.toml --workspace --all-targets --all-features -- -D warnings
# passed with warnings denied

cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check
git diff --check
# both passed
```

The resolved feature tree contains `reqwest` -> `hyper-rustls` -> `rustls` with WebPKI roots and no native-TLS or invalid-certificate feature. A scoped production-source audit found no mutation trait, process execution, arbitrary command, SSH, Ansible, native-TLS, or invalid-certificate escape hatch. The proxy regression's parent test uses `std::process::Command` only to re-execute the exact current test binary with an isolated environment; it cannot select an arbitrary executable or command payload.

## Remaining concern

The approved Task 6 interface does not define token or ticket injection, so this observer deliberately adds no credential transport. It can exercise local anonymous contract servers now; authenticated PVE observation requires a later typed, redacted credential boundary. This task does not claim live-Proxmox readiness, deployment proof, or production acceptance.
