# Fixture capability and typed Clone patch plan

Status: implementation plan; no controller IPC capability is implemented by this document.

## Inspected constraints

`pve-port/src/provisioning.rs` already defines `ProvisioningFakePort` with the
crate-private `FakeMutationCapability` supertrait. Preserve that seal. The
production observer must never implement it. The current concrete dependencies
are `OsDeployController.fake`, its constructor, `osdeploy/collect.rs`,
`osdeploy/send.rs`, and `OsDeployDispatchPermit::submit_fake_once`. The controller
also calls the inherent `NativeFakePve::controller_checkpoint` after dispatch
commit, so replacing just the field or submit signature is insufficient.

The daemon and ledger currently exist only under `pve-port/tests/support`.
Their client `Effect` request accepts raw before/after state; their reply has no
receipt. `VmState` contains only disk bytes and a PE-configured flag. A wrapper
around this protocol cannot implement truthful provisioning observations.

Two additional bounds need deliberate changes with boundary tests: daemon
lifetime currently rejects values above ten seconds, while the controller call
budget is twenty-four seconds; request framing currently accepts at most 1024
bytes, which must be measured against a serialized typed provisioning request
before choosing a bounded protocol size. Neither limit should be silently
removed.

## Coherent implementation sequence

1. Add a default-disabled `fixture-ipc` feature in `pve-port`. Move reusable
   daemon/protocol/ledger code into a feature-gated fixture module, retaining the
   existing integration-test entry point and negative protocol tests. Make
   SHA-256 support an optional normal dependency when needed by this module.
   Export only the fixture client and supervisor construction needed by tests.
2. Add daemon-owned provisioning fixture facts: template configuration, power,
   identity, cluster inventory, node/storage/bridge/media preflight, operation
   provenance, and task/UPID state. Persist validated fixture initialization
   and accepted Clone transitions in the log. Replay must reconstruct both VM
   and task state without constructing success from the caller's expectation.
3. Introduce a versioned typed Clone request using the existing validated
   `ProvisioningMutationRequestV1` wire contract. Reject every other mutation
   action initially. Compute the request digest at the daemon. Record an
   attempt, independently compare its expected state with daemon-owned facts,
   allocate a synthetic UPID, then durably record the effect and receipt before
   replying. Keep raw `Effect` bookkeeping unavailable through the controller
   client type. Duplicate operations remain recorded and rejected.
4. Implement `ProvisioningFakePort` and its read supertraits for the fixture
   client inside `pve-port`, where the seal remains accessible. Add a sealed
   controller-fixture trait extending `ProvisioningFakePort` with the async
   checkpoint hook. Implement the hook for `NativeFakePve` by delegation and
   for IPC using a bounded supervisor-controlled barrier.
5. Change the controller's private storage and collection/send helpers to the
   sealed trait. Preserve `OsDeployController::new(..., Arc<NativeFakePve>, ...)`
   for existing callers and add a feature-gated IPC constructor. Generalize
   `submit_fake_once` to accept the sealed provisioning trait without exposing
   the underlying request, cloning the permit, or introducing a second send
   path. Propagate the opt-in feature through operation-controller and any
   required test dependency; leave the service default feature set unchanged.
6. Prove one real controller Clone round-trip using isolated PostgreSQL and the
   daemon subprocess: dispatch commit, one consumed submit, daemon-generated
   validated receipt, and original-response persistence. Read back the journal,
   fixture attempt/effect ledger and task/UPID. This is the first controller IPC
   acceptance point. Only then extend the daemon to capacity/configure and the
   A/B worker-death scenarios from the governing recovery design.

## Required checks for the first code increment

- Default build and observer capability compile-fail tests continue to pass.
- Feature-enabled tests reject malformed typed Clone, unsupported actions,
  stale daemon facts, duplicate operation IDs, incorrect receipts, truncated
  records, oversized frames, and exhausted daemon/request deadlines.
- Restart recovers the same VM, operation provenance, UPID and submission count.
- Consuming-permit compile-fail tests continue proving no clone/copy/restore or
  second submission; the existing NativeFakePve controller path still passes.
- A successful protocol call alone is not reported as worker recovery. The
  separate controller process must actually be terminated and its replacement
  observed against the same PostgreSQL and daemon state before that claim.

This plan requires no production HTTP changes, callback/session contract
changes, real Proxmox access, or deployment. The inspected missing facts and
cross-crate ownership changes make a standalone compile-only wrapper an
incomplete deliverable; this bounded review therefore leaves code unchanged.

## Read-client adapter audit after the capability seam

`FixtureReadClient` now reads status counts, a VM's `disk_bytes` and
`pe_configured`, and an accepted effect bound to operation UUID/digest. Its
three negative doctests pin the absence of provisioning, controller-checkpoint,
and preflight capabilities. These are observations of the bookkeeping daemon,
not sufficient facts for implementing the following traits:

| Required method | Daemon-owned facts still required |
| --- | --- |
| `PveReadPort::vm_config` | Bound node/VM identity, clone provenance and observation time |
| `task_status` | Allocated UPID, node binding, task state/exit status and observation time |
| `storage_content` | Storage-bound volume inventory and content metadata |
| `qga_ping` | VM-bound guest-agent state and observation time |
| `PvePreflightReadPort::node_status` | Node online/uptime snapshot |
| `storage_status` | Node/storage binding, active/shared state, content support and capacity |
| `bridges` | Node-bound bridge inventory |
| `cluster_vms` | Complete cluster inventory including templates and target, with freshness |
| `native_vm_config` | Validated native template/target configuration including identity/provenance |
| `vm_status` | Node/VM-bound power status |
| `ProvisioningFakePort::provisioning_vm_config` | Complete provisioning configuration, disk/media/boot facts and freshness |
| `provisioning_identity` | UUID, MAC and serial identity bound to the requested node/VM |
| `provisioning_media` | Node/storage-bound deployment and driver media inventory |
| `submit_provisioning` | Daemon validation of request against those facts; allocated UPID; durable attempt and accepted transition/receipt before reply |
| `ControllerFixturePort::controller_checkpoint` | Supervisor-owned bounded barrier, independent of worker lifetime |

The existing `World { vmid }` response has neither inventory completeness nor
node binding. A missing VM therefore must not become `NotFound` in a preflight
adapter: that would incorrectly certify target absence. Likewise an accepted
effect is not a task result, and request expectations must not supply the missing
observations. `PveReadError` currently has no `Unsupported` variant; introducing
a placeholder adapter returning `NotFound` would change recovery semantics.

The next concrete protocol increment is a versioned supervisor-initialized
snapshot containing the existing validated preflight/provisioning value types,
fixture UUID and revision. Persist initialization before accepting worker calls;
bind every read response to fixture UUID, requested resource and revision.
Compute inventory from that snapshot plus accepted transitions, retain a
separate task table keyed by daemon-allocated UPID, and sample observation time
at daemon read completion. Validate the snapshot through the existing typed
deserializers. Then implement the read supertraits before granting the private
mutation seal. The raw caller-supplied `Effect { before, after }` protocol remains
test bookkeeping and must not become the controller's submission path.
