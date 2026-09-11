# Current fixture stop outcome matrix audit

Audit baseline: `8f3d1608` (bound StartPe adapter) and current branch
`b886d34e`.

## Result

The authentic PostgreSQL outcome matrix is still not safely connectable. The
new StartPe adapter proves a genuine daemon-side StartPe receipt/publication,
but it does not expose the registered controller `OsDeployOperationPlan`,
store-issued `LeaseGrant`, stop `FixtureStopAuthorityV1`, or a connected
`FixtureSharedHistoryProvenanceV1` that can be passed into the PostgreSQL
selection/consumption APIs. Its own evidence explicitly leaves Setup and
PostgreSQL callback/grace integration open.

The existing `Scheduler::prepare_fixture_stop_release` and
`record_fixture_stop_release_outcome` APIs therefore cannot be called from a
real prepared fixture without manufacturing either the consumed envelope or
proposal. Direct SQL inserts would bypass the locked execution/current-grant
checks and would not prove the requested matrix.

## Exact next seam

Extend `crates/operation-controller/tests/fixture_prefix_process` with the
bound StartPe predecessor context from `FixtureProvisioningPort`, then return
the original typed StartPe request/receipt and shared-history provenance to a
PostgreSQL test helper. The helper must use the existing controller/store path
to obtain stop authority, select the outbox row, consume the typed envelope,
and construct the matching proposal. Only then add the accepted/refused/
ambiguous, replay, rollback/reopen, and immutable-row matrix in
`crates/postgres-store/tests/osdeploy_durability.rs`.

## Verification boundary

The bound StartPe evidence remains valid: focused subprocess and full
`fixture_post_dispatch` tests pass, but those tests are daemon/IPC proofs, not
PostgreSQL stop-outcome proofs. No synthetic row, external submit, barrier
release, stopped-power claim, production access, or `192.168.2.4` access was
introduced.

This is a bounded PoC audit, not evidence of the complete Ansible-to-Rust port.
