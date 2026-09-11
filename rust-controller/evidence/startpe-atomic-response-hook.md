# Atomic fixture StartPe response persistence hook

`Scheduler::record_fixture_start_pe_receipt` accepts only the closed
`FixtureStartPeCaptureInput`. It recomposes the original dispatch/IPC binding at
entry and shares the existing original-response capture transaction. The normal
`record_osdeploy_pve_receipt` API retains its previous behavior and default
builds contain no fixture response table or hook.

Under the existing authority/run/operation lock order, the transaction validates
the original dispatch identity, appends the semantic receipt journal event,
inserts the semantic receipt, and inserts the original fixture response before
updating projection and committing. A failure in the fixture insert therefore
rolls back the same SQL transaction rather than leaving a separately committed
semantic receipt.

Feature migration `0017_fixture_start_pe_responses.sql` adds an immutable row
referencing the semantic receipt's operation. It retains canonical stage and
predecessor identity JSON, exact request/predecessor envelopes and original
response envelope bytes. Byte lengths are bounded and SQL generates the response
SHA-256. Update, delete and truncate are prohibited. No bearer/session secret is
included in these task receipt envelopes.

On replay, the semantic receipt must match the original and every fixture field
must match the persisted row byte for byte. The original capture timestamp and
journal event are preserved. If a semantic-only receipt was committed earlier,
the fixture hook refuses the absent original envelope instead of attaching it in
a later transaction. Changed bytes also conflict. The generic semantic-only
replay never alters an existing fixture row.

The hook records original response evidence and returns unit; it grants no
current lease, callback, checkpoint or dispatch authority. The trusted configured
fixture-route join, observation loader and connected genuine IPC+PostgreSQL
proof remain open. The source uses one transaction, but new-path rollback,
concurrent replay and process-restart behavior are not claimed as exercised
until the genuine fixture dispatch harness reaches this hook. No test creates a
synthetic `FixtureStartPeResponseV1` or fake dispatch authority to bypass that
requirement.

Validation results are scoped to feature/default compilation, schema migration,
the existing original-receipt rollback regression and API construction checks;
they must not be read as full qualification of the new fixture write path.

Passed locally: migration inventory 1/1 with `fixture-ipc` and 1/1 without it;
existing `receipt_storage_retry_rolls_back_every_write_and_preserves_first_db_time`
regression 1/1 (2.31 seconds); arbitrary-byte-input compile-fail test 1/1;
feature-enabled all-target compilation and Clippy with warnings denied;
default-feature library compilation; formatting and targeted whitespace checks.
