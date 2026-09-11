# Fixture stop release outcome design

Design target: the next bounded fixture-only continuation after
`FixtureStopReleaseProposalV1`. This is an implementation-ready design note;
it intentionally does not add a release or physical-send implementation while
the current database consumption and checkpoint protocol are separate.

The first storage-only piece is now present in migration
`0016_fixture_stop_release_outcomes.sql`. `PgStore::migrate` applies it only
under the existing `fixture-ipc` feature gate. The table and its immutable
update/delete/truncate triggers are schema-only at this stage; no scheduler
method reads or writes an outcome row, and no release authority is exposed.

The sealed bridge now also exposes `FixtureSharedHistoryProvenanceV1::sha256`
and `FixtureStopReleaseProposalV1::provenance_sha256`. The digest is derived
from the private operation, generation, owner, and channel fields using a
length-delimited canonical encoding; it is never fabricated from admission or
sample JSON. The focused proposal test proves stability for the same sealed
identity and separation when the channel changes.

## Durable outcome record

Add migration `0016_fixture_stop_release_outcomes.sql` with an immutable,
operation-scoped table:

```sql
CREATE TABLE rust_controller.fixture_stop_release_outcomes (
    operation_id uuid PRIMARY KEY
        REFERENCES rust_controller.fixture_stop_outbox(operation_id),
    attempt_id uuid NOT NULL,
    lease_owner uuid NOT NULL,
    generation bigint NOT NULL,
    request_sha256 text NOT NULL,
    admission_sha256 text NOT NULL,
    sample_sha256 text NOT NULL,
    provenance_json jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('accepted', 'refused', 'ambiguous')),
    submit_sequence bigint NOT NULL CHECK (submit_sequence > 0),
    receipt_json jsonb,
    receipt_sha256 text,
    recorded_at timestamptz NOT NULL,
    CHECK ((state = 'accepted') = (receipt_json IS NOT NULL AND receipt_sha256 IS NOT NULL)),
    CHECK (state <> 'accepted' OR receipt_sha256 = encode(sha256(receipt_json::text::bytea), 'hex'))
);
```

The production migration should use the repository's canonical JSON digest
function/representation rather than relying on implicit JSON text ordering;
the SQL above specifies the invariant, not a replacement for that existing
canonicalization helper. Add the same reject-update/delete/truncate triggers
used by `fixture_stop_outbox` and its consumption table. No row may be changed
from `ambiguous` to `accepted` by a retry; reconciliation must first establish
the external fixture result and write a separate audited resolution record.

## Typed Rust boundary

Add the types beside the existing fixture outbox scheduler module:

- `FixtureStopReleaseOutcomeV1`: private fields, constructors requiring a
  validated `FixtureStopReleaseProposalV1`, positive submit sequence, and an
  explicit `Accepted`, `Refused`, or `Ambiguous` state.
- `FixtureStopReleaseReceiptV1`: opaque accepted receipt containing the exact
  proposal digests, sequence, fixture target, and canonical receipt digest.
- `FixtureStopReleaseError`: `Cancelled`, `StaleLease`, `ReplayConflict`,
  `MissingConsumption`, `InvalidEvidence`, `TransportUnavailable`, and
  `ClockExpired`.

Extend `Scheduler` with two transaction-owned methods, in this order:

1. `prepare_fixture_stop_release(grant, proposal)` locks execution, current
   grant, consumption, cancellation/deadline, and the immutable proposal
   digests; it inserts one pending submit sequence or returns the exact prior
   outcome. It must not release the checkpoint.
2. `record_fixture_stop_release_outcome(grant, proposal, sequence, outcome)`
   rechecks the same locked authority and inserts exactly one immutable outcome
   row. A duplicate identical outcome is an idempotent replay; a differing
   outcome, owner, generation, lease, or digest is `ReplayConflict`.

The supervisor adapter then follows this strict order:

`consume outbox -> prepare outcome -> submit fixture command -> record
accepted/refused/ambiguous outcome -> only later expose a release continuation`.

Transport timeout, worker death, or lost acknowledgement records `ambiguous`
when the database transaction is known to have committed; if commit status is
unknown, the caller must return `Ambiguous` and reconcile by operation/sequence
before any retry. No ambiguous result can authorize a second submit.

## Focused proof matrix

Add unit tests in `crates/pve-port/src/fixture_ipc/stage.rs` for canonical
receipt/proposal binding and all three outcome classifications. Add PostgreSQL
tests in `crates/postgres-store/tests/postgres.rs` or a dedicated
`fixture_stop_release_outcome.rs` covering:

1. accepted outcome persists exact proposal and receipt digests;
2. refused outcome persists without a receipt and cannot release;
3. ambiguous outcome survives reload and refuses a second submit;
4. lost acknowledgement/restart returns the same ambiguous outcome;
5. stale owner, generation, lease expiry, cancellation, deadline expiry, and
   mismatched request/admission/sample/provenance are rejected with zero new
   attempt/effect/receipt rows;
6. identical replay is stable, conflicting replay is refused, and immutable
   triggers reject update/delete/truncate;
7. accepted `qmstop` fixture receipt remains bookkeeping only: no stopped-power
   observation or task-success claim is created by this protocol.

Run the focused PostgreSQL test, fixture IPC test, strict Clippy, fmt, and diff
checks. Preserve a restart receipt and database query transcript as evidence.

The storage-only migration proof currently passes:

- `cargo fmt --all`: passed.
- `cargo test -p postgres-store --features fixture-ipc --test postgres
  migration_creates_constrained_foundation_tables -- --exact`: 1 passed;
  inventory includes the outcome table, all six immutability triggers exist,
  and truncate is rejected.
- `cargo test -p pve-port --features fixture-ipc stop_release --lib`: 2
  passed, including sealed-provenance digest stability and channel separation.
- `cargo clippy -p pve-port --features fixture-ipc --lib -- -D warnings`:
  passed.
- `git diff --check`: passed.

## Explicit non-goals and gates

This design does not authorize production `qm stop`, touch `192.168.2.4`,
replace Ansible, or claim stopped power. A later implementation still requires
review of the exact fixture command adapter, recovery/reconciliation behavior,
current-source Linux evidence, and explicit non-production acceptance before
any production deployment or cutover.

This is a bounded Rust controller PoC design, not the complete Ansible-to-Rust
port.
