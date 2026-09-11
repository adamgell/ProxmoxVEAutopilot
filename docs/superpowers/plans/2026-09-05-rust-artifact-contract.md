# Rust artifact identity and byte-match contract implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the native OSDeploy workflow an immutable artifact identity that separates declared metadata, selected image index, and supplied-byte hash agreement from publication and boot readiness.

**Architecture:** Add a pure `artifact-index` crate. A validated native-v1 descriptor admits explicit sanitized metadata, fingerprints the entire descriptor, and optionally compares caller-supplied ISO/WIM byte slices against declared SHA-256 values. Neither successful construction nor byte matching grants PVE, filesystem, boot, or workflow authority. Existing Python endpoints and manifests are unchanged.

**Tech Stack:** Rust 1.92, existing workspace serde/serde_json/sha2/hex/thiserror/uuid, event-journal canonical hashing. No new external package/version and no I/O dependency.

**Spec:** `docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md`, sections 5–8, 13–18 and 21.

## Global Constraints

- All work is local to the isolated worktree. Production `192.168.2.4` and real Proxmox remain read-only; this task makes no live calls at all.
- No secrets, paths, URLs, raw package payloads, host identities, commands, arbitrary metadata maps, publication, database writes, process execution, service activation, or new environment settings.
- RustedOutClient and downstream product tracks remain excluded.
- Rust enums and private fields enforce the contract. Output descriptors and byte-match reports derive Serialize, not Deserialize; no unchecked constructor, public fields, or conversion to native mutation/evidence types.
- `build_sha` in the existing builder is a timestamp label, not a source or content digest. Declared hashes, locally supplied-byte hash matches, published location, and boot/readiness proofs are separate claims.
- This is an explicitly limited native-v1 library, not a replacement parser for existing API rows or manifests. Existing wire compatibility is not tightened or changed.
- Preserve accepted predecessor artifact/evidence files and existing source edits. No cleanup, merge, push, deploy, or production write. Do not delete SDD evidence.
- One implementer, then independent task/final review. No children from implementer. No parallel compilation with the selected-node Linux artifact gate.

## Verified legacy inputs and exclusions

Main inspected `tools/osdeploy-build/build-osdeploy.ps1:764`: manifest v1 records ISO/WIM declared hashes, `image_index` and `output_image_index`. `autopilot-proxmox/web/osdeploy_endpoints.py:851` prefers positive manifest output index, otherwise the row index. `autopilot-proxmox/scripts/osdeploy_remote_build.py:192` trusts provided hashes rather than independently rehashing in all cases. `osdeploy_pg.py:358` persists metadata. These are source-contract facts, not proof of current live artifacts.

For this library, source index must be positive and a provided output index must be positive. Missing output index explicitly selects source index; invalid provided output index is rejected, never silently treated as absent. A later legacy adapter must report malformed-manifest fallback independently; this library does not read a manifest or claim byte-for-byte compatibility with Python's coercions.

Published location is deliberately not represented until selected node/storage/media binding is implemented. File existence, transfer, live storage GET side effects, signed boot media, WIM contents and firmware are outside this slice. Do not synthesize a `ready` boolean from these metadata facts.

### Task 1: Immutable declared descriptor and supplied-byte comparison

**Files:**
- Create `rust-controller/crates/artifact-index/Cargo.toml`.
- Create `rust-controller/crates/artifact-index/src/lib.rs`, `descriptor.rs`, `bytes.rs`.
- Create `rust-controller/crates/artifact-index/tests/descriptor.rs`, `bytes.rs`.
- Modify `rust-controller/Cargo.lock` only for the new local workspace member.
- Modify `rust-controller/README.md` to explain the claim boundaries and test command.
- Modify `rust-controller/Dockerfile.test` to execute `cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p artifact-index` alongside existing pure crate tests; preserve all existing filters.

**Interfaces:**

```rust
// Input only: not Serialize/Deserialize. Artifact UUID may be any non-nil UUID,
// matching existing opaque artifact IDs; it is not an operation UUIDv7.
pub struct DeclaredArtifactInput<'a> {
    pub artifact_id: uuid::Uuid,
    pub architecture: &'a str,
    pub build_label: &'a str,
    pub iso_sha256: &'a str,
    pub wim_sha256: &'a str,
    pub source_image_index: u32,
    pub output_image_index: Option<u32>,
}
pub enum ImageIndexSource { OutputManifest, SourceFallback }
pub struct DeclaredArtifact { /* private validated fields */ }
impl DeclaredArtifact {
    pub fn new(input: DeclaredArtifactInput<'_>) -> Result<Self, ArtifactError>;
    pub fn artifact_id(&self) -> uuid::Uuid;
    pub fn architecture(&self) -> &str;
    pub fn build_label(&self) -> &str;
    pub fn iso_sha256(&self) -> &str;
    pub fn wim_sha256(&self) -> &str;
    pub fn source_image_index(&self) -> u32;
    pub fn output_image_index(&self) -> Option<u32>;
    pub fn apply_image_index(&self) -> u32;
    pub fn image_index_source(&self) -> ImageIndexSource;
    pub fn fingerprint(&self) -> Result<String, ArtifactError>;
}
pub struct SuppliedByteMatch { /* private fields; not authority */ }
pub fn compare_supplied_bytes(
    artifact: &DeclaredArtifact, iso: &[u8], wim: &[u8],
) -> Result<SuppliedByteMatch, ArtifactError>;
impl SuppliedByteMatch {
    pub fn descriptor_fingerprint(&self) -> &str;
    pub fn iso_size_bytes(&self) -> u64;
    pub fn wim_size_bytes(&self) -> u64;
}
```

- [ ] Create the crate manifest with `publish = false`, workspace package metadata, existing workspace dependencies serde, serde_json, sha2, hex, thiserror, uuid and path `event-journal`. The workspace `crates/*` glob already discovers it. Do not add pve-port or runtime dependencies.
- [ ] Add behavior tests before implementation. Use input fixture UUID `11111111-1111-4111-8111-111111111111`, architecture `amd64`, label `20260905090000`, ISO digest `a` repeated 64, WIM digest `b` repeated 64, source index 4, output index Some(1). Representative assertions:

```rust
let a = DeclaredArtifact::new(fixture()).unwrap();
assert_eq!(a.apply_image_index(), 1);
assert_eq!(a.image_index_source(), ImageIndexSource::OutputManifest);
let mut input = fixture();
input.output_image_index = None;
let fallback = DeclaredArtifact::new(input).unwrap();
assert_eq!(fallback.apply_image_index(), 4);
assert_eq!(fallback.image_index_source(), ImageIndexSource::SourceFallback);
assert_ne!(a.fingerprint().unwrap(), fallback.fingerprint().unwrap());
```

- [ ] Pin the complete validation matrix: nil UUID rejected; only literal `amd64` admitted; build label exactly 14 ASCII decimal digits (the current timestamp-label native-v1 shape, not calendar or Git validation); both hashes exactly 64 ASCII hexadecimal characters normalized lowercase; zero source or present zero output rejected; positive u32 bounds accepted without narrowing. Test empty, whitespace, Unicode, control, overlong, URI/path-shaped label and malformed/nonhex hashes. Fixed `ArtifactError` variants/messages never retain or echo rejected input.
- [ ] Run `cargo test --offline --manifest-path rust-controller/Cargo.toml -p artifact-index`. First missing API compile failure is scaffolding evidence, not behavioral RED. Add minimal rejecting implementations, then show valid-fixture tests fail on behavior before implementing admission.
- [ ] Implement private validated fields with Clone/Debug/Eq/PartialEq/Serialize and read-only getters. Serialize enum snake_case. Descriptor serialized keys are exactly: `contract_version` (1), `artifact_id`, `architecture`, `build_label`, `iso_sha256`, `wim_sha256`, `source_image_index`, `output_image_index`, `apply_image_index`, `image_index_source`, `evidence_level` (`declared_only`). `evidence_level` and contract version are fixed private constants in serialization, never caller inputs. Avoid storing redundant derived index state when it can be computed once for serialization and getters.
- [ ] Fingerprint using `event_journal::payload_digest` over the complete serialized descriptor, including contract version and evidence level. Map serialization/canonical failures into a fixed error without raw input. Test identical normalized input yields identical digest; changing artifact UUID, build label, either declared hash, source index, output presence/value changes digest; uppercase/lowercase hashes yield identical digest; Some(source) versus None has different provenance and digest despite equal apply index. Explicitly snapshot the exact key set and constant values.
- [ ] Add supplied-byte tests using small nonempty synthetic byte strings and expected SHA-256 computed in the test. Empty ISO or WIM fails; either mismatch and swapped distinct ISO/WIM fail. A successful match returns fingerprint and exact lengths only, plus serialized `evidence_level: supplied_bytes_matched` and `publication_verified: false`. No raw bytes or redundant declared hashes are copied into the report. It proves only agreement of the supplied slices, not their origin, file stability, PVE publication, WIM validity or bootability. There is no `ready` field or native evidence conversion. Representative behavior:

```rust
let report = compare_supplied_bytes(&artifact_for(b"iso", b"wim"), b"iso", b"wim").unwrap();
assert_eq!(report.iso_size_bytes(), 3);
assert_eq!(report.wim_size_bytes(), 3);
assert!(compare_supplied_bytes(&artifact_for(b"iso", b"wim"), b"bad", b"wim").is_err());
assert_eq!(serde_json::to_value(&report).unwrap()["publication_verified"], false);
```

- [ ] Implement slice hashing directly, without copying input bytes or reading files. Capture no paths, URI, filesystem, clock or caller truth flags. Report serialization has exactly `descriptor_fingerprint`, `iso_size_bytes`, `wim_size_bytes`, `evidence_level`, `publication_verified`. Fixed mismatch/empty errors do not include bytes or expected/actual hashes. A match for another descriptor cannot be reused silently: changing any descriptor field changes the bound fingerprint even with the same content hashes.
- [ ] Add compile-fail doctests showing `DeclaredArtifact` and `SuppliedByteMatch` cannot be deserialized and a report cannot be constructed with public fields. These tests pin construction ownership, not malicious in-process code security. No changes to native execution or configuration are allowed.
- [ ] Run focused tests and a mutation check removing the ISO comparison: its mismatch test must fail, then restore and rerun. Also pin the WIM comparison independently with its mismatch test. Run fmt check, strict workspace all-target/all-feature Clippy and offline dependency policy using existing cached policy. Do not refetch advisories or dependencies. Report compile/run tests separately; do not rerun unrelated PostgreSQL/Compose suites for this pure task.
- [ ] Add the Dockerfile pure test command and README boundary notes, regenerate local workspace lockfile offline, rerun `cargo test --offline --locked --manifest-path rust-controller/Cargo.toml -p artifact-index` including doctests. Commit only task paths. Report exact commit, RED/GREEN commands/counts, mutation restoration, source scope and any unmet gates in the assigned SDD report. Main owns independent review and the later exact-source Linux gate.

## Main self-review

This slice implements artifact identity/index/hash distinctions in design sections 6, 8 and 15. It does not claim the full OSDeploy plan, callback, publication or production gates. Those remain explicit in the PoC tracker. No existing API policy changes. Interfaces are self-contained in one independently reviewable task; descriptor validation feeds fingerprint and byte-match report, and both tests share only local fixture helpers. File ownership is disjoint from selected-node code; implementation must nevertheless wait for its artifact snapshot/gate to finish. Task input strings cannot bypass private output construction. Derived index provenance participates in the digest. Bounded metadata and fixed errors avoid carrying arbitrary rejected payloads into evidence.
