# Daemon-backed controller Clone admission

The controller accepts `ControllerFixturePort`, whose sealed parent requires
`ProvisioningFakePort`, `PvePreflightReadPort`, and `PveReadPort`. The current
`FixtureMutationClient` does not implement this capability. Its exact-request
Clone seed supplies mutation authorization and a reduced effect, not the
observations needed to admit or reconcile a controller operation.

`crates/pve-port/tests/fixture_controller_admission.rs` executes the real
provisioning preflight evaluator against a known valid Clone episode, removes
each required observation family independently, and checks admission closes.
This is a prerequisite test, not a PostgreSQL/controller round-trip proof.

## Required daemon reads

The Clone collector in `operation-controller/src/osdeploy/collect.rs` needs:

| Port method | Required daemon facts |
| --- | --- |
| `cluster_vms` | A complete synthetic cluster inventory with original observation time; the existing node-scoped snapshot cannot establish global coverage. |
| `provisioning_identity` | Identity for every inventory member (up to the collector's 32-member bound), including collision-relevant identities. |
| `node_status` | Bound node online/uptime observation. |
| `storage_status` | Bound target storage availability, content capabilities and capacity. |
| `bridges` | Bound node bridge inventory. |
| `provisioning_vm_config` | Full validated source configuration and explicit target absence before Clone; replayed target configuration after acceptance. |
| `vm_status` | Source power observation. Target power is collected, but is not required for Clone when target absence is independently proved. |
| `provisioning_media` | Deployment and driver ISO inventories, with storage identity and observation time. |
| `task_status` | After receipt persistence, task state for the exact daemon-generated UPID. The static task seed alone is not a transition model. |

The evaluator test establishes the distinction for absent target power: removing
that read still permits the valid Clone, while removing target configuration
absence closes admission. Do not demand an invented power state for an absent VM.

The remaining inherited methods (`vm_config`, `storage_content`, `qga_ping`, and
`native_vm_config`) must either read explicit daemon facts or return a read
failure. They must not borrow independent `NativeFakePve` state. The checkpoint
must be supervisor-controlled to prove crash placement after durable dispatch.

## Next implementation and acceptance proof

The opt-in `FixtureCloneReads` envelope now represents supervisor observations
for node status, storage, bridges, and complete cluster inventory with identities.
Its bounded loader validates the expected fixture UUID, rejects unknown fields,
duplicate inventory VM IDs and duplicate resource names, and preserves each read's
timestamp and explicit error. Missing files return unavailable. This is a seed
schema prefix only: the daemon does not yet load or serve it, and it grants no
preflight or mutation capability. Full config/media facts and replay projections
remain required. Focused schema tests and feature library Clippy passed.

1. Add a bounded, typed supervisor seed for the read families above. Preserve
   source observation times and explicit read errors. Bind the seed, Clone
   authorization and daemon state to one fixture identity.
2. Serve reads from that daemon's replayed world. A Clone effect must update full
   target config, identity, inventory and task facts; the current reduced
   `VmState` is insufficient for reconciliation.
3. Implement the sealed capability inside `pve-port`, with the read and mutation
   clients sharing the same daemon endpoint and fixture identity. Reject
   unsupported actions and incomplete facts.
4. Run the actual PostgreSQL-backed controller: observe committed dispatch,
   consume its permit once, capture the generated UPID, and verify the exact
   original receipt persisted in the journal. Then kill worker A after acceptance
   and recover with worker B against the same daemon and database; assert no
   second Clone submission and unchanged original request/receipt identity.

The exact-request Clone seed must be created from the admitted operation binding
and before-state, before allowing the worker through its dispatch checkpoint.
It cannot simply be guessed before the journal allocates that binding. Neither
the transport's passing tests nor this admission matrix proves that ordering.
