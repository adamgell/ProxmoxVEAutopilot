# Post-dispatch observation seam

`FixturePostDispatchV1` is a strict observation envelope for a previously accepted Clone effect. It contains task state, provisioning facts, and complete-inventory-or-error facts. Its decoder requires the independently supplied exact Clone request, accepted receipt bytes, acceptance time, and collection time. It validates fixture/operation/request/node/source/target identity, exact receipt UPID, nested contract versions, every observation timestamp, and wire size. Nested decoders validate config/media timestamps and per-inventory-member status/coverage times.

Missing, failed, absent, running, partial-coverage, and contradictory observations do not acquire success authority through this decoder. They remain available for the existing provisioning evaluator to classify. Decoding an envelope does not mean that a Clone succeeded.

Validation: four `fixture_post_dispatch` tests cover task/error/absence preservation, request/receipt/VM substitution, stale/future timestamps across every observation family, and missing/extra/duplicate/oversized wire fields. The target passes with `fixture-ipc`, strict Clippy passes for that target, and workspace formatting passes.

## Remaining wiring

The daemon does not yet publish this envelope, and `FixtureProvisioningPort` does not consume it. Existing v1 task/collection protocols remain unchanged. The current fresh-controller test still ends at `Decided(Unknown)` because startup facts precede dispatch and `task_status` has no post-dispatch source.

Next, add a supervisor-only publication command that validates this envelope against the daemon's durable accepted effect and its acceptance boundary, persists it before acknowledgment, and exposes a read-only exact-identity lookup. The adapter must use a coherent post-dispatch observation for source, target, inventory, identity, power, and task reads. Workers must never submit desired completion facts or replace the accepted receipt. A positive `run_osdeploy_once` test must then prove `Decided(Satisfied)` through the existing evaluator with one original attempt and exact receipt. This seam alone does not close that gate.
