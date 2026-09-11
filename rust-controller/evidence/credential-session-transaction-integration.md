# Credential/session association integration

Source inspected: `a2cdff22`. This is an implementation boundary assessment;
no session association or runtime persistence is implemented by this artifact.

## Existing authority and the necessary integration

`IssuedRunBearer` is closed, but currently retains only credential bytes. Its
constructor accepts a server-selected legacy identity and expiry; it does not
bind those values to a registered Rust run. `MaterializedPePackageSemanticsV1`
binds the registered run and StartPe operation, but carries no attempt.
`LeaseGrant` is closed and carries the real `AttemptId` and bigint generation.
Its freshness is established only by `current_grant` inside the scheduler
transaction; holding an old grant is insufficient.

An in-memory map over these values would lose its credential ownership history
after restart. It could then attach a deterministic old token to a replacement
attempt. A private map cannot prove the required permanent replacement refusal,
even if duplicate/idempotency unit tests pass within one process.

Implement the association with the StartPe scheduler admission work in
`src/scheduler/osdeploy/pve.rs`, using the existing authority, run, operation,
attempt and lease lock order in `src/scheduler/osdeploy/transaction.rs`.
The existing three-stage admission whitelist presently refuses StartPe; adding
a migration with no admitted caller would not integrate session behavior.

## Concrete association contract

1. Reconstruct materialized package semantics from the registered plan under the
   run lock. Match its run and StartPe operation to the locked snapshot.
2. Obtain the logical attempt from the current database-validated lease grant.
   Use `(run_id, start_pe_operation_id, attempt_id)` as immutable session identity.
   Do not accept a caller-provided attempt or substitute the grant's deadline for
   the original PE registration deadline.
3. Issue a bearer with the server-resolved legacy run identity and explicit
   expiry. Preserve string/integer identity type. The issuer must return closed
   issuance metadata, or issue within this transaction's trusted preparation
   boundary; parsing unsigned token claims cannot establish the association.
4. Persist a private digest of the exact signed bearer bytes as a unique alias
   of the session. No raw bearer belongs in SQL, events, debug or errors. The
   unique alias ownership must survive cancellation, replacement and restart;
   deletion of an old session must not free its aliases for reuse.
5. An identical alias for the same session and package is idempotent. The same
   alias for another session is a conflict. Renewal adds an alias to the same
   session without changing its package or original registration deadline.
   A changed package under the same session is a conflict.
6. Persist arming, original registration anchor, dispatch record and journal
   event atomically. Retain the current final lease/deadline validation before
   commit. Only the existing post-commit path may return dispatch permission.
7. Callback lookup verifies the signed bearer and then resolves the persisted
   alias. Alias ownership is historical association, not current continuation
   permission: cancellation, deadline and current scheduler authority are
   checked separately. Do not remap an old token from a cancelled attempt.

The legacy-to-Rust run mapping must be stored or already reconstructed from a
validated registered run. UUID formatting or numeric coercion is not a substitute
for that mapping. No token wire-format change is required by this contract.

## Required integrated proofs

- Same-second deterministic reissue returns one association; renewal preserves
  session, package and the exact original deadline.
- Two concurrent transactions claiming one alias for different attempts yield
  one committed owner and one conflict without partial session/dispatch rows.
- Restart reload preserves alias ownership, including a cancelled original
  attempt; replacement refuses the old credential.
- Run, operation, attempt and package substitutions fail under the locked
  reconstruction. Stale generation, lease epoch and revision fail before commit.
- Failure after alias insertion and before dispatch commit rolls back both.
  Lost commit response reloads the same association and prevents another start.
- Errors and debug exclude bearer bytes; public JSON cannot construct an issued
  association. Expired aliases never bypass signature/expiry validation.

Validation: read the current issuer, materialized package type, lease grant,
`current_grant`, `admit`, and dispatch transaction. No executable behavior changed;
the cases above remain tests required of the integrated StartPe implementation.
