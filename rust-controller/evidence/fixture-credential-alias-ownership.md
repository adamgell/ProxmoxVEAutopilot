# Existing fixture session credential ownership

`Scheduler::issue_fixture_pe_credential` is available only with `fixture-ipc`
and explicit `with_fixture_start_pe()` opt-in. It derives the text bearer identity
from the committed `rust-owned-fixture-v1` origin, validates the current scheduler
authority and lease, reconstructs the registered PE package, and matches the
existing boot session's run, operation, attempt and package. The caller supplies
server signing configuration and absolute expiry, never a run claim, alias digest,
session identity or deserialized authority object.

The transaction stores only SHA-256 of the exact signed bearer and its immutable
owner. The raw bearer is returned in the existing closed `IssuedRunBearer` only
after commit. A failed commit returns no credential. Repeating identical inputs
resolves to the same permanent alias; changed expiry adds an alias to the same
session. Neither path updates the session, package or registration deadline.

The primary-key conflict path reads and compares the complete owner tuple.
An alias belonging to another operation, attempt, run, package or expiry returns
`Conflict`. A composite foreign key pins the tuple to the boot session. Alias
update, delete and truncate are prohibited. Session deletion and owner-field
changes cannot free a referenced alias. Authority, cancellation, lease, credential
expiry and original registration deadline are checked before delivery; retaining
historical aliases grants no right to continue a cancelled run.

The integrated test
`fixture_credential_aliases_replay_renew_rollback_and_reopen` passed locally with
`RUST_MIN_STACK=16777216` (1 passed, 0 failed, 11.23 seconds). It covers missing
session refusal, an AFTER INSERT rollback, concurrent deterministic reissue,
reopened-store replay, renewal preserving package/deadline/attempt, default-path
and stale-generation refusal, expired/empty-secret refusal, SQL immutability,
cross-owner conflict, and cancellation retaining ownership. Cross-owner collision
coverage deliberately inserts an adversarial historical session/alias through
SQL test setup; the public scheduler cannot construct that substituted session.
It demonstrates conflict handling, not a supported import or callback path.
Feature-enabled all-target Clippy passed with warnings denied.
Migration inventory tests passed with and without `fixture-ipc` (1/1 each),
including absence of the alias table in the default build.
The existing StartPe atomic-arming proof also passed (1/1, 8.71 seconds),
including refusal to issue for its caller-selected run without a trusted origin.
Default-feature all-test compilation, formatting and diff checks passed.

This is issuance for an already committed fixture boot session. Initial
session, alias and dispatch atomicity remains unimplemented: dispatch currently
has neither signing configuration nor a credential-delivery result. The operation
controller does not call this new issuance method yet. Python run import, deployed
signing-secret configuration, callback authentication, PeRegister admission and
production replacement remain open. Reopened database replay is not evidence of
operating-system process-kill recovery. No Linux runtime proof is claimed here.
