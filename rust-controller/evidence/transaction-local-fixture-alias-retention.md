# Transaction-local fixture alias retention

The first prerequisite from `start-pe-presend-recovery-seam.md` is implemented
in `postgres-store/src/scheduler/osdeploy/fixture_credential.rs`.
`retain_alias_owner` inserts or verifies a digest's complete immutable owner
inside a transaction supplied by the scheduler. The owner binds operation, run,
attempt, package digest and expiry. Exact replay preserves the original row,
including its insertion timestamp; an existing different owner returns Conflict.

The helper and its ownership data are private to the OSDeploy scheduler. It
accepts a fixed 32-byte alias digest, never bearer bytes or signing material. It
does not begin or commit a transaction, mint a credential, return a send permit,
or establish authority. Its caller must derive ownership from locked state and
perform admission and final lease/deadline checks. The existing issuer now uses
this helper with the same transaction, lock order and checks it had before.

This extraction allows a future initial arming transaction to reuse alias
retention without committing independently. It does not connect the initial
alias, session, dispatch, delivery acknowledgement or exposure transitions.
Existing sessions still have the previously documented physical dispatch
semantics; absence of an alias or receipt does not prove they were unsent.
Authenticated callbacks and PeRegister remain separate implementation work.

Local validation:

- `RUST_MIN_STACK=16777216 cargo test --offline --locked -p postgres-store
  --features fixture-ipc --test osdeploy_durability
  fixture_credential_aliases_replay_renew_rollback_and_reopen -- --exact
  --nocapture --test-threads=1`: 1 passed, 0 failed, 9.90 seconds. This covers
  rollback after insertion, concurrent deterministic issuance, reopened-store
  replay, exact immutable owner/timestamp retention, renewal and foreign-owner
  conflict. It uses disposable local PostgreSQL.
- `cargo test --offline --locked -p postgres-store --features fixture-ipc --doc
  issue_fixture_pe_credential`: 1 compile-fail test passed, proving external
  callers cannot import the transaction-local helper.
- Feature-enabled all-target Clippy with warnings denied, formatting and diff
  checks passed.

No Linux runtime qualification or process-death recovery result is claimed for
this refactoring.
