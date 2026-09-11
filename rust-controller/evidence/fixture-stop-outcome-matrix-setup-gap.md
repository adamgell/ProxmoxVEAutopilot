# Authentic fixture stop outcome matrix setup gap

Audit baseline: `8f3d1608` bound StartPe adapter and current branch
`b886d34e`.

The bound StartPe adapter now supplies genuine daemon receipt/publication and
sealed checkpoint provenance, but it still does not return a PostgreSQL
`OsDeployWorkflowIds`/`LeaseGrant` pair for the same operation. Conversely,
`tests/osdeploy_execution_support::Scenario` can create store-issued leases,
but has no daemon checkpoint socket or `FixtureSharedHistoryProvenanceV1` for a
real accepted StartPe journal. The two existing harnesses therefore cannot yet
be joined without manufacturing either the consumed envelope or release
proposal.

The outcome APIs are implemented and compile, but an end-to-end matrix test
would be invalid until one helper owns both sides. The required helper belongs
in `crates/operation-controller/tests/fixture_prefix_process` (or a shared
support module): it must return the original StartPe request/receipt bytes,
bound provenance, store operation IDs, and current lease/stop authority after
the existing authenticated progression. `osdeploy_durability.rs` can then call
select/consume/prepare/record through public APIs and test accepted, refused,
ambiguous, replay, reopen, rollback, and immutability behavior.

This is a setup/interface gap, not permission to add synthetic SQL. No
physical submit, barrier release, stopped-power claim, production access, or
`192.168.2.4` access was performed.
