# Rust OSDeploy fixed-stage and immutable-input contract

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Freeze the executable input and stage contract for the complete service-driven local OSDeploy slice. This is its first implementation phase, not an alternative PoC completion criterion.

**Architecture:** A pure `osdeploy-adapter` crate composes existing validated PVE identities and artifact declarations. It defines a closed sixteen-stage manifest, grow-only disk policy, deterministic names, phase budgets and immutable deployment inputs. Later store/scheduler/controller work consumes this contract; no parallel orchestrator is introduced.

**Spec:** `docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md`. Research: `.superpowers/sdd/2026-09-05-rust-artifact-contract/osdeploy-vertical-proposal.md`, `osdeploy-input-inventory.md`, `native-extension-seams.md` and `next-workflow-binding-notes.md`.

**Tech:** Rust 1.92; existing locked serde, serde_json, uuid, thiserror, event-journal, pve-port and artifact-index. No new external package/version.

## Global Constraints

- Local isolated worktree only. No live calls, secrets, production writes, schema operations, service startup, Docker or build cleanup in implementation tasks. Main already fulfilled repository session status/MCP docs preflight; do not rerun `skill.sh`.
- Production `192.168.2.4` and real PVE remain read-only. RustedOutClient and downstream tracks remain deferred. No deployment/push/merge.
- One implementer at a time, independent task review after each task; main owns final integration review and later Linux gate. No implementer children. Preserve SDD evidence and predecessor artifacts.
- This phase does not alter existing Python wire schemas, `NativeVmPlan`, existing three-stage native operation keys, scheduler, store, service execution selection, or PVE transport.
- Constructor-validated outputs have private fields and Serialize, not Deserialize. Public input structs are not durable or wire DTOs. Later storage reload must reconstruct through these constructors and verify the complete canonical digest; it must not introduce unchecked deserialization. This phase alone is not a restart proof.
- All errors are fixed variants/messages with no rejected payloads. No arbitrary metadata maps, secret values, URLs, filesystem paths, commands, native grants or readiness booleans in the contract. Opaque IDs below are references, not proof the referenced object exists.
- Keep claim levels explicit: declared artifact hashes and pinned template fingerprints are inputs, never fresh observation, publication, applied-content, ownership or execution authority.

## Main rulings and full-slice continuation

Adopt the proposal's fixed stages, distinct PE/disk start operations, one immutable dispatch per mutation and no automatic resend. Grace expiry remains Unknown. Only the specifically guarded ensure-stopped edge may follow that reason, preserving degraded history; this phase describes that edge but cannot authorize it. Store evaluation must later verify the exact decision revision/deadline, PE completion, fresh matching running state, policy and current fences under locks.

Disk capacity uses checked binary GiB for the native contract. Legacy source requests literal `G` and compares integer template sizes; this local unit choice is explicit, not yet verified real-PVE size parsing. Effective size is max(template, requested); free-storage minimum is separate and caller-pinned. No inferred overhead or storage reservation guarantee. Changed source identity/capacity snapshot blocks later preflight instead of silently changing the plan.

Native v1 is amd64/base, SeaBIOS, Secure Boot false, CPU host, balloon disabled, QGA VirtIO channel, one scsi0 disk and fixed ide2/ide3 media. Reject inherited EFI/TPM/unrecognized devices in later observed preflight; do not silently remove them. No arbitrary OEM fields in v1: explicitly support system serial and disk serial only. Additional OEM fields require a versioned typed extension; absence is not evidence that a template is clean.

Preserve exact legacy name normalization in a pure helper. Native admission rejects empty/trailing-hyphen/all-numeric resulting Windows names; this is an explicit narrower native policy, not a Python API change. Preserve requested name, independent PVE name and derived Windows/agent names separately. Do not regenerate serials or VM identities on replay.

Use production-policy defaults registration 2400 s, PE 7200 s, shutdown grace 300 s, full OS 7200 s, mutation 300 s, evidence freshness 30 s. All phase values are pinned inputs with positive bounded validation. Full-OS actions share one absolute deadline; lease renewal cannot reset it. Tests may construct an explicitly synthetic short policy. Storing wall-clock anchors and evaluating elapsed time belongs to later DB work.

Optional driver/client/agent payload hashes remain explicitly `declared` or `unverified` metadata for legacy compatibility. Missing hashes cannot produce a content-integrity claim. Existing run HMAC and opaque agent token families remain distinct. `/next` response loss cannot silently cause executable redelivery: current client has no durable delivery ledger, so preserve Unknown and report recovery limitations unless a separately versioned client contract is implemented.

Continuation after this phase, in dependency order: typed PVE resize/config/media/stop and independent starts; additive immutable registration/reload; parked waits and phase binding in existing scheduler; compatible authenticated callback/guest-action transaction boundaries; existing NativeController orchestration; actual service plus independent fake world; full success/fault/restart proof and exact Linux candidate. Then remaining replacement-design gates in `poc-readiness-tracker.md`. None of these are marked complete by this contract.

### Task 1: Closed stages, grow-only capacity, names and pinned phase policy

**Files:** Create `rust-controller/crates/osdeploy-adapter/Cargo.toml`, `src/lib.rs`, `src/stages.rs`, `src/input_values.rs`, `tests/stages.rs`, `tests/input_values.rs`. Modify only the local member entry in `rust-controller/Cargo.lock` and boundary notes in `rust-controller/README.md`.

**Interfaces:**

```rust
pub enum OsDeployStage {
    Clone, DiskCapacity, ConfigurePe, StartPe, PeRegister, PeComplete,
    PeShutdownGrace, PeEnsureStopped, ConfigureDisk, StartDisk,
    InstallQga, VerifyQga, InstallQgaWatchdog, InstallAgent,
    AgentHeartbeat, VerifyOperational,
}
pub enum StageKind { PveMutation, CallbackWait, ObservationWait, GuestAction }
pub enum StageDependency {
    Intake,
    Satisfied(OsDeployStage),
    ShutdownGraceOrGuardedEscalation,
}
impl OsDeployStage {
    pub const ALL: [Self; 16];
    pub const fn operation_key(self) -> &'static str;
    pub const fn kind(self) -> StageKind;
    pub const fn dependency(self) -> StageDependency;
}
pub struct DiskCapacity { /* private */ }
impl DiskCapacity {
    pub fn new(requested_gib: u64, template_bytes: u64) -> Result<Self, ContractError>;
    pub fn requested_gib(&self) -> u64;
    pub fn template_bytes(&self) -> u64;
    pub fn effective_bytes(&self) -> u64;
    pub fn growth_required(&self) -> bool;
}
pub fn normalize_legacy_windows_name(input: &str) -> String;
pub struct DeploymentNames { /* private */ }
impl DeploymentNames {
    pub fn new(requested_name: &str, pve_name: pve_port::NativeVmName) -> Result<Self, ContractError>;
    pub fn requested_name(&self) -> &str;
    pub fn pve_name(&self) -> &pve_port::NativeVmName;
    pub fn windows_name(&self) -> &str;
    pub fn expected_agent_id(&self) -> &str;
}
pub struct PhasePolicyInput {
    pub registration_seconds: u32, pub pe_seconds: u32,
    pub shutdown_grace_seconds: u32, pub full_os_seconds: u32,
    pub mutation_seconds: u32, pub evidence_freshness_seconds: u32,
    pub allow_force_stop: bool,
}
pub struct PhasePolicy { /* private, same field getters */ }
impl PhasePolicy {
    pub fn new(input: PhasePolicyInput) -> Result<Self, ContractError>;
    pub fn production_defaults(allow_force_stop: bool) -> Self;
}
```

- [ ] Add crate metadata `publish=false`, workspace metadata; dependencies initially pve-port, serde and thiserror. Add a fixed `ContractError` enum as needed, with payload-free messages.
- [ ] Write tests first and record behavioral RED independently of missing-API compile failure. Follow the existing crate styles, focused tests while iterating.
- [ ] Pin exact stage keys in order: `osdeploy.clone.v1`, `osdeploy.disk.capacity.v1`, `osdeploy.configure.pe.v1`, `osdeploy.start.pe.v1`, `osdeploy.pe.register.v1`, `osdeploy.pe.complete.v1`, `osdeploy.pe.shutdown.grace.v1`, `osdeploy.pe.ensure-stopped.v1`, `osdeploy.configure.disk.v1`, `osdeploy.start.disk.v1`, `osdeploy.fullos.install-qga.v1`, `osdeploy.fullos.verify-qga.v1`, `osdeploy.fullos.install-qga-watchdog.v1`, `osdeploy.fullos.install-agent.v1`, `osdeploy.fullos.agent-heartbeat.v1`, `osdeploy.verify-operational.v1`. Test sixteen unique keys, exact ordering and every dependency, with the special edge only on PeEnsureStopped. Ordinary predecessors are the previous array element. Do not expose user-defined keys/predecessors.
- [ ] Kinds: first four stages PveMutation; PeRegister and PeComplete CallbackWait; PeShutdownGrace ObservationWait; PeEnsureStopped, ConfigureDisk and StartDisk PveMutation; InstallQga through AgentHeartbeat GuestAction; VerifyOperational ObservationWait. PveMutation means potentially mutating, not mandatory dispatch: capacity and ensure-stopped may satisfy from verified no-change facts. AgentHeartbeat includes guest action plus independent heartbeat in later evaluation. The enum alone is not an authorization method.
- [ ] Stages/kinds/dependencies derive Copy/Clone/Eq/Debug/Serialize with snake_case variant encoding. No Deserialize in this phase. Test complete serialization so future storage has a concrete contract.
- [ ] Disk tests: requested 80 GiB minimum, reject 79/0, positive template bytes, checked u64 multiplication by 1,073,741,824; values around overflow boundary. 80GiB template/120 request grows; 160GiB template/120 request keeps160; equal no growth; non-whole-GiB larger template retained exactly. Serialization exactly requested_gib/template_bytes/effective_bytes/growth_required. No minimum_storage_bytes argument or inferred storage sufficiency.
- [ ] Name helper exactly filters ASCII A-Z/a-z/0-9/hyphen after trimming, strips edge hyphens before truncating to15. Unicode is removed, not transliterated; case retained. No Python module import (its imports may initialize state). Test literal golden examples including `  --Lab VM_01--  ` -> `LabVM01`, Unicode removal, empty, all punctuation, 16+ characters and truncation leaving a hyphen. DeploymentNames retains original requested string (1..256 UTF-8 bytes, no control characters; spaces allowed), validated independent PVE name, normalized Windows name and `agent-` + ASCII lowercase. Reject unsupported native outputs noted above. Case-insensitive collisions must yield same agent ID; this does not enforce uniqueness without later DB reservation.
- [ ] Policy new admits each duration in 1..=86400; freshness 1..=300 and no larger than mutation budget. Do not require freshness <= phase budgets, so short synthetic waits can coexist with evidence freshness. Test each zero/upper bound independently, exact defaults, force-stop true/false preserved. Serialize exact seven input keys, no clock/deadline/readiness fields. Defaults invoke the same validated constructor without runtime secret/config I/O.
- [ ] Derive Serialize/Clone/Debug/Eq for private validated value types, implement read-only getters, no public mutation. Add compile-fail proof that DiskCapacity and PhasePolicy cannot be deserialized. Constructors, not downstream caller booleans, set derived output fields.
- [ ] Run `cargo test --offline --manifest-path rust-controller/Cargo.toml -p osdeploy-adapter`, then locked focused tests after local lock update. Run fmt check and strict workspace all-target/all-feature Clippy once at completion. No unrelated PG/Compose suites. Self-review, commit exact paths and report RED/GREEN, exact SHA, counts and remaining boundaries to assigned task report.

### Task 2: Complete immutable base deployment input snapshot

**Files:** Create `rust-controller/crates/osdeploy-adapter/src/plan.rs`, `tests/plan.rs`; modify crate Cargo.toml and lib.rs, local Cargo.lock entry, README and add one pure `osdeploy-adapter` test RUN in Dockerfile.test without changing existing filters.

**Consumed:** Task1 value types, `pve_port::NativeVmPlan`, `artifact_index::DeclaredArtifact`, `event_journal::payload_digest`.

**Interfaces:**

```rust
pub struct PayloadDeclarationInput<'a> {
    pub reference_id: uuid::Uuid, pub sha256: Option<&'a str>,
}
pub struct PayloadDeclaration { /* private reference, normalized hash, claim */ }
impl PayloadDeclaration {
    pub fn new(input: PayloadDeclarationInput<'_>) -> Result<Self, ContractError>;
    pub fn reference_id(&self) -> uuid::Uuid;
    pub fn sha256(&self) -> Option<&str>;
}
pub struct OsDeployPlanInput<'a> {
    pub vm: pve_port::NativeVmPlan,
    pub names: DeploymentNames,
    pub artifact: artifact_index::DeclaredArtifact,
    pub disk: DiskCapacity,
    pub policy: PhasePolicy,
    pub template_config_sha256: &'a str,
    pub deployment_iso_volid: &'a str,
    pub driver_iso_volid: &'a str,
    pub driver_payload: PayloadDeclaration,
    pub osd_client_payload: PayloadDeclaration,
    pub agent_payload: PayloadDeclaration,
    pub system_serial: &'a str,
    pub disk_serial: &'a str,
    pub os_version: &'a str, pub os_edition: &'a str,
    pub os_language: &'a str, pub image_name: &'a str,
    pub secret_profile_id: uuid::Uuid,
    pub callback_profile_id: uuid::Uuid,
}
pub struct OsDeployPlanV1 { /* private validated snapshot */ }
impl OsDeployPlanV1 {
    pub fn new(input: OsDeployPlanInput<'_>) -> Result<Self, ContractError>;
    pub fn fingerprint(&self) -> Result<String, ContractError>;
    // Read-only getters named for every input field, returning &T / &str
    // for owned values, UUID for profile IDs. No mutable getters.
}
```

- [ ] Add only local artifact-index/event-journal paths and existing serde_json/uuid dependencies. No lower-level crate gains a dependency on osdeploy-adapter. Do not modify artifact-index to make declared metadata unchecked-deserializable.
- [ ] Write behavioral RED for a complete valid synthetic fixture. Assert VM memory at least4096MiB, vm.name equals names.pve_name, artifact apply index <=i32::MAX (positive already enforced), required nonnil profile and payload UUIDs. NativeVmPlan already supplies bounded cores/memory, distinctVMID and positive storage-free minimum. Persist its free minimum independently from DiskCapacity. No run/operation IDs in pure plan: store later binds plan digest to existing run/command IDs and reservations atomically.
- [ ] Validate template SHA exactly64ASCIIhex and lowercase. Payload hash absent serializes claim `unverified`; present valid normalized64hex serializes claim `declared`. Neither becomes verified. Fixed serialization for declaration exactly reference_id/sha256/evidence_level. Test malformed hashes and nonnil opaque UUIDs, including valid UUIDv4.
- [ ] Media volids narrowly accept `<storage>:iso/<filename>.iso`, where storage passes existing StorageName validation, filename stem1..128 ASCII alnum/underscore/hyphen/dot, at least one alnum, no `..` segment anywhere, extension literal `.iso`. No URL, percent encoding, backslash, controls, whitespace, additional slash or colon. Different storage from disk storage is permitted and must be observed later. Require two different volids. These are native-v1 references, not an API-wide PVE parser or media existence proof.
- [ ] System and disk serials each1..64ASCIIalnum/underscore/hyphen, first/last alnum. No random suffix generation, SMBIOS base64 or wire encoding yet. Do not accept a generic OEM map. OS version/edition/image fields1..128ASCIIletters/digits/space/dot/underscore/hyphen with alnum endpoints; language2..16ASCIIletters/hyphen with letter endpoints. These are bounded declarations, not an installed-image capability guarantee. No commands, paths or arbitrary secrets accepted as fields.
- [ ] Serialize the entire snapshot with fixed contract_version1/workflow_kind`os_deploy`/architecture`amd64`/server_role`base`/firmware`seabios`/secure_bootfalse/cpu`host`/balloonfalse/qga_channel`virtio`/primary_disk`scsi0`/pe_iso_slot`ide2`/driver_iso_slot`ide3`/pe_boot_order`ide2;scsi0`/disk_boot_order`scsi0`/template_device_policy`reject_extra_devices`/evidence_level`declared_only`, plus every validated input. Fixed contract fields are not caller configurable and confer no authority. Snapshot exact top-level key set, nested artifact provenance and payload claim levels.
- [ ] Canonical complete serialization feeds event-journal payload_digest. No omitted policy/reference, derived name or artifact provenance. Test mutation of each input field/category changes digest, normalized equivalent hashes do not, absent vs present optional hash does, source-index provenance participates. Golden same-fixture digest is stable across repeated construction. Include compile-fail private construction/deserialization proof for plan and payload declaration.
- [ ] Run focused tests, strict workspace Clippy, fmt and cached offline dependency policy; no dependency/advisory refresh. Add Dockerfile pure test command, preserving old service filters. Commit exact task paths and report behavioral RED/GREEN and exact input/claim coverage. Main independently reviews task+integration then executes focused exact-source Linux crate tests, without relabeling inherited controller-service as a newly rebuilt candidate.

## Main self-review

Coverage: Task1 makes all sixteen stage identities and dependency semantics concrete; capacity, names and policy feed Task2. Task2 binds every specified artifact/resource/identity/media/payload/profile declaration into one digest and depends only downward on existing domain/PVE/artifact libraries. Actual run identity, reservation, fresh template/media checks, durable reload, deadlines, authenticated callback binding and dispatch decisions deliberately remain with the existing later store/controller boundaries. The input snapshot has no mutation capability or readiness conversion.

The complete vertical proposal remains the continuation contract, not a substituted DTO-only finish. The native-v1 restrictions, missing payload-hash claim and legacy /next response-loss limitation are explicit. No real mutation or production-ready claim follows from this phase. Stage serialization and plan output are stable contracts for later revalidation; absence of Deserialize prevents accidental direct reload without admission.
