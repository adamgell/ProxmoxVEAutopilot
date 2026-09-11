# Fixture shutdown grace expiry

The fixture credential scheduler consumes an elapsed, durably parked
`PeShutdownGrace` through the existing `expire_osdeploy_scopes` sweep. Discovery
reads the original immutable scope deadline, so missing disposable schedule rows
cannot postpone expiry. The locked transaction selects `Unknown` with
`shutdown_grace_deadline_expired` and the exact successful PeComplete operation
and event. Elapsed time establishes no observation of VM power state.

The proof requires the fixture completion-package origin, successful immutable
completion record, original parked attempt and deadline, no cancellation, and
no worker lease. It appends the decision and transition and settles the attempt
and schedule in one transaction. A concurrent sweep or replay cannot select a
second expiry. Cancellation before the deadline blocks the parked operation;
cancellation after expiry preserves the already selected Unknown outcome.

## Verification coverage

`fixture_pecomplete_authenticated_report_and_atomic_grace` uses a 30-second
immutable fixture policy and real PostgreSQL time. It checks early sweep has no
effect, ordinary scheduler refusal, forced transaction rollback, concurrent
sweepers selecting once, unchanged attempt/deadline, exact completion anchor,
schedule removal, absence of worker leases, replay, migration/reload, and the
remaining EnsureStopped capability refusal.

`fixture_grace_cancellation_before_due_prevents_elapsed_selection` cancels before
the same real deadline, waits until that deadline, and verifies expiry selects
nothing and the operation remains Blocked.

Local macOS validation passed with `RUST_MIN_STACK=16777216` and
`RUST_TEST_THREADS=1`: final authenticated-completion/grace test 1/1 in 118.94s;
pre-due cancellation test 1/1 in 110.21s. Default `cargo check -p postgres-store`,
all-feature/all-target `cargo clippy -p postgres-store --all-features --all-targets
-- -D warnings`, workspace format check, and `git diff --check` passed. These
results do not constitute Linux or production execution evidence.

## Next execution boundary

`PeEnsureStopped` still requires a physical execution integration. Specifically,
`osdeploy/execution/history.rs::enabled` rejects that stage, the scheduler has no
activation path that binds its `ShutdownGraceOrGuardedEscalation` dependency to
this selected expiry, and the descriptive `PeShutdownScopeV1::assess` continues
to return `GuardedEscalationUnavailable`. The next implementation must reconstruct
trusted clone/configuration/StartPe history, bind the immutable completion and
elapsed grace to a fresh stop-stage mutation scope, evaluate current power and
identity evidence, and commit its original dispatch before obtaining a single
send permit. Reconciliation and cancellation must retain that dispatch identity.

This change does not create an EnsureStopped attempt or assert a stopped VM.
Generic callbacks, imported production runs, real Proxmox mutation, and production
cutover remain outside the implemented fixture path.
