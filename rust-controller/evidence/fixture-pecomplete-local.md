# Fixture PeComplete local qualification

This change consumes only the explicitly selected native fixture completion
package. It adds no legacy callback route or generic guest action authority.

The store authenticates the delivered bearer, restores the selected PeRegister
result, consumes the package definition digest and the exact
`boot-files-staged.v1` result fields, and commits one completion result. Every
required boolean must be true for `Satisfied`; any false boolean selects
`Failed` and creates no grace scope. Equivalent replay retains the original
selection time and event. A conflicting report, unsupported package, wrong
credential, stale lease, or run cancellation is refused.

Successful completion atomically creates the original shutdown-grace scope,
a parked attempt, a `Pending -> Waiting` decision and transition, and its due
projection. The parked attempt has no worker lease. Reload derives the due time
from immutable decisions, and projection repair restores that same time.

The completion attempt inherits its deadline from the PeRegister selection;
neither report replay nor projection repair extends it. The grace deadline is
anchored once at completion selection plus the admitted grace budget.

## Diagnostic runs retained

All commands ran locally from this worktree's `rust-controller` directory with
`RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1`. The owned PostgreSQL harness
performed its existing real lease-expiry waits. No production endpoint was used.

- Session `20020`: both new PostgreSQL tests failed at completion claim after
  154.19 seconds. A missing allowance for PeComplete in the global activation
  loader was identified.
- Session `86798`: diagnostic success-path rerun failed after 76.87 seconds;
  temporary location-only diagnostics pinpointed `load_decisions` validation.
  That binary predated the activation-loader correction.
- Session `74223`: both tests advanced to the physical-history capability gate
  and failed after 153.60 seconds. The validator had a fixture callback path
  only for PeRegister. The fix validates the immediate callback predecessor
  recursively through the already verified physical chain, while physical
  context construction remains closed for callbacks.
- Session `65341`: the focused success path passed, 1 test in 88.50 seconds,
  including concurrent first reports, replay/conflict, rollback injections,
  migration replay and parked grace. Temporary diagnostics were removed.

These are diagnostic run records, not claims that the failed intermediate
sources passed. Final verification is recorded separately below.

## Final local verification

- Session `44162`: `cargo test -p postgres-store --all-features --test
  osdeploy_durability fixture_pecomplete -- --nocapture` passed both tests in
  174.51 seconds. This source has no temporary diagnostic logging. Coverage
  includes authenticated success and failure, inherited completion deadline,
  equivalent concurrent selection, conflicting replay, invalid bearer and
  definition/milestone, rollback at completion/grace/projection write boundaries,
  repeated migration replay, load from another store, no grace for failure,
  lease-free parked grace, exact due time after projection deletion and repair,
  and cancellation refusal after selection.
- Session `17171`: the unchanged
  `fixture_peregister_authenticated_selection_replay_and_rollback` regression
  passed, 1 test in 79.77 seconds. Its historical package remains unable to
  acquire the new completion capability.
- Session `84696`: the exact-field report schema unit test passed. Missing and
  unknown fields and non-boolean values are rejected; each required false
  boolean selects the failure interpretation.
- Session `83406`: all-target, all-feature postgres-store Clippy passed with
  warnings denied.
- Default-feature postgres-store compilation, formatting and whitespace checks
  passed after the final source changes.

Focused commands used `RUST_MIN_STACK=16777216 RUST_TEST_THREADS=1` as above.
These are local macOS/store proofs; they do not substitute for Linux execution
or a controller HTTP integration proof.

## Remaining boundary

Elapsed grace adjudication is still gated by `activated_scope_expired`.
Creating a parked grace record does not authorize EnsureStopped or prove
shutdown. The next runtime slice must consume the original due record and
prove the specific elapsed-grace transition before admitting a stop stage.
This change also does not establish HTTP routing, full legacy compatibility,
real WinPE execution, process-kill qualification, or Linux runtime acceptance.
