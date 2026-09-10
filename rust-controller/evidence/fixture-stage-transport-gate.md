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

Full progression still requires migration of all protocol consumers. `FixtureLog`
now accepts additive version-two records carrying stage, attempt UUID, generation,
and owner, alongside the existing operation and request digest. Its duplicate key
is operation/stage: changing attempt, generation, owner, or digest cannot obtain a
second effect for that stage. Exact accepted-effect lookup checks the original
authority binding. Version-one frames retain their original serialization and
behavior. Mixing unscoped legacy and stage-scoped records for one operation is
rejected during append, lookup, and replay because assigning an old record a stage
would be ambiguous. Different operations may use different record versions.

The durable ledger target proves independent identities for all three stages of
one operation, exact restart replay, duplicate effect refusal, wrong authority and
digest rejection, and rejection of correctly checksummed but rebound effect
frames. Its synthetic transitions only exercise the ledger; they do not implement
PVE stage behavior. The daemon does not yet call the version-two ledger methods.

Checkpoint
authorization, consumption, accepted-effect lookup, and publication must use that
same identity; their current Clone-specific request parsing cannot authorize v2
stages. The controller adapter must then submit each durable stage through that
authority, preserving predecessor evidence and stage-specific task receipts.

This seam does not prove three-stage execution, cancellation recovery, or controller
takeover. It provides an executable fail-closed boundary for the pending migration.
