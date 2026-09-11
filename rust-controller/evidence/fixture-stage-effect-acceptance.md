# Stage effect acceptance

The stage daemon accepts v2 Clone and DiskCapacity requests after exact stage checkpoint authorization. It persists authority consumption before recording one stage attempt and its receipt/effect. Clone creates only the expected template capacity; DiskCapacity grows only to the operation's expected effective capacity and requires the current durable world to equal the requested before capacity.

Resize carries the original predecessor identity and typed request as evidence. The daemon recomputes the request digest, retrieves its exact durable effect, decodes the accepted receipt, and checks the predecessor's operation/attempt/workflow/plan binding against the resize request. The observation fence may advance between dispatch and satisfied evidence; `same_operation_attempt` deliberately retains all dispatch identity fields while allowing that evidence fence change. The full predecessor plan must equal the accepted Clone plan. Supplying another owner, missing evidence, or an unaccepted predecessor does not consume authority or create an attempt.

The integration proof `durable_clone_then_resize_requires_exact_accepted_predecessor` runs Clone, stops and restarts the daemon, then grows the VM using that accepted Clone. It verifies generated typed receipts at sequences 1 and 2, missing and mismatched predecessor rejection, consumed release rejection, one attempt per stage, and exact receipt recovery. Existing legacy-stage rejection and identity mismatch tests remain passing.

This is daemon acceptance only. Post-dispatch publication/readback and `FixtureProvisioningPort` remain Clone-specific; no full controller DiskCapacity progression, ConfiguredPe success, satisfied resize observation, independent worker takeover, or production readiness is claimed. Production and real Proxmox were not accessed. A separate validation must cover restart after resize itself; this proof restarts after Clone and verifies resize recovery within its accepting daemon lifetime.

Validation: offline/locked `fixture_stage` (2 tests), `fixture_stage_consumers` (2 tests), strict feature Clippy across all targets, and formatting.
