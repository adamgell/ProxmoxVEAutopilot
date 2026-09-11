# Rust Node and Network Read Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, non-authoritative node/network visibility contracts and authenticated GET implementations without changing service activation or native execution.

**Architecture:** Separate permissive observation DTOs and a new two-method trait preserve existing strict native preflight types. The existing verified HTTP client supplies authenticated, bounded GETs. This independently testable transport slice precedes explicit target-selection and service scheduling integration.

**Tech Stack:** Existing Rust1.92/edition2024, Tokio, Reqwest/rustls, Serde, owned loopback fixtures; no new dependency or version change.

**Spec:** docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md sections8,11,16–21; preceding authenticated-observation plan remaining infrastructure scope.

## Global Constraints

- "The current production controller at `192.168.2.4` remains unchanged until a separately approved cutover."
- "During local development the Rust controller uses a separate database and namespace. It must never claim production `jobs` rows."
- "Secrets never appear in argv, logs, journal payloads, health details, or fixtures."
- "RustedOutClient is out of scope. It is neither a dependency nor a delivery target for this program."
- No real PVE/controller requests, production credentials, SSH, QGA, storage activation, POST, tenant access, deployment, publication or schema changes. Owned loopback tests only.
- Preserve strict native/preflight DTOs, broad read behavior, accepted scheduler/evidence/fencing, cluster visibility and all service behavior. No new transport selector, target environment setting or runtime collection in this slice.
- Storage content/status GETs are excluded: pinned official sources invoke storage activation, and selected status can activate additional enabled storages. GET-only is not a side-effect guarantee. See retained next-slice-storage-side-effects.md research; not installed-release proof.
- One Astra implementer per task, independent review per task, final consolidated review, one coordinated repair wave if needed, scoped re-review, then exact-source artifact proof. Start only after predecessor acceptance. User already chose subagents; no execution-choice prompt.

## Files and interfaces

Create `rust-controller/crates/pve-port/src/infrastructure_visibility.rs` for DTOs and narrow trait; `tests/infrastructure_visibility.rs` for parser/capability tests; `tests/infrastructure_visibility_http.rs` for owned HTTP contracts. Modify `src/lib.rs` explicit exports and `src/observer.rs` bounded GET implementation. Update README/Dockerfile.test only for this scoped capability and Linux selection at Task2.

No extraction of existing strict parsers or generic executor API. New trait has no supertrait and no methods beyond node/network GET visibility. Types have private fields, sanitized Serialize only, no Deserialize or conversion into native evidence. Every report returns coverage `unverified`.

## Task 1: Observation-specific node and network contracts

**Files:** Create infrastructure_visibility.rs and tests/infrastructure_visibility.rs; modify lib.rs exports only.

**Interfaces:** `NodeVisibility::from_wire(node: NodeName, value: Value, observed_at: DateTime<Utc>) -> Result<Self,PveReadError>`; getters node(), uptime()->Option<u64>, observed_at(), coverage(). `NetworkVisibility::from_wire` has the same inputs/result shape and getters node(), records()->&[VisibleInterface], rejected_rows()->usize, observed_at(), coverage(). `VisibleInterface` getters name()->&str, kind()->InterfaceKind, active()->Option<bool>. `InterfaceKind::{LinuxBridge,OvsBridge,Other}` serializes snake_case; no raw type is retained. New `#[async_trait] pub trait PveInfrastructureVisibilityReadPort: Send + Sync` defines `async fn node_visibility(&self,node:&NodeName)->Result<NodeVisibility,PveReadError>` and `async fn network_visibility(&self,node:&NodeName)->Result<NetworkVisibility,PveReadError>` only.

- [ ] **Step1: Write missing-API tests, then behavioral RED with minimal skeletons.** Include this exact boundary:

```rust
#[test]
fn zero_uptime_is_visibility_not_online_authority() {
    let node = NodeName::parse("pve-test").unwrap();
    let report = NodeVisibility::from_wire(node, serde_json::json!({"uptime":0}), chrono::Utc::now()).unwrap();
    assert_eq!(report.uptime(), Some(0));
    assert_eq!(report.coverage(), "unverified");
}
```

Node data must be an object; optional node field if present must be a matching valid NodeName. Missing uptime=>None; present nonnegative u64 including0 accepted; negative/fraction/string/null invalid. No derived online or execution-ready field. Drop arbitrary fields including names, certificates, CPU/memory details.

Network input must be an array of at most1024 rows. Each row is an object with iface string of1..64 ASCII alphanumeric or `._:-` bytes; this bounded opaque interface policy is deliberately separate from BridgeName execution policy. Optional node if present must match the requested node. Parse valid iface identity into a seen set before optional metadata so malformed metadata cannot hide duplicates; duplicate identity rejects entire report. Reject/count malformed rows, including missing/nonstring type. Exact type bridge=>LinuxBridge, OVSBridge=>OvsBridge, other string=>Other. Missing active=>None; integer0/1=>false/true; other present value=>rejected row. Preserve wire order; retain neither raw unknown type nor addresses/comments/configuration. Cross-node row rejects entire response, not silently an unrelated row.

```rust
#[test]
fn ovs_and_missing_activity_remain_visible() {
    let node = NodeName::parse("pve-test").unwrap();
    let report = NetworkVisibility::from_wire(node, serde_json::json!([
        {"iface":"vmbr0","type":"bridge","active":0},
        {"iface":"ovs0","type":"OVSBridge"}
    ]), chrono::Utc::now()).unwrap();
    assert_eq!(report.records()[1].kind(), InterfaceKind::OvsBridge);
    assert_eq!(report.records()[1].active(), None);
    assert_eq!(report.rejected_rows(), 0);
    assert_eq!(report.coverage(), "unverified");
}
```

- [ ] **Step2: Run focused RED.** `cargo test --locked --manifest-path rust-controller/Cargo.toml -p pve-port --test infrastructure_visibility`. Pin missing uptime, malformed node/uptime, nonobject, exact interface length/character boundary, malformed row count, unknown kind, active0/1/missing/invalid, duplicate after metadata rejection,1024/1025 rows, empty still unverified, redacted arbitrary fields and timestamp preservation in separate tests.
- [ ] **Step3: Implement private parser helpers and exports.** Use existing NodeName constructor and fixed PveReadError::InvalidResponse only; no raw error strings. Implement getters and fixed coverage serialization following ClusterVisibility convention. Do not implement online/has_active/absence or native conversion.

```rust
#[async_trait::async_trait]
pub trait PveInfrastructureVisibilityReadPort: Send + Sync {
    async fn node_visibility(&self, node: &NodeName) -> Result<NodeVisibility, PveReadError>;
    async fn network_visibility(&self, node: &NodeName) -> Result<NetworkVisibility, PveReadError>;
}
```

Add compile-fail doctests attempting qga_ping, clone, native node_status, and conversion into NodeStatus through this trait/type. These must fail from unavailable authority, not an unrelated syntax error.
- [ ] **Step4: Run focused/full PVE tests+doctests, fmt, strict Clippy.** `cargo test --locked --manifest-path rust-controller/Cargo.toml -p pve-port`; `cargo fmt --manifest-path rust-controller/Cargo.toml --all -- --check`; `cargo clippy --locked --manifest-path rust-controller/Cargo.toml -p pve-port --all-targets -- -D warnings`. Existing strict preflight tests must remain unchanged and pass.
- [ ] **Step5: Commit only task files** as `feat(rust): add node and network visibility contracts`; report exact parser policies and authority omissions.

## Task 2: Bounded authenticated node and network GETs

**Files:** Modify observer.rs; create tests/infrastructure_visibility_http.rs; scoped README and Dockerfile.test updates. Preserve legacy broad methods and current cluster behavior.

**Interfaces:** Implement the new two-method trait for ReqwestPveObserver. Each call makes exactly one authenticated GET to `/api2/json/nodes/{validated-node}/status` or `/api2/json/nodes/{validated-node}/network`, no query or discovery. Use existing URL segment builder and sensitive token header. Every new method has an explicit2second whole request/body deadline even if caller constructed an observer with a larger timeout; total cancellation drops response/read. Body cap1048576 bytes, network rows1024, no retry/redirect/proxy/QGA/storage call.

- [ ] **Step1: Write actual HTTP RED.** Owned ephemeral loopback server checks exact Authorization and route, returns an incomplete but valid payload. Example assertion at fixture boundary:

```rust
assert!(request.starts_with("GET /api2/json/nodes/pve-test/network HTTP/1.1\r\n"));
assert!(request.contains("authorization: PVEAPIToken=observer@pve!fixture=synthetic-secret\r\n"));
```

Call the new trait method through `&dyn PveInfrastructureVisibilityReadPort`, require exactly1 request, and check OVS/missing-active/zero-uptime DTO semantics. Use an owned listener guard that aborts/joins background tasks; never use a user's endpoint.
- [ ] **Step2: Run focused tests RED.** `cargo test --locked --manifest-path rust-controller/Cargo.toml -p pve-port --test infrastructure_visibility_http`. Add separate401/403 unauthorized,404 unavailable,500 unavailable, malformed envelope/data, duplicate/node mismatch, declared/chunked/EOF oversize,1025rows,2second wholebody deadline despite larger client timeout, cancellation, redirect/proxy untouched canaries, exact1request/no unexpected route cases. Use existing loopback fixture patterns; no source-string capability assertions.
- [ ] **Step3: Implement bounded GET sharing only private transport mechanics.** Existing bounded_visibility_body may be reused directly. Factor a private bounded authenticated GET helper only if necessary to avoid duplication; do not route broad legacy methods through a changed parser or alter cluster limits. Wrap new whole collection as:

```rust
async fn infrastructure_data(
    &self, node: &NodeName, leaf: &'static str,
) -> Result<(serde_json::Value, chrono::DateTime<Utc>), PveReadError> {
    tokio::time::timeout(Duration::from_secs(2), async {
        let url = self.base_url.endpoint(&["api2", "json", "nodes", node.as_str(), leaf]);
        self.request_audit.record_request();
        let mut request = self.client.get(url);
        if let Some(token) = &self.token {
            request = request.header(reqwest::header::AUTHORIZATION, token.header());
        }
        let response = request.send().await.map_err(map_reqwest_error)?;
        match response.status() {
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => return Err(PveReadError::Unauthorized),
            StatusCode::NOT_FOUND => return Err(PveReadError::NotFound),
            StatusCode::CONFLICT => return Err(PveReadError::Conflict),
            status if !status.is_success() => return Err(PveReadError::TransportUnavailable),
            _ => {}
        }
        let body = bounded_visibility_body(response).await?;
        let observed_at = Utc::now();
        let envelope: PveEnvelope<serde_json::Value> = serde_json::from_slice(&body)
            .map_err(|_| PveReadError::InvalidResponse)?;
        Ok((envelope.data, observed_at))
    }).await.unwrap_or(Err(PveReadError::TimedOut))
}
```

Place this private method in ReqwestPveObserver's implementation. Its only call sites supply literal status or network; no public arbitrary-route helper and no new dependency. Each trait implementation calls it then the corresponding from_wire(node.clone(), data, observed_at) parser. The2second bound covers request/body/envelope; bounded synchronous DTO parsing follows. No fan-out occurs.
- [ ] **Step4: Run focused/full workspace local gates and exact-source artifact proof after reviews.** Full PVE+compile-fail, workspace/all-features, Python8, fmt, strict Clippy, cargo-deny with cache/warnings disclosed. Owned local database fixtures allowed only with external test DSN cleared and verified local Docker endpoint/cleanup. Linux artifact uses predecessor verified local image, current tracked manifest/exact effective context, offline locked Cargo and no socket mounts. Execute new nonzero-discovered loopback tests and unchanged selected regression targets; keep authenticated service/PG macOS-only distinction. Preserve actual fake observer and multiworker/adapter proof on final reviewed image; record actualSHA, test counts/repeats, ownership cleanup and hashes.
- [ ] **Step5: Commit source** as `feat(rust): add bounded node and network GET observations`; independent task/final reviews then one coordinated repair wave if needed precede immutable artifact acceptance.

## Self-review and remaining programme

This slice supplies reusable safe-method contracts, not runtime fan-out or installed-release compatibility. Next integration plan must fix explicit validated node selection, request ceilings, independent cadence/cancellation and per-component freshness before service activation. Storage/artifact modeling and any activation decision remain separate. Real write provenance/retry/reservations, media/firmware/TPM/QGA, OSDeploy/CloudOSD/agent integration, shared Python fencing, restore/nonproduction proof and separately approved cutover remain required programme work. Deferred product tracks follow stable controller contracts.
