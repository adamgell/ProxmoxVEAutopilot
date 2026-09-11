# Durable fixture run origin registration

`PgStore::create_fixture_osdeploy` is available only with `fixture-ipc`. It
accepts a create-request UUID and an admitted plan. The store allocates the run
UUID and its exact text endpoint identity; callers cannot supply an existing
run, a bearer, an integer identity, or a source namespace. The closed
`FixtureCreatedOsDeployV1` result is returned after commit and cannot be
constructed from JSON or public fields.

The fixed source namespace is `rust-owned-fixture-v1`. This boundary creates new
Rust-owned fixture runs. It does not import or establish ownership of Python
OSDeploy runs, and it grants no callback or dispatch authority.

The create-request advisory lock precedes the existing run and VM identity
locks. The same transaction inserts the immutable origin, VM/agent reservations,
registered workflow, and all stage operations. Exact replay validates the
persisted source, text kind/value, workflow fingerprint and reconstructed plan,
then returns the original IDs. Another create request cannot claim the reserved
VM. A changed plan under the same request conflicts. The origin table prohibits
update, delete and truncate so cancellation cannot release historical ownership.

The schema and public API intentionally admit only server-allocated UUID text
identities. Integer claims, numeric text and foreign source namespaces are
rejected, rather than coerced into this fixture identity domain. Legacy integer
and text compatibility remains a separate service import requirement.

The integrated local PostgreSQL proof
`fixture_create_origin_is_atomic_typed_immutable_and_replayable` covers:

- Failure after origin insertion but before stage registration: no origin,
  workflow, operation or reservation survives rollback.
- Concurrent exact create requests: one durable run with identical returned IDs.
- Changed-plan and different-request VM conflicts without partial rows.
- SQL check rejection of integer kind, substituted text and foreign namespace.
- Immutable historical origin and reopened-store replay after cancellation.

This is database replay evidence, not an operating-system process-kill proof.
The new origin is a prerequisite for a later StartPe transaction to derive the
credential identity from committed registration. StartPe does not yet read this
origin; durable credential aliases, authenticated callbacks and production
legacy import remain unimplemented.

Validation: the integrated proof passed locally with `RUST_MIN_STACK=16777216`
(1 passed, 0 failed, 3.41 seconds). Closed-type compile-fail tests passed (2/2).
Migration inventory tests passed with and without `fixture-ipc` (1/1 each),
including absence of the fixture table in the default build. Default-feature
library compilation and the existing default registration/reload regression
passed (1/1). All-target feature-enabled Clippy with warnings denied,
formatting and diff checks passed. No Linux runtime result is claimed here.
