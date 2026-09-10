# Three-stage transport gate

The `FixtureMutationClient::stage_late` command carries the version-2
`FixtureStageRequest` and checkpoint binding over the bounded local IPC transport.
The daemon explicitly rejects this reserved command before checkpoint consumption,
attempt logging, effect creation, or post-dispatch publication. No stage receipt is
returned. Clone, DiskCapacity, and ConfigurePe all remain gated through this path.
The legacy Clone command retains its existing behavior.

The executable `fixture_stage` integration target checks all three typed requests
are rejected with no attempts or effects, then repeats after daemon restart. The
existing message test rejects later stages and incorrect receipt kinds/bindings.

Full progression requires an atomic protocol migration. `FixtureLog` currently
tracks duplicate attempts by operation UUID alone, including during replay. Thus
the second stage of the same operation is a duplicate even with a distinct valid
attempt and request digest. Changing only dispatch would either reject legitimate
progression or weaken retry protection. The ledger must identify operation/stage/
attempt while retaining exact request binding and replay validation. Checkpoint
authorization, consumption, accepted-effect lookup, and publication must use that
same identity; their current Clone-specific request parsing cannot authorize v2
stages. The controller adapter must then submit each durable stage through that
authority, preserving predecessor evidence and stage-specific task receipts.

This seam does not prove three-stage execution, cancellation recovery, or controller
takeover. It provides an executable fail-closed boundary for the pending migration.
