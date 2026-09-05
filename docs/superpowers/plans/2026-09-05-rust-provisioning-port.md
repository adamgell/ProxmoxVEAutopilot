# Rich native provisioning PVE port implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement and review one task at a time.

**Goal:** Execute the PVE portion of the fixed OSDeploy plan against one owned fake world: rich clone, actual growth, complete PE configuration/start, stop, media handoff and distinct disk start. This phase supplies typed requests, facts and postconditions for subsequent durable store/controller/service integration; it is not a replacement scheduler or the final PoC.

**Architecture:** Add a sibling provisioning-v1 contract within pve-port. Preserve old native-v1 parser, keys and stored proof semantics. osdeploy-adapter converts its accepted immutable plan downward to PVE expectations; no reverse dependency. Reuse the existing fake's one VM namespace, task machinery and sealed capability. No real mutation transport.

**Spec:** approved Rust replacement design, sections 8–10 and 14–18. Main read `.superpowers/sdd/2026-09-05-rust-osdeploy-contract/typed-pve-extension-proposal.md`; binding decisions are `typed-pve-main-rulings.md` and `template-fingerprint-ruling.md` beside it. The full sixteen-stage service-driven OSDeploy proposal remains the continuation target.

**Tech:** Existing Rust 1.92 workspace, serde/serde_json, chrono, UUID, async-trait and event-journal canonical hashing. No new external package/version.

## Global Constraints

- Do not begin source changes until main accepts the frozen OSDeploy-contract Linux gate and supplies the next exact baseline. Production 192.168.2.4 and real PVE remain read-only; these tasks make no live calls. Main fulfilled session MCP/status preflight; no skill.sh or tunnel actions.
- All source work is in the isolated codex-rust-controller-design worktree. No secrets, remote calls, service startup, PostgreSQL/Compose work, source publication, merge, deploy, cache cleanup or Docker in implementation tasks. RustedOutClient and downstream tracks remain deferred.
- One implementer at a time, independent task review after each, final combined integration review and main-owned exact Linux gate after the phase. No implementer/reviewer children; preserve SDD evidence and previous source/artifacts.
- Keep NativeVmPlan, NativeStep, NativeOperationPlan, NativeVmConfig, existing native evidence/ownership and old parser/evaluator public serialization and behavior unchanged. New rich snapshots must never be stripped into old clean-looking snapshots.
- Fake provenance remains FakePve only and is never a real PVE parameter or live ownership proof. Typed values and pure Ready decisions are advice, not grants. Existing locked store dispatch/decision APIs will be integrated later; do not introduce caller-authorized sends or a second authority table.
- One immutable dispatch per operation remains the later store rule. No automatic resend, alternate operation key or start-already-running shortcut. Distinct PE and disk starts keep distinct operation/profile/digest bindings despite the same external form.
- Constructor outputs have private fields, validating admission and getters. Persisted fact/expectation/request/evidence decoding uses private deny_unknown_fields wire types and reconstruction, not unchecked derives on invariant-bearing outputs. All errors/unsupported reasons are fixed and payload-free. Raw rejected strings, unknown config properties and arbitrary maps do not enter sanitized evidence.
- This is an explicit synthetic native-v1 PVE contract. Integral binary K/M/G/T parsing and qmresize/qmstop conventions are fake fixtures, not claims about installed PVE semantics. Real-wire support requires separately verified contracts and authorization.

## Shared interface rulings

New lower-level types belong to pve-port, never import OsDeployStage. `ProvisioningActionV1` is Clone, EnsureCapacity, ConfigurePe, StartPe, EnsureStopped, ConfigureDisk, StartDisk. `ProvisioningBootProfile` is PeMedia or InstalledDisk. The OSDeploy layer owns the sixteen semantic operation keys and full workflow digest.

PVE expectations bind NativeVmPlan, template semantic SHA, template/effective capacities, system/disk serials and both ISO volids. Fixed desired firmware is SeaBIOS, CPU host, balloon0, QGA enabled/typevirtio, scsi0, ide2/ide3 and the two exact boot profiles. Full workflow digest and derived PVE operation-plan digest are separate; future store must recompute their relation from the immutable OSDeploy plan.

The rich snapshot requires explicit core fields, including BIOS/CPU/balloon/agent/boot/template/size. Missing required fields are InvalidResponse, not defaults. Well-formed unsupported settings retain only fixed enum/reason classifications; malformed/duplicate property syntax is InvalidResponse. Serials may be absent before ConfigurePe. Absent media slots are distinct from present empty/unsupported CD-ROM values. A nonempty unsupported set blocks execution and semantic-template admission.

Historical new-clone proof uses the rich snapshot. Reuse CloneRequest and matching FakeCloneProvenance but do not convert new clone evidence into NativeCloneOwnership. Capacity preserves clone-generated intermediate UUID/MAC and primary disk identity; ConfigurePe alone establishes final UUID/MAC/serials. Afterwards resources, capacity, volume and final identity stay invariant. ConfigureDisk only removes both media and changes boot order. No unnecessary common ownership abstraction now.

### Task 1: Rich facts, expectations and semantic template fingerprint

**Files:** Create `crates/pve-port/src/provisioning.rs`, `provisioning/expectations.rs`, `provisioning/config.rs`, `provisioning/serde_wire.rs`, `tests/provisioning_config.rs`, `tests/provisioning_expectations.rs` under rust-controller. Modify only pve-port/src/lib.rs for exports and README boundary notes. Existing crate dependencies suffice.

**Produced interfaces:**

```rust
pub enum ProvisioningActionV1 { Clone, EnsureCapacity, ConfigurePe, StartPe, EnsureStopped, ConfigureDisk, StartDisk }
pub enum ProvisioningBootProfile { PeMedia, InstalledDisk }
pub struct ProvisioningExpectationsInputV1<'a> {
    pub vm: NativeVmPlan,
    pub template_config_sha256: &'a str,
    pub template_capacity_bytes: u64,
    pub effective_capacity_bytes: u64,
    pub system_serial: &'a str, pub disk_serial: &'a str,
    pub deployment_iso_volid: &'a str, pub driver_iso_volid: &'a str,
}
pub struct ProvisioningExpectationsV1 { /* private validated inputs */ }
impl ProvisioningExpectationsV1 {
    pub fn new(input: ProvisioningExpectationsInputV1<'_>) -> Result<Self, InvalidProvisioning>;
    pub fn fingerprint(&self) -> Result<String, InvalidProvisioning>;
    // Exact read-only getters for every input, borrowing owned values.
}
pub struct ProvisioningOperationPlanV1 { /* action and expectations */ }
impl ProvisioningOperationPlanV1 {
    pub fn new(action: ProvisioningActionV1, expected: ProvisioningExpectationsV1) -> Self;
    pub fn action(&self) -> ProvisioningActionV1;
    pub fn expected(&self) -> &ProvisioningExpectationsV1;
    pub fn fingerprint(&self) -> Result<String, InvalidProvisioning>;
}
pub struct ProvisioningVmConfigV1 { /* rich sanitized snapshot */ }
impl ProvisioningVmConfigV1 {
    pub fn from_wire(node: NodeName, vmid: Vmid, source: NativeEvidenceSource,
        data: serde_json::Value, observed_at: chrono::DateTime<chrono::Utc>)
        -> Result<Self, PveReadError>;
    pub fn template_fingerprint(&self) -> Result<String, InvalidProvisioning>;
    // Getters for all retained fields, including unsupported classifications.
}
pub struct ProvisioningMediaInventoryV1 { /* node/storage/list/coverage/time */ }
pub enum ProvisioningCoverageV1 { Complete, Partial }
impl ProvisioningMediaInventoryV1 {
    pub fn new(node: NodeName, storage: StorageName, iso_volids: Vec<String>,
        coverage: ProvisioningCoverageV1, observed_at: DateTime<Utc>) -> Result<Self, PveReadError>;
    pub fn node(&self) -> &NodeName;
    pub fn storage(&self) -> &StorageName;
    pub fn iso_volids(&self) -> &[String];
    pub fn coverage(&self) -> ProvisioningCoverageV1;
    pub fn observed_at(&self) -> DateTime<Utc>;
}
pub struct ProvisioningQgaObservationV1 { /* node/vmid/reachable/time */ }
impl ProvisioningQgaObservationV1 {
    pub fn new(node: NodeName, vmid: Vmid, reachable: bool, observed_at: DateTime<Utc>) -> Self;
    pub fn node(&self) -> &NodeName;
    pub fn vmid(&self) -> Vmid;
    pub fn reachable(&self) -> bool;
    pub fn observed_at(&self) -> DateTime<Utc>;
}
```

- [ ] TDD first: literal valid rich template and destination wire fixtures, separate missing-API failure from behavioral RED. Pin old NativeVmConfig still rejects BIOS/serial/media additions; do not change old expected output to make new tests pass.
- [ ] Expectation admission: template bytes>0, effective>=template, checked hash syntax64ASCIIhex normalized lowercase; both serials and ISO volids obey the already approved OSDeploy input rules (including no consecutive `..` in filenames and distinct media). Expected growth must be expressible as absolute integral GiB when effective>template; a retained larger template may have exact non-GiB bytes. No inference from minimum_storage_bytes. Full canonical expectation/operation serialization includes contractversion1 and every input/action; no OSDeploy metadata or authority flags. Test all fields affect fingerprints and both starts differ.
- [ ] Rich snapshot retains: node/VMID/source, PVE digest, name/cores/memory, primary storage+volume+capacity bytes+optional diskserial, UUID/MAC/bridge/optional systemserial, sanitized firmware/CPU/QGA-channel enums, balloonMiB and QGA-enabled, exact media slot states, boot profile, template/lock, fixed unsupported set, optional fake provenance and observed_at. Use existing validated identifiers and resource bounds; bounded primary volume alphabet is existing safe_atom (1..128 ASCIIalnum/._-, no slash). Do not silently drop size or serial.
- [ ] Parse numeric positive integral bytes or K/M/G/T with checked binary multipliers. Reject zero, overflow, sign, fraction, exponent, whitespace and unsupported suffix. Raw scsi0 must include exactly one size; optional serial is validated. Duplicate comma keys in disks, SMBIOS, NIC, agent, media or boot fail before projection. Disks permit size/serial only; NIC permits virtio/bridge/firewall0; SMBIOS UUID/serial only; agent explicit enabled0/1,typevirtio. Well-formed extra options produce fixed unsupported reasons; do not persist their keys/values.
- [ ] Required raw fields: digest/name/cores/memory/scsi0/smbios1/net0/bios/cpu/balloon/agent/boot/template. Missing core fields or invalid type is InvalidResponse. Missing lock means unlocked; present bounded nonempty lock means locked without retaining its text. Missing ide2/ide3 means absent slots; `none,media=cdrom` is present unsupported, never absent. BIOS other thanseabios, CPUotherthanhost, nonzero balloon, unsupported agentchannel/boot, extraNIC/disk/EFI/TPM/unused/OEM/QEMUargs/unknownfields stay fixed unsupported classifications. Never retain arbitrary raw values. If node/VMID appears in rawdata, require exact requested binding.
- [ ] Wire source PveApi plus any fake marker is rejected; FakePve marker must validate existing FakeCloneProvenance. Snapshot persisted decoding validates the same sanitized invariants, required fields, version, identifiers, serial/media/capacity and unsupported consistency; reject unknown/duplicate JSONfields and forged source/marker combinations. Duplicate top-level raw JSON keys cannot be recovered after conversion to serde_json::Value: do not claim from_wire detects those; its duplicate checks cover comma properties, while persisted Deserialize checks JSON struct fields. No raw wire map is retained. Exact roundtrip/golden JSON tests and mutation of each invariant prevent serde bypasses.
- [ ] Implement semantic template fingerprint exactly as `template-fingerprint-ruling.md`: the entire fixed field set, required supported unlocked template, disk-only boot and absent media. Independent golden digest; each semantic change differs, timestamp/PVE digest changes do not. QGA enabledfalse and missing preconfiguration serials are allowed and explicit. This proves observed config, never blank disk contents or freshness by itself.
- [ ] Media inventory constructor takes node/storage, bounded vector of validated ISO volids, explicit coverage Complete/Partial and observation time. Require every volume's storage matches inventory storage, at most1024 unique entries, rejectduplicates/invalid entries (not silentlyfiltered); no hash/publicationauthority. QGA constructor binds node/VMID/reachable/time, not a bare reusable boolean. Validate sanitized Deserialize and exact shape for both. No GET collector or transport yet.
- [ ] Run focused new tests while iterating; finish complete offline/locked pve-port tests including existing regressions/doctests, fmt and strict workspace Clippy. Commit exact task paths and detailed TDD/source/test report. No artifact build or unrelated suites.

### Task 2: Typed requests, bound evidence and capability-free postconditions

**Files:** Create `provisioning/requests.rs`, `provisioning/evidence.rs`, `provisioning/evaluation.rs`, tests/provisioning_requests.rs and tests/provisioning_evaluation.rs. Modify provisioning.rs exports, lib.rs exports and README only. Do not alter old request/evidence/evaluator APIs.

**Consumed:** Task1 expectations/rich facts plus existing IDs, NativeRead, node/storage/bridge/inventory/power/task facts, NativeDecision, CloneRequest, FakeCloneProvenance and MutationReceipt.

**Produced:** `GrowDiskRequestV1`, `ConfigureProvisioningRequestV1`, `StartProvisioningRequestV1`, `StopProvisioningRequestV1`, closed `ProvisioningMutationRequestV1`; `ProvisioningBindingV1`, `ProvisioningReceiptV1`, `ProvisioningEvidenceInputV1`, private validated `ProvisioningEvidenceV1`, `ProvisioningCloneOwnershipV1`; `ProvisioningEvaluationContextV1`, fixed `ProvisioningReasonV1` and `ProvisioningEvaluationV1 { decision:NativeDecision, reason }`.

```rust
pub fn evaluate_provisioning_preflight(context: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1, as_of: DateTime<Utc>) -> ProvisioningEvaluationV1;
pub fn evaluate_provisioning_outcome(context: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1, as_of: DateTime<Utc>) -> ProvisioningEvaluationV1;
impl ProvisioningCloneOwnershipV1 {
    pub fn from_satisfied_clone(context: &ProvisioningEvaluationContextV1,
        evidence: &ProvisioningEvidenceV1, as_of: DateTime<Utc>) -> Result<Self, InvalidProvisioning>;
}
```

- [ ] Before implementation, pin input struct signatures in the task brief with main: binding holds run/operation/attempt, full workflow SHA, derived operationplanSHA and evidencefence; context holds expected binding, state, cancellation, immutable deadline, pinned freshness1..300, dispatch-recorded/time/may-have-dispatched, optional rich ownership and expected predecessor/profile proof. Evidence holds typed collections/receipts and plan, not caller readiness. This API checkpoint must not invent another authority source; the existing native input shape is a reference, not a mandate to copy obsolete three-step booleans.
- [ ] TDD exact synthetic end-to-end PVE stage facts plus error/ambiguity branches. All new request structs are private validated, Serialize with validating Deserialize, and expose method/path/form/request_digest, operation identity, expectedbefore and plan. Digest includes action/profile, operation and full expected-before context even where external form is identical. No arbitrary URL, argument map or fake marker in external form.
- [ ] Rich Clone reuses existing CloneRequest form/provenance. Grow is PUT target resize with scsi0 and absolute effective G, never +size. PEconfig is PUT target config with PVE digest plus exact cores/memory/hostCPU/balloon0/SeaBIOS/QGA, finalUUID/MAC/serials, original volume/capacity and both media/PEboot. Diskconfig sends digest/deleteide2,ide3/diskboot only. Starts POST status/start with separateprofile/operation but empty externalform. Stop POST status/stop. Preserve before-config identity and require appropriate stopped/running power in evaluation; constructing a request is never a permit.
- [ ] Binding and evidence constructors verify complete operationplan digest and valid fullworkflowSHA; receipt same run/operation/attempt/digests, exact task UPID when present and source/maker rules. Both collection time and snapshot time are checked independently, no future/pre-dispatch/out-of-window facts. Evidence fence anchors first journal append; current CAS/generation checks remain store-owned. Persisted decoding must reconstruct validity, not accept unchecked matchingstrings as external authority.
- [ ] Receipt worker conventions: clone qmclone/sourceVMID; resize qmresize/targetVMID; bothstarts qmstart/targetVMID; stop qmstop/targetVMID; configurations synchronousaccepted only. Central matcher used by evidence and later fake. A task success without exact postconditions is insufficient; another start's task cannot satisfy this start. Check wrongnode/worker/VMID/operation/attempt/hash, taskrunning/failure, receipt loss/substitution and stale/future clocks.
- [ ] Clone preflight requires fresh exact semantictemplateSHA/capacity, supported stopped/unlocked template, node/storagefree/bridge readiness, both media existence, targetabsence jointly frominventory+404 and complete independent identitycoverage. SameVMID anywhere or matching reservedUUID/MAC blocks; partial/failed coverage never proves vacancy. Rich clone success requires matching task and provenance, owned stopped destination, new diskvolume on selectedstorage, carried templatecapacity and supported config. IntermediateUUID/MAC are bound by the rich proof, not assumed final.
- [ ] Capacity only preserves bound volume/intermediateidentity/stopped config; exact effective pre-dispatch permits explicit observed-no-change Satisfied, otherwise exactly pinned templatebytes→effective growth. Unexpected larger/different bytes conflict rather than replan. PEconfigure permits the single intermediate→final identity transition and exact desired resources/media; later diskconfigure/start/stop require finalidentity/capacity/resources invariance. Check unrelated change in every fieldcategory.
- [ ] Stop preflight returns advice only for fresh running owned PEconfig, or observed-no-change for exact stopped before dispatch. Pure port cannot grant the grace-timeout exception; later store must prove PEcompletion/exacttimeoutrevision/deadline/forcepolicy/currentfences. Never satisfy gracewait here. Starts alreadyrunning without their own dispatched operation remain unresolved, not automatically satisfied.
- [ ] After a task-bearing dispatch, missing receipt remains Unknown even if desired state exists, unless an independently bound receipt arrives; no guessed task or resend. Receiptless synchronous configuration may reconcile exact desired state with original dispatch/time/ownership, preserving old conservative rule. No-change branches only apply before dispatch, never erase an ambiguous send. Deadline expiry produces Unknown, not success. Cancel/unauthorized/stale state must not produce Ready.
- [ ] Rich ownership constructor runs complete clone outcome validation and retains its original request/binding/receipt/intermediatesnapshot/historicalevaluationtime. No public unchecked owner constructor, Deserialize or cross-run conversion. Existing NativeCloneOwnership stays unchanged. Tests prove wrongmarker/request/fullworkflow/disk/run cannot reuse ownership; a caller-shaped evidence value still has no dispatch capability.
- [ ] Run focused tests, all existing pve-port tests and doctests, fmt/strict Clippy at completion; record full proof matrix and exact commit. Do not claim store fencing or actual service orchestration from pure evaluator tests.

### Task 3: One fake world, real synthetic state changes and downward OSDeploy conversion

**Files:** Modify pve-port/src/native_fake.rs with a focused new native_fake/provisioning.rs helper; provisioning.rs and lib.rs exports. Create tests/provisioning_fake.rs. Add osdeploy-adapter/src/provisioning.rs and tests/provisioning.rs; modify its lib.rs and README. No external dependency, service/store change or Dockerfile filter change is needed (existing pve-port and osdeploy-adapter commands already cover the phase).

**Interfaces:** A sealed `ProvisioningFakePort: PvePreflightReadPort + native::sealed::FakeMutationCapability` provides async provisioning_vm_config(node,vmid), provisioning_media(node,storage), and submit_provisioning(&ProvisioningMutationRequestV1) -> Result<MutationReceipt,PveWriteError>. Existing task/power/node/inventory/QGA interfaces remain. `osdeploy_adapter::pve_expectations(&OsDeployPlanV1) -> Result<ProvisioningExpectationsV1,ContractError>` derives, never independently overrides, every PVE subset input.

- [ ] TDD first for 80→120GiB actual fake growth, 160→120retention, full clone→capacity→PEconfig→PEstart→stop→diskconfig→diskstart with exact receivedforms and immutable operation/profile identities. QGA stays false unless independent world state sets it; configured channel or runningpower does not set it.
- [ ] Reuse one locked State and VMID namespace. Internal FakeConfig enum may hold oldNativeVmConfig or newProvisioningVmConfig; no separate invisible VM map. Neutral identity inventory sees both families, with honest incompletecoverage when identity cannot be parsed. Old reads of incompatible richconfig fail closed, never drop newfields. Preserve existing legacy fake API and fault behavior/expected tests.
- [ ] Provisioning wire/apply preserves capacity/volume/serials/firmware/media and all unrelated supported fields. Validate expected-before digest/identity/power/marker atomically at submit; delayed accepted tasks preserve and revalidate target/disk binding before applying. Replacement/tamper cannot redirect accepted work. No world reset from expectedcontrollerplan.
- [ ] Add closed per-action/profile/optional-operation fault selection, counting attempted sends separately from accepted outcomes. PE and disk start faults must be independent. Support rejection, acceptedtaskdelayed/failure, appliedresponse-lost and explicitrelease using existing fake machinery. Synchronous configuration rejects task-only fault modes rather than inventing tasks. Media ISO catalog and QGA state are independent controls, not disk-derived inventory or automatic effects.
- [ ] Both starts and stop change only power; capacity changes only allowed size; ConfigurePe exactly establishes target configuration; ConfigureDisk deletes bothslots and switchesboot only. Test stale beforestate, wrongidentity, volume replacement, missingmedia, insufficientstorage, stopped/running mismatches, duplicate send rejection/recording and tasklatecompletion. Directfakecalls test transport behavior, not durable dispatch authorization.
- [ ] Implement downward conversion and test every PVE input matches accepted plan, fullworkflow digest remains separate and changes on non-PVE inputs, and no reverse pve-port→osdeploy-adapter dependency. Reconstruct expectation via validated new, no uncheckedfield access. Old three-step controller tests remain unchanged and passing.
- [ ] Add compile-fail checks that ReqwestPveObserver and a downstream wrapper cannot implement/call the new sealed mutation capability. Keep synthetic transport literal and no arbitrary endpoint. Future independent fake host can implement the same sealed seam inside pve-port; do not build that HTTP host or service now.
- [ ] Final phase checks: complete pve-port and osdeploy-adapter suites; existing operation-controller pure decision suite; existing native PostgreSQL suite only its pure support filter if unchanged (no Docker). Workspace fmt, strict Clippy, cached offline dependency policy. Main owns full integration review and proportional exact-source Linux proof, then next additive store/scheduler phase. Report actual counts, no unrun/service/production claims.

## Main self-review and staged API gate

The phase spans the whole PVE portion rather than another metadata-only completion criterion. Task1 provides lossless facts and expectations; Task2 binds requests/evidence/postconditions; Task3 changes the actual shared fake world and converts accepted OSDeploy input. Old public contracts remain isolated. Shared store locks, authority, parked waits, callbacks and actual service lifecycle deliberately remain with the next integration phase, where the full sixteen-stage target will be exercised.

Task2's context/evidence/request constructor shapes need main's explicit interface checkpoint after Task1 establishes concrete fact types; Task2 must not start from this high-level API list alone. This staged dependency gate is not approval to infer signatures or silently broaden scope. Task1 interfaces and behavioral rules are implementation-ready; no task may start during the current frozen Linux proof. No goal completion or production readiness follows from this phase alone.
