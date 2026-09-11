# Typed stored StartPe observation loader

`PgStore::load_fixture_start_pe_response` now returns a distinct closed
`FixtureStoredStartPeResponseV1` in `fixture-ipc` builds. The result exposes
immutable observation data for adapter restoration; it cannot deserialize from
JSON or convert into the original successful-IPC capture type.

The loader runs in a read-only repeatable-read SQL transaction. It validates
the strict StartPe execution snapshot, canonical stage/predecessor identities,
exact encoded request envelopes and original semantic receipt. The stored
request must equal the committed dispatch. The original ConfigurePe operation
must belong to the same run, be Satisfied, and match the stored predecessor
request and attempt. Decoding and re-encoding the receipt must reproduce its
bytes exactly, and its semantic receipt must equal the validated SQL receipt.
An absent fixture row returns None; another stage is rejected.

The genuine combined IPC/PostgreSQL proof now compares every returned identity,
request/predecessor envelope and response byte with the original captured value.
It rejects a ConfigurePe operation passed to the StartPe loader. The independent
reload subprocess now uses this typed loader instead of raw response SQL; the
existing rollback, concurrent replay, original clock/revision and unchanged
IPC journal assertions still execute.

## Configured route ownership remains open

The persisted capture contains fixture UUID and original stage owner/generation,
but not the sealed checkpoint-channel provenance hash. Those UUIDs do not prove
that a restored adapter uses the controller's configured IPC route. The opaque
`FixtureSharedHistoryProvenanceV1` includes a channel in its digest, and that
digest is not retained in `fixture_start_pe_responses` today. Consequently this
loader validates stored observation consistency only; it does not claim route
authentication or current continuation authority.

Closing route ownership requires capturing the original sealed route provenance
with the original response, persisting it in the same transaction, and comparing
it against the operation's trusted configured route during restoration. Existing
rows lacking that provenance must not acquire it from callback fields, decoded
stage UUIDs, or an arbitrary new channel. Original and replacement supervisor
generations require an explicit historical-readback policy.

Validation: genuine combined test passed (1/1, 12.89 seconds), including the
independent-process typed reload. Compile-fail tests cover JSON construction and
conversion back into original IPC capture (2/2 passed). All-target all-feature
Clippy for postgres-store and operation-controller, default store library
compilation, formatting and targeted whitespace checks passed. No physical stop, real Proxmox or
production mutation occurred. This is not current-source Linux qualification.
