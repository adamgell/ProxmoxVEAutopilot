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
