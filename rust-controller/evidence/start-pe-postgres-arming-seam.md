# Capability-closed PostgreSQL session seam

The fixture-ipc feature now exposes an explicit proposal-schema constant and
two separate entrypoints. Default migrations and dispatch/satisfaction paths
remain unchanged.

`arm_osdeploy_start_pe_session` returns CapabilityUnavailable for the only
available witness variant before acquiring a connection or opening a transaction.
There is no authenticated branch or insertion path.

`probe_osdeploy_start_pe_session_transaction` is diagnostic: it opens a
repeatable-read READ ONLY transaction, compares the proposal with the durable
StartPe dispatch, registration operation and original deadline/event, assesses
the original deadline, and explicitly rolls back on both success and refusal.
Its only non-error outputs are unavailable/expired refusal, never an arm permit.

The opt-in `osdeploy_start_pe_session_proposals` schema has identity/attempt/event/
deadline foreign keys, uniqueness, original-budget checks, immutable rows, and
only the `unavailable` witness state. It is a proposal schema, not a live-session
store; no default migration installs it. Future authenticated arming requires a
new transactional implementation and appropriate constraints, not merely a flag.

Two important unresolved mappings are kept fail-closed:

- Proposal generation/owner are UUIDs. Database authority generation is bigint
  and worker identity is text. They are not reinterpreted as the same fence;
  the schema explicitly separates original authority generation.
- V1 proposal times are milliseconds. PostgreSQL anchors with nonzero
  submillisecond precision are rejected, not silently rounded. A future contract
  must retain full timestamp precision before it can accept those anchors.

Validation: unavailable-witness preconnection test passed; exact timestamp
precision unit test passed. Existing postgres-store library regression run
passed 48 tests before adding the precision test; the precision test then passed
separately. osdeploy arming test passed. Strict all-target fixture-ipc Clippy for
postgres-store/osdeploy-adapter/operation-controller, fmt and diff checks passed.

The SQL DDL rollback test compiles but was not executed: it requires explicit
`PVA_START_PE_SCHEMA_TEST_DSN` for an empty isolated loopback PostgreSQL database.
It creates all prerequisite schema and the proposal table inside one transaction,
then checks that rollback leaves no rust_controller schema. No Docker launcher
is required by that test. A read-only Docker probe currently reports 29.4.0;
therefore Docker is not claimed unavailable. SQL runtime validity and the
diagnostic query's live execution remain unverified in this slice.

No production database mutation was performed by this implementation task.
Authenticated session creation, atomic dispatch/session/deadline persistence,
live authority fencing, worker-death proofs, and controller progression remain
open. No real qmstart or Satisfied transition was enabled.
