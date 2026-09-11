# Synchronous ConfigurePe publication

`FixtureSynchronousPostDispatchV1` carries versioned provisioning and inventory
observations for ConfigurePe. It has no task or UPID field; strict decoding
rejects either field, even when null. Decoding requires an exact v2 ConfigurePe
request and its `SynchronousAccepted` receipt. It uses the same source/target
config, power, coverage, deployment/driver media, inventory identity, and
observation-time validation as asynchronous publications. It validates evidence
provenance, not whether the desired ConfigurePe postcondition is satisfied.

The supervisor-only `publish_synchronous_stage` command requires a matching
accepted ConfigurePe effect in the durable stage ledger, including operation,
stage, attempt, generation, owner, and request digest. Its acceptance clock must
come from the current daemon lifetime. Publication is immutable and persisted
before acknowledgement. Workers use `synchronous_stage` through
`FixtureReadClient::synchronous_stage_post_dispatch`; this returns a
`FixtureSynchronousPublication` without constructing any task identity.

Existing ConfigurePe submission still requires exact accepted resize predecessor
evidence and current world state. Its stage authorization is consumed before
recording the effect. This change adds observations and readback without changing
that mutation path. Restart retains effects but invalidates publication and
acceptance clocks; historical effects cannot refresh observations.

Evidence: nine `fixture_post_dispatch` tests pass, including strict synchronous
decoding and a real daemon Clone→resize→ConfigurePe chain. The chain tests missing
predecessor rejection, duplicate mutation refusal, worker publication refusal,
exact synchronous readback, duplicate publication refusal, ownership/generation/
attempt mismatch refusal, and restart invalidation. Strict offline/locked
pve-port feature all-targets Clippy, formatting, and diff checks pass.

Remaining gap: `FixtureProvisioningPort` has no ConfigurePe adapter selection or
late binding for its generated request. The fresh PostgreSQL controller proof
currently ends after DiskCapacity. ConfigurePe still needs accepted resize
predecessor collection, exact request checkpoint/submission, synchronous physical
readback, journaled receipt, and evaluator satisfaction in that controller chain.
Later stages, callbacks/service compatibility, current-source Linux qualification,
and operator/non-production acceptance remain open. Production/default paths were
not changed.
